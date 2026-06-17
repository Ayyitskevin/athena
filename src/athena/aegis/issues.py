"""Data access for Aegis issues.

All issue SQL lives here. HTTP handlers call these functions instead of writing
queries, so if the storage ever changes, only this file does.
"""
from __future__ import annotations

import sqlite3


def create_issue(
    conn: sqlite3.Connection, *, title: str, body: str, created_by: int
) -> dict:
    """Insert an issue and return it. Raises sqlite3.IntegrityError if
    created_by isn't a real user (the foreign key refuses the orphan)."""
    cur = conn.execute(
        "INSERT INTO issues (title, body, created_by) VALUES (?, ?, ?)",
        (title, body, created_by),
    )
    conn.commit()
    return get_issue(conn, cur.lastrowid)


def get_issue(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return dict(row) if row else None


def list_issues(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM issues ORDER BY id").fetchall()
    return [dict(row) for row in rows]
