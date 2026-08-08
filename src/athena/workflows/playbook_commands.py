"""Playbooks: a checklist page becomes real work.

Athena already lets work show up in docs (live embeds resolve issue lists at
view time) and lets work write back to docs (a run promotes what it learned
into the issue's runbook page). The direction that was missing is the one that
starts things: **a page that describes a procedure should be able to create the
work that carries it out.**

A playbook is an ordinary Mentor page carrying the ``playbook`` label — the
same "a label marks a kind of page" mechanism templates already use, so there
is no new concept and no new table. Starting one reads the page's markdown
checklist and creates one parent issue plus one child per unchecked item —
and an INDENTED item nests under the issue its enclosing item became, so a
checklist with sub-steps instantiates as the same issue hierarchy a hand
would build (`set_issue_parent` owns that write, as ever). Every body cites
the page with an ordinary ``[[page:N]]`` wikilink. That
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

#: A markdown task line: optional indent (captured — it carries the nesting),
#: a bullet, a box, then the text.
#: One quantified run per term and no overlap between a term and its successor
#: (`[ \t]*` then a literal bullet, `[ ]|[xX]` then a literal `]`), so the
#: pattern cannot backtrack polynomially over a long line of spaces — the
#: discipline core/links.py documents, applied to body-derived text.
_TASK_RE = re.compile(r"^([ \t]*)[-*+] \[( |x|X)\] ?(.*)$")

#: One tab of indentation reads as four spaces when measuring nesting. A fixed
#: published equivalence, because "how wide is a tab" must not depend on the
#: author's editor for the structure of the work it creates.
_TAB_WIDTH = 4

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


def parse_checklist(body: str | None) -> tuple[list[dict], int]:
    """Split a page body into (unchecked work items, checked item count).

    Each item is ``{"title": str, "parent": int | None}`` where ``parent``
    indexes an EARLIER item in the returned list — indentation maps to
    hierarchy, so an indented step nests under the nearest less-indented task
    line above it. Relative indent is all that matters (two spaces or a full
    tab both read as "deeper"); a tab measures ``_TAB_WIDTH`` spaces.

    Only ``- [ ]`` becomes work. A ``- [x]`` line is someone recording that the
    step is already done, and creating an issue for it would be the tool
    arguing with its author — so those are counted and reported, never silently
    dropped and never turned into work. A done step's unchecked children are
    still real work, though: they promote to the nearest ancestor that IS work
    (top level when there is none), rather than vanishing with their parent or
    resurrecting it.

    An item's title is trimmed and bounded; an empty box (``- [ ]`` with no
    text) is skipped rather than creating an issue with no title, and its
    children promote the same way.
    """
    items: list[dict] = []
    checked = 0
    # The enclosing task lines, innermost last: (indent, effective_parent) where
    # effective_parent is the item index descendants should nest under — the
    # line's own index when it became work, or its inherited parent when it did
    # not (checked, or an empty box).
    stack: list[tuple[int, int | None]] = []
    for line in (body or "").splitlines():
        match = _TASK_RE.match(line)
        if match is None:
            continue
        indent = len(match.group(1).expandtabs(_TAB_WIDTH))
        # Keep only STRICT ancestors: a sibling (equal indent) or a dedent pops.
        while stack and indent <= stack[-1][0]:
            stack.pop()
        inherited = stack[-1][1] if stack else None
        if match.group(2) in ("x", "X"):
            checked += 1
            stack.append((indent, inherited))
            continue
        title = match.group(3).strip()
        if not title:
            stack.append((indent, inherited))
            continue
        items.append({"title": title[:MAX_TITLE_LENGTH], "parent": inherited})
        stack.append((indent, len(items) - 1))
    return items, checked


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

    Returns ``{"page", "parent", "children", "checked_skipped", "snapshot"}``.
    ``children`` is in reading order, and each child's ``parent_id`` is real:
    an indented step nests under the issue its enclosing step became, so the
    checklist's shape IS the issue hierarchy — one ``set_issue_parent`` per
    child, the same command a hand-built tree uses.

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
        children: list[dict] = []
        for step in steps:
            child = issue_commands.create_issue(
                conn,
                actor=actor,
                title=step["title"],
                body=citation,
                project_id=project_id,
            )
            # A top-level step nests under the instantiation's parent issue; an
            # indented one under the issue its enclosing step just became.
            # set_issue_parent returns the updated row, so every child reported
            # back carries its REAL parent_id rather than the pre-nesting None.
            structural_parent = (
                parent["id"]
                if step["parent"] is None
                else children[step["parent"]]["id"]
            )
            child = issue_commands.set_issue_parent(
                conn, actor=actor, issue_id=child["id"], parent_id=structural_parent
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
