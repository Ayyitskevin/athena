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

from athena.mentor import pages

VERB_LEARNING_RECORDED = "page_learning_recorded"

#: Bounded so one promotion cannot dominate a page (or a request body). Long enough
#: for a real postmortem; short enough that a runbook stays readable.
MAX_SUMMARY_CHARS = 8000

_RUNBOOK_TITLE_PREFIX = "Runbook: "


def runbook_title(issue_title: str | None, issue_id: int) -> str:
    """A readable title. Identity comes from the 0066 row, never from this string,
    so renaming the page later breaks nothing."""
    name = (issue_title or "").strip()
    return (
        f"{_RUNBOOK_TITLE_PREFIX}{name}"
        if name
        else f"{_RUNBOOK_TITLE_PREFIX}issue #{issue_id}"
    )


def get_runbook(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    """The issue's runbook page, or None — including when the row points at a page
    that has since been deleted, which reads as "no runbook" rather than an error."""
    row = conn.execute(
        "SELECT page_id FROM issue_runbooks WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    if row is None:
        return None
    return pages.get_page(conn, int(row["page_id"]))
