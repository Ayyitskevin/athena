"""Promoting what a run learned into durable Mentor memory.

`VISION.md`'s fifth step — Trust/**Learn** — promises that corrections "feed back
into Mentor as durable context the agents read next time". Mentor pages were
already read by agents (`work_context.py` surfaces linked pages), but nothing ever
wrote back, so every run started from the same knowledge the last one did.

Promoting a learning appends it to the issue's **runbook**: one Mentor page, bound
to the issue by migration 0066, containing what people and agents found out while
working on it. The appended text carries a wiki reference to the issue, so the
link index picks it up, backlinks make it discoverable from the issue, and the
next agent's work-context packet surfaces it **without anyone wiring that up** —
the loop closes through machinery that already existed.

**Three deliberate constraints.**

*Promotion is explicit.* Nothing is promoted automatically, ever. A yield note or
a handoff answer becomes durable memory only because a human or an agent asked for
it. `docs/ACTIVE_WORK.md` classes that text as untrusted advisory input, and the
operator decides what earns a place in the knowledge base.

*Promoted text is quoted, not merged.* The summary is included as a blockquote
under an attribution header Athena writes. A summary that contains its own
headings therefore renders INSIDE the quote rather than forging a second
attribution beside it — untrusted text must not be able to impersonate the
provenance around it. Nothing in it is ever executed or interpreted as an
instruction; it is somebody's report, rendered as one.

*Provenance is verified, not accepted.* The actor comes from the credential, the
timestamp from the server clock, and a named run must be one that actually exists
and that this actor can see — the same rule `activity._validated_lineage` applies
to run ancestry. An unverifiable claim of provenance is worse than none.
"""

from __future__ import annotations

import sqlite3

from athena.core import access, activity, db, links
from athena.mentor import page_commands, pages

VERB_LEARNING_RECORDED = "page_learning_recorded"

#: Bounded so one promotion cannot dominate a page (or a request body). Long enough
#: for a real postmortem; short enough that a runbook stays readable.
MAX_SUMMARY_CHARS = 8000

_RUNBOOK_TITLE_PREFIX = "Runbook: "


class LearningError(Exception):
    """A transport-neutral rejection. ``kind`` maps to each adapter's status."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


STATUS_BY_KIND: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid": 422,
}


def runbook_title(issue_title: str | None, issue_id: int) -> str:
    """A readable title. Identity comes from the 0066 row, never from this string,
    so renaming the page later breaks nothing."""
    name = (issue_title or "").strip()
    return (
        f"{_RUNBOOK_TITLE_PREFIX}{name}"
        if name
        else f"{_RUNBOOK_TITLE_PREFIX}issue #{issue_id}"
    )


def _quoted(summary: str) -> str:
    """Every line prefixed, so the promoted text cannot escape its own quotation.

    A heading, list, or fence inside the summary renders within the blockquote;
    what it cannot do is produce a top-level heading that reads like Athena's own
    attribution for a different actor."""
    return "\n".join(f"> {line}" if line else ">" for line in summary.splitlines())


def get_runbook(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    """The issue's runbook page, or None — including when the row points at a page
    that has since been deleted, which reads as "no runbook" rather than an error."""
    row = conn.execute(
        "SELECT page_id FROM issue_runbooks WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    if row is None:
        return None
    return pages.get_page(conn, int(row["page_id"]))


def _validated_summary(summary: object) -> str:
    if not isinstance(summary, str):
        raise LearningError("invalid", "summary must be a string")
    text = summary.strip()
    if not text:
        raise LearningError("invalid", "summary must not be empty")
    if len(text) > MAX_SUMMARY_CHARS:
        raise LearningError(
            "invalid", f"summary must be at most {MAX_SUMMARY_CHARS} characters"
        )
    return text


def _validated_run_id(
    conn: sqlite3.Connection, actor: dict, run_id: object
) -> str | None:
    """A named run must be real and visible to this actor, or it is not recorded.

    Storing an unverifiable run id would put invented provenance into the knowledge
    base — the same failure `activity._validated_lineage` exists to prevent for run
    ancestry. Here it refuses outright rather than dropping the coordinate, because
    the caller explicitly asked to attribute this learning to that run."""
    if run_id is None:
        return None
    if not isinstance(run_id, str) or not run_id.strip():
        raise LearningError("invalid", "run_id must be a non-empty string")
    normalized = run_id.strip()
    bound = conn.execute(
        "SELECT 1 FROM run_bindings WHERE run_id = ?", (normalized,)
    ).fetchone()
    if bound is None or activity.run_lineage(conn, normalized, actor=actor) is None:
        raise LearningError("not_found", "no such run")
    return normalized


def record_learning(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    summary: object,
    run_id: object = None,
    space_id: int | None = None,
) -> dict:
    """Append one learning to an issue's runbook, creating the page if needed.

    Authorization is checked here rather than left to the boundary, because this
    command crosses two domains: it needs the issue to be visible AND the space
    writable, and a caller that got either wrong would be publishing private work
    into a space, or quietly writing to a runbook it cannot read.

    Returns ``{"page": ..., "created": bool, "runbook_title": str}``.
    """
    if actor is None:
        raise LearningError("unauthorized", "authentication required")
    text = _validated_summary(summary)
    if not access.can_see_issue(conn, actor, issue_id):
        # Missing and hidden collapse: naming an issue id must not reveal one.
        raise LearningError("not_found", "no such issue")
    validated_run_id = _validated_run_id(conn, actor, run_id)
    issue_ref = links.resolve_ref(conn, "issue", issue_id)

    with db.transaction(conn, immediate=True):
        existing = get_runbook(conn, issue_id)
        if existing is None and space_id is None:
            raise LearningError(
                "invalid", "space_id is required to start this issue's runbook"
            )
        if existing is None:
            if not access.can_see_space(conn, actor, int(space_id or 0)):
                raise LearningError("not_found", "no such space")
        elif not access.can_see_page(conn, actor, int(existing["id"])):
            # The runbook moved into a space this actor cannot read. Refuse rather
            # than append into a page they are not allowed to see.
            raise LearningError("not_found", "no such runbook")

        # Athena writes the header; the actor's text is quoted beneath it.
        heading = (
            f"## Learning from run `{validated_run_id}`"
            if validated_run_id
            else "## Learning"
        )
        block = (
            f"{heading}\n\n"
            f"Recorded by **{actor['name']}** "
            f"({'agent' if actor.get('is_agent') else 'human'}) "
            f"while working on [[issue:{issue_id}]].\n\n"
            f"{_quoted(text)}\n"
        )

        if existing is None:
            page = page_commands.create_page(
                conn,
                actor=actor,
                space_id=int(space_id or 0),
                title=runbook_title(issue_ref["title"], issue_id),
                body=(
                    f"What we have learned while working on [[issue:{issue_id}]].\n"
                    f"Each entry below is somebody's report, quoted as recorded.\n\n"
                    f"{block}"
                ),
            )
            conn.execute(
                "INSERT INTO issue_runbooks (issue_id, page_id, created_by) "
                "VALUES (?, ?, ?)",
                (issue_id, page["id"], actor["id"]),
            )
            created = True
        else:
            page = page_commands.edit_page(
                conn,
                actor=actor,
                page_id=int(existing["id"]),
                body=f"{existing['body'].rstrip()}\n\n{block}",
            )
            created = False

        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_LEARNING_RECORDED,
            target_kind="page",
            target_id=int(page["id"]),
            detail=f"issue #{issue_id}",
            commit=False,
        )
        return {
            "page": page,
            "created": created,
            "runbook_title": page["title"],
        }
