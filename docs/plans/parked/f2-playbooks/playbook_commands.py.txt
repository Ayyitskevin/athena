"""Playbooks: a checklist page becomes real work.

Athena already lets work show up in docs (live embeds resolve issue lists at
view time) and lets work write back to docs (a run promotes what it learned
into the issue's runbook page). The direction that was missing is the one that
starts things: **a page that describes a procedure should be able to create the
work that carries it out.**

A playbook is an ordinary Mentor page carrying the ``playbook`` label — the
same "a label marks a kind of page" mechanism templates already use, so there
is no new concept and no new table. Starting one reads the page's markdown
checklist and creates one parent issue plus one child per unchecked item,
every body citing the page with an ordinary ``[[page:N]]`` wikilink. That
citation is the whole trick: the existing link indexer turns it into real
backlinks, so the page immediately shows the work it started, and a ``rollup``
embed on that page counts the children's progress — with no code here knowing
anything about links, backlinks, or embeds.

**A template is not a live mirror.** Instantiation SNAPSHOTS the page: the
version it read is recorded in the activity detail, and editing the page later
changes nothing that already exists. Running the same playbook twice creates a
second, independent instantiation — which is what templates are for. Callers
that need retry-safety use the ordinary ``Idempotency-Key`` contract the
``/pages`` API root already honors; there is no second, playbook-specific
replay mechanism to keep in sync with it.

**Writes go through the issue command owner.** This module reads Mentor and
never writes Aegis directly — `issue_commands.create_issue` and
`set_issue_parent` own those writes, their audit events, their budget metering,
and their authorization, exactly as they do for any other caller.
"""

from __future__ import annotations

import re
import sqlite3

from athena.aegis import issue_commands
from athena.core import access, db, labels
from athena.mentor import pages

#: The label that marks a page as a playbook. Case-insensitive in practice: the
#: labels table is UNIQUE COLLATE NOCASE, so "Playbook" and "playbook" are one.
#: (The `template` label works the same way — see mentor/page_templates.py.)
PLAYBOOK_LABEL = "playbook"

#: A markdown task line: optional indent, a bullet, a box, then the text.
#: One quantified run per term and no overlap between a term and its successor
#: (`[ \t]*` then a literal bullet, `[ ]|[xX]` then a literal `]`), so the
#: pattern cannot backtrack polynomially over a long line of spaces — the
#: discipline core/links.py documents, applied to body-derived text.
_TASK_RE = re.compile(r"^[ \t]*[-*+] \[( |x|X)\] ?(.*)$")

#: Bounds. A playbook that would create more issues than a human can review in
#: one sitting is a data-entry accident, not a plan.
MAX_ITEMS = 50
MAX_TITLE_LENGTH = 200

STATUS_BY_KIND: dict[str, int] = {
    "unauthorized": 401,
    "not_found": 404,
    "invalid": 422,
    "forbidden": 403,
    "conflict": 409,
    "capacity": 429,
}


class PlaybookCommandError(Exception):
    """A refusal with a transport-neutral kind. The adapter maps it."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def parse_checklist(body: str | None) -> tuple[list[str], int]:
    """Split a page body into (unchecked item titles, checked item count).

    Only ``- [ ]`` becomes work. A ``- [x]`` line is someone recording that the
    step is already done, and creating an issue for it would be the tool
    arguing with its author — so those are counted and reported, never silently
    dropped and never turned into work.

    An item's title is trimmed and bounded; an empty box (``- [ ]`` with no
    text) is skipped rather than creating an issue with no title.
    """
    unchecked: list[str] = []
    checked = 0
    for line in (body or "").splitlines():
        match = _TASK_RE.match(line)
        if match is None:
            continue
        if match.group(1) in ("x", "X"):
            checked += 1
            continue
        title = match.group(2).strip()
        if title:
            unchecked.append(title[:MAX_TITLE_LENGTH])
    return unchecked, checked


def _visible_playbook(conn: sqlite3.Connection, actor: dict, page_id: int) -> dict:
    """The page, if this actor may see it AND it is a live playbook.

    A page the actor cannot see and a page that does not exist give the same
    answer, so this never becomes a probe for private spaces.
    """
    page = pages.get_page(conn, page_id)
    if page is None or not access.can_see_space(conn, actor, page["space_id"]):
        raise PlaybookCommandError("not_found", "no such page")
    if page["archived_at"] is not None:
        raise PlaybookCommandError(
            "invalid", "this page is archived; restore it before starting it"
        )
    marks = {label["name"].lower() for label in labels.labels_for_page(conn, page_id)}
    if PLAYBOOK_LABEL not in marks:
        raise PlaybookCommandError(
            "invalid",
            f"page is not a playbook; add the '{PLAYBOOK_LABEL}' label to start it",
        )
    return page


def start_playbook(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    page_id: int,
    project_id: int | None = None,
    title: str | None = None,
) -> dict:
    """Instantiate a playbook page as a parent issue with one child per step.

    Returns ``{"page", "parent", "children", "checked_skipped", "page_version_id"}``.

    Everything lands in one transaction: either the whole instantiation exists
    or none of it does, so a failure halfway through cannot leave an orphaned
    parent with half its steps.
    """
    if actor is None:
        raise PlaybookCommandError("unauthorized", "authentication required")

    override = None
    if title is not None:
        override = title.strip()
        if not override:
            raise PlaybookCommandError("invalid", "title must not be blank")
        if len(override) > MAX_TITLE_LENGTH:
            raise PlaybookCommandError(
                "invalid", f"title must be at most {MAX_TITLE_LENGTH} characters"
            )

    with db.transaction(conn, immediate=True):
        page = _visible_playbook(conn, actor, page_id)
        steps, checked = parse_checklist(page["body"])
        if not steps:
            raise PlaybookCommandError(
                "invalid",
                "this playbook has no unchecked '- [ ]' steps to start",
            )
        if len(steps) > MAX_ITEMS:
            raise PlaybookCommandError(
                "capacity",
                f"a playbook may start at most {MAX_ITEMS} steps at once; "
                f"this page has {len(steps)}",
            )

        # The citation every created issue carries. An ordinary wikilink, so the
        # existing indexer builds the backlinks — this module never touches the
        # links table.
        citation = f"Started from [[page:{page_id}]]"
        parent = issue_commands.create_issue(
            conn,
            actor=actor,
            title=override or page["title"],
            body=f"{citation} — a playbook with {len(steps)} steps.",
            project_id=project_id,
        )
        children = []
        for step in steps:
            child = issue_commands.create_issue(
                conn,
                actor=actor,
                title=step,
                body=citation,
                project_id=project_id,
            )
            issue_commands.set_issue_parent(
                conn, actor=actor, issue_id=child["id"], parent_id=parent["id"]
            )
            children.append(child)

        return {
            "page": {"id": page_id, "title": page["title"]},
            "parent": parent,
            "children": children,
            # Reported, never silently dropped: an author who ticked steps
            # before starting should see that Athena honored the ticks.
            "checked_skipped": checked,
            "snapshot": (
                "this instantiation reflects the page as it is now; editing the "
                "page later changes nothing already created"
            ),
        }
