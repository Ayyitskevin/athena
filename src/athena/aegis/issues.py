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

# Every read returns the assignee's display name alongside the row (NULL when
# unassigned), so callers never resolve assignee_id -> name themselves. LEFT
# JOIN, not JOIN: an unassigned issue must still come back.
_SELECT = (
    "SELECT i.*, u.name AS assignee_name "
    "FROM issues i LEFT JOIN users u ON u.id = i.assignee_id"
)


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


def update_issue(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
) -> dict | None:
    """Partial update: only the fields passed as non-None change. Returns the
    updated issue, or None if no issue has that id (so the caller can 404).
    Field validation (status in STATUSES, non-empty title) is the boundary's job.

    The column names below are hardcoded literals, never caller input, so the
    f-string assembles a safe SET clause; the values stay parameterized."""
    fields = {
        col: val
        for col, val in (("title", title), ("body", body), ("status", status))
        if val is not None
    }
    if not fields:
        # Nothing to change — still distinguish "no such issue" (None) from a
        # real but unchanged issue, so the boundary's 404 stays correct.
        return get_issue(conn, issue_id)
    assignments = ", ".join(f"{col} = ?" for col in fields)
    cur = conn.execute(
        f"UPDATE issues SET {assignments} WHERE id = ?",
        (*fields.values(), issue_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def update_status(
    conn: sqlite3.Connection, issue_id: int, status: str
) -> dict | None:
    """Move an issue to a new status. Thin wrapper over update_issue, kept as a
    named operation for the status-change call sites (web route + API)."""
    return update_issue(conn, issue_id, status=status)


def set_assignee(
    conn: sqlite3.Connection, issue_id: int, assignee_id: int | None
) -> dict | None:
    """Assign the issue to a user, or clear it (assignee_id=None -> Unassigned).
    Returns the updated issue, or None if no issue has that id. Checking that
    assignee_id is a real user is the boundary's job; the DB's foreign key is
    the backstop (raises sqlite3.IntegrityError on an unknown non-NULL id)."""
    cur = conn.execute(
        "UPDATE issues SET assignee_id = ? WHERE id = ?", (assignee_id, issue_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def get_issue(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE i.id = ?", (issue_id,)).fetchone()
    return dict(row) if row else None


def list_issues(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"{_SELECT} ORDER BY i.id").fetchall()
    return [dict(row) for row in rows]
