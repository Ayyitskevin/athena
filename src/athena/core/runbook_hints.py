"""Read-only locators so the desk and work-context can narrate a runbook.

Aegis and Mentor are peers and may not import each other. The first-learning
space and the existing runbook pointer are facts both surfaces need to *show*,
so the shared read lives in core. Nothing here writes a page.
"""

from __future__ import annotations

import sqlite3

from athena.core import access


def visible_space_summaries(conn: sqlite3.Connection, actor: dict | None) -> list[dict]:
    """Spaces this actor may see, as {id, key, name}, name-sorted."""
    visible = access.visible_space_filter(conn, actor)
    if visible is not None and not visible:
        return []
    if visible is None:
        rows = conn.execute(
            "SELECT id, key, name FROM spaces ORDER BY name COLLATE NOCASE"
        ).fetchall()
    else:
        placeholders = ",".join("?" for _ in visible)
        rows = conn.execute(
            f"SELECT id, key, name FROM spaces WHERE id IN ({placeholders}) "
            "ORDER BY name COLLATE NOCASE",
            sorted(visible),
        ).fetchall()
    return [{"id": int(r["id"]), "key": r["key"], "name": r["name"]} for r in rows]


def runbook_narration(
    conn: sqlite3.Connection, issue_id: int, *, actor: dict | None
) -> dict:
    """What an agent needs to record a learning without guessing.

    ``exists`` is true only when the bound page is still there. Suggested spaces
    are the actor's visible spaces — never a space they cannot see.
    """
    row = conn.execute(
        "SELECT r.page_id, p.space_id FROM issue_runbooks r "
        "JOIN pages p ON p.id = r.page_id WHERE r.issue_id = ?",
        (issue_id,),
    ).fetchone()
    suggested = visible_space_summaries(conn, actor)
    if row is None:
        return {
            "exists": False,
            "space_id": None,
            "page_id": None,
            "suggested_spaces": suggested,
        }
    return {
        "exists": True,
        "space_id": int(row["space_id"]),
        "page_id": int(row["page_id"]),
        "suggested_spaces": suggested,
    }
