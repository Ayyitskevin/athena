"""Data access for Aegis issues.

All issue SQL lives here. HTTP handlers call these functions instead of writing
queries, so if the storage ever changes, only this file does.
"""
from __future__ import annotations

import sqlite3

# The lifecycle an issue moves through. This is the canonical set the whole app
# agrees on — the REST API and the web forms both validate against it, and the
# boards view lays out one column per status. 'open' is the create default
# (matches the schema). Keep this in sync with templates' status <option>s.
STATUSES = ("open", "in_progress", "done")


def create_issue(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    created_by: int,
    status: str = "open",
) -> dict:
    """Insert an issue and return it. Raises sqlite3.IntegrityError if
    created_by isn't a real user (the foreign key refuses the orphan)."""
    cur = conn.execute(
        "INSERT INTO issues (title, body, status, created_by) VALUES (?, ?, ?, ?)",
        (title, body, status, created_by),
    )
    conn.commit()
    return get_issue(conn, cur.lastrowid)


def update_status(
    conn: sqlite3.Connection, issue_id: int, status: str
) -> dict | None:
    """Move an issue to a new status. Returns the updated issue, or None if no
    issue has that id (so the caller can answer 404). Validating the status
    against STATUSES is the boundary's job, not this function's."""
    cur = conn.execute(
        "UPDATE issues SET status = ? WHERE id = ?", (status, issue_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def get_issue(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return dict(row) if row else None


def list_issues(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM issues ORDER BY id").fetchall()
    return [dict(row) for row in rows]
