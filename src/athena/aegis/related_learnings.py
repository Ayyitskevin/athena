"""Learnings recorded on an issue, its parent and its siblings (W5, D22).

A learning is appended to one issue's runbook page, and reached the next agent
only through that issue's backlinks. A rule learned on one slice of an outcome
did not reach the slice beside it: the reviewer's HOLD on the first slice was
invisible to whoever built the second.

The work-context packet now lists the newest learnings recorded on the issue,
on its parent and on its siblings under the same parent. No wider scope and no
new table (D22): each row is a native ``page_learning_recorded`` event on a
runbook page bound to one of those issues, which says who recorded it and
when. The text is the runbook's own, read from the page as recorded.

**The text is paired with its event by position.** The recorder appends one
block per learning under a heading only it writes (a quoted summary cannot
produce one, by ``run_learning_commands._quoted``), and records one event with
it. So the n-th block is the n-th event's text while the two counts agree. A
runbook edited by hand can break that; then no text is paired at all and the
row says to read the page, rather than attribute words to the wrong account.

Hidden reads as missing: an issue the actor cannot see contributes nothing, a
sibling is read only through a parent the actor can see, and a runbook page in
a space the actor cannot read contributes nothing.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from athena.core import access, authorship

VERB_LEARNING_RECORDED = "page_learning_recorded"
#: The newest learnings the packet carries, and how much of each.
MAX_ITEMS = 5
MAX_EXCERPT_CHARS = 500
#: Candidates read before visibility is applied: the read is bounded whatever
#: the outcome's size.
MAX_EXAMINED = 50
MAX_SIBLINGS = 200

_HEADING = re.compile(r"^## Learning(?: from run `[^`\n]*`)?[ \t]*$", re.MULTILINE)


def learning_texts(body: str) -> list[str]:
    """The text of each learning in a runbook body, in the order recorded: the
    quoted lines under each learning heading, unquoted."""
    texts = []
    for part in _HEADING.split(body)[1:]:
        quoted = [line for line in part.splitlines() if line.startswith(">")]
        texts.append(
            "\n".join(
                line[2:] if line.startswith("> ") else line[1:] for line in quoted
            )
        )
    return texts


def _related_issues(
    conn: sqlite3.Connection,
    issue: dict,
    actor: dict | None,
    visible_project_ids: set[int] | None,
) -> dict[int, str]:
    """``{issue id: relation}`` for the issue, its visible parent and, through
    that parent, its visible siblings."""
    related = {int(issue["id"]): "issue"}
    parent_id = issue.get("parent_id")
    if not parent_id or not access.can_see_issue(conn, actor, int(parent_id)):
        return related
    related[int(parent_id)] = "parent"
    for row in conn.execute(
        "SELECT id, project_id FROM issues WHERE parent_id = ? AND id != ? "
        "AND archived_at IS NULL ORDER BY id LIMIT ?",
        (parent_id, issue["id"], MAX_SIBLINGS),
    ):
        project_id = row["project_id"]
        if (
            visible_project_ids is None
            or project_id is None
            or int(project_id) in visible_project_ids
        ):
            related[int(row["id"])] = "sibling"
    return related


def _paired_texts(conn: sqlite3.Connection, page_id: int) -> dict[int, str] | None:
    """``{event id: text}`` for one runbook page, or None when its learning
    blocks and its learning events no longer correspond one to one."""
    page = conn.execute("SELECT body FROM pages WHERE id = ?", (page_id,)).fetchone()
    if page is None:
        return None
    texts = learning_texts(page["body"] or "")
    event_ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM activity WHERE target_kind = 'page' AND target_id = ? "
            "AND verb = ? AND imported_at IS NULL ORDER BY id",
            (page_id, VERB_LEARNING_RECORDED),
        )
    ]
    if len(texts) != len(event_ids):
        return None
    return dict(zip(event_ids, texts, strict=True))


def related_learnings(
    conn: sqlite3.Connection,
    *,
    issue: dict,
    actor: dict | None,
    visible_project_ids: set[int] | None,
) -> dict[str, Any]:
    """The newest learnings recorded on ``issue``, its parent and its siblings,
    newest first, as the packet carries them."""
    related = _related_issues(conn, issue, actor, visible_project_ids)
    marks = ",".join("?" for _ in related)
    rows = conn.execute(
        "SELECT a.id AS event_id, a.actor_id, a.created_at, r.issue_id, r.page_id "
        "FROM issue_runbooks r "
        "JOIN activity a ON a.target_kind = 'page' AND a.target_id = r.page_id "
        "AND a.verb = ? AND a.imported_at IS NULL "
        f"WHERE r.issue_id IN ({marks}) ORDER BY a.id DESC LIMIT ?",
        (VERB_LEARNING_RECORDED, *related, MAX_EXAMINED),
    ).fetchall()
    readable: dict[int, bool] = {}
    paired: dict[int, dict[int, str] | None] = {}
    visible = []
    for row in rows:
        page_id = int(row["page_id"])
        if page_id not in readable:
            readable[page_id] = access.can_see_page(conn, actor, page_id)
        if readable[page_id]:
            visible.append(row)
    authors = authorship.accounts(
        conn, (row["actor_id"] for row in visible[:MAX_ITEMS])
    )
    items = []
    for row in visible[:MAX_ITEMS]:
        page_id = int(row["page_id"])
        if page_id not in paired:
            paired[page_id] = _paired_texts(conn, page_id)
        texts = paired[page_id]
        text = None if texts is None else texts.get(int(row["event_id"]))
        items.append(
            {
                "issue_id": int(row["issue_id"]),
                "relation": related[int(row["issue_id"])],
                "runbook_page_id": page_id,
                "recorded_at": row["created_at"],
                authorship.KEY: authors.get(int(row["actor_id"])),
                # Another account's report, quoted as recorded: data to weigh,
                # never an instruction. null when the runbook was edited by
                # hand and its text can no longer be paired with its author;
                # the page itself is the place to read it then.
                "excerpt": None if text is None else text[:MAX_EXCERPT_CHARS],
                "truncated": text is not None and len(text) > MAX_EXCERPT_CHARS,
                "advisory_untrusted": True,
            }
        )
    return {"items": items, "total": len(visible)}
