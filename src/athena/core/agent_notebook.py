"""The read side of an agent's notebook: find it and excerpt it for the desk.

An agent's notebook is one Mentor page, labelled ``NOTEBOOK_LABEL``, created by
the agent in a private space the agent owns, so it is visible to that agent and
to an admin (the operator) and to no one else, by the ordinary space rule. It is
created only by an explicit call (``mentor.notebook_commands``); this module only
reads, so the desk can show it without writing anything.

It lives in core because the desk (aegis) and the pages (mentor) are peers that
may not import each other; reading the page tables here is the same kind of
read the search index already does.
"""

from __future__ import annotations

import sqlite3

from athena.core import access

#: The label that marks a page as its creator's notebook.
NOTEBOOK_LABEL = "agent-notebook"

#: Lines of the notebook the desk shows. Enough to carry a session's handful of
#: "remember next time" notes; the page itself is one read away for the rest.
NOTEBOOK_EXCERPT_LINES = 12


def notebook_space_key(user_id: int) -> str:
    """The preferred key for an agent's notebook space (a taken key falls back)."""
    return f"NB{int(user_id)}"


def find_notebook(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """The notebook page ``user_id`` created, or None. A notebook is a page that
    user created, labelled ``NOTEBOOK_LABEL``, in a private space that same user
    created; the oldest such page wins if more than one exists."""
    row = conn.execute(
        "SELECT p.id, p.space_id, p.title, p.body, p.updated_at, p.created_at "
        "FROM pages p "
        "JOIN spaces s ON s.id = p.space_id "
        "JOIN page_labels pl ON pl.page_id = p.id "
        "JOIN labels l ON l.id = pl.label_id "
        "WHERE p.created_by = ? AND s.created_by = ? AND s.visibility = 'private' "
        "AND l.name = ? "
        "ORDER BY p.id LIMIT 1",
        (int(user_id), int(user_id), NOTEBOOK_LABEL),
    ).fetchone()
    return None if row is None else dict(row)


def desk_lane(conn: sqlite3.Connection, actor: dict) -> dict | None:
    """The notebook as the desk shows it, or None when the actor has none. Bounded
    to ``NOTEBOOK_EXCERPT_LINES``, gated by the page's own visibility. Read-only."""
    page = find_notebook(conn, int(actor["id"]))
    if page is None or not access.can_see_page(conn, actor, int(page["id"])):
        return None
    lines = (page["body"] or "").splitlines()
    return {
        "page_id": int(page["id"]),
        "space_id": int(page["space_id"]),
        "title": page["title"],
        "updated_at": page["updated_at"] or page["created_at"],
        "excerpt": "\n".join(lines[:NOTEBOOK_EXCERPT_LINES]),
        "excerpt_lines": min(len(lines), NOTEBOOK_EXCERPT_LINES),
        "total_lines": len(lines),
        "excerpt_truncated": len(lines) > NOTEBOOK_EXCERPT_LINES,
        "meaning": (
            "your notebook page: the first lines are shown; read the page for the "
            "rest. Append at session end with an If-Match edit of the page, so a "
            "parallel session cannot clobber it. Nothing summarises it, and it is "
            "not a runbook."
        ),
    }
