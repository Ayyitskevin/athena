"""Data access for pages — a single document inside a space.

All page SQL lives here, mirroring mentor/spaces.py and aegis/issues.py. A page
belongs to one space and may nest under a parent page (parent_id) to form a tree.
This slice covers create + read; editing a page (and snapshotting versions) is a
later slice. The cross-space tree rule (a parent must share the page's space) is
enforced at the API boundary, not here.
"""
from __future__ import annotations

import sqlite3


def create_page(
    conn: sqlite3.Connection,
    *,
    space_id: int,
    title: str,
    created_by: int,
    body: str = "",
    parent_id: int | None = None,
) -> dict:
    """Insert a page and return it. The foreign keys refuse an orphan: space_id
    must be a real space, parent_id (when given) a real page, created_by a real
    user."""
    cur = conn.execute(
        "INSERT INTO pages (space_id, parent_id, title, body, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (space_id, parent_id, title, body, created_by),
    )
    conn.commit()
    return get_page(conn, cur.lastrowid)


def get_page(conn: sqlite3.Connection, page_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    return dict(row) if row else None


def list_pages_in_space(conn: sqlite3.Connection, space_id: int) -> list[dict]:
    """Every page in a space, alphabetical by title. Each row carries its
    parent_id so a caller can assemble the tree; this returns the flat set."""
    rows = conn.execute(
        "SELECT * FROM pages WHERE space_id = ? ORDER BY title COLLATE NOCASE",
        (space_id,),
    ).fetchall()
    return [dict(row) for row in rows]
