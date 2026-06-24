"""Data access for spaces — a top-level container for documentation pages.

All space SQL lives here, mirroring aegis/projects.py. A space is the Mentor
equivalent of an Aegis project, but identified by a short KEY (e.g. "ENG") rather
than by its name, because pages will later be addressed by that key. Pages (a
later slice) will reference a space; this module owns only the space row itself.
"""
from __future__ import annotations

import sqlite3


def create_space(
    conn: sqlite3.Connection,
    *,
    key: str,
    name: str,
    created_by: int,
    description: str = "",
) -> dict:
    """Insert a space and return it. Raises sqlite3.IntegrityError if a space with
    this key already exists (key is UNIQUE, case-insensitive) or if created_by
    isn't a real user (the foreign key refuses the orphan)."""
    cur = conn.execute(
        "INSERT INTO spaces (key, name, description, created_by) VALUES (?, ?, ?, ?)",
        (key, name, description, created_by),
    )
    conn.commit()
    return get_space(conn, cur.lastrowid)


def get_space(conn: sqlite3.Connection, space_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
    return dict(row) if row else None


def get_space_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    """Look a space up by its key (case-insensitive — the column is COLLATE NOCASE)."""
    row = conn.execute("SELECT * FROM spaces WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def list_spaces(conn: sqlite3.Connection) -> list[dict]:
    """Every space, alphabetical by name."""
    rows = conn.execute(
        "SELECT * FROM spaces ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [dict(row) for row in rows]


def delete_space(conn: sqlite3.Connection, space_id: int) -> bool:
    """Delete a space. Returns True if a space was deleted, False if no space had
    that id (so the boundary can 404).

    PRECONDITION: the space holds no pages. The caller checks
    pages.count_pages_in_space first and returns a clean 409 — we do NOT cascade,
    because deleting a whole documentation tree is exactly the kind of irreversible
    bulk loss a wiki must never do silently (the same rule project- and page-delete
    follow). Unlike a page, a space has no version history or derived link/search
    rows of its own, so there is nothing to clean up beyond the row itself. (If a
    stray page somehow remained, the pages.space_id foreign key has no ON DELETE, so
    it would refuse the delete and raise rather than orphan it.)"""
    cur = conn.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
    conn.commit()
    return cur.rowcount > 0
