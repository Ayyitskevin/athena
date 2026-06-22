"""Data access for issue comments.

All comment SQL lives here, mirroring aegis/issues.py. A comment is append-only:
it belongs to one issue and one author, and listing joins the author's name so
the UI can show "who said it" without a second lookup.
"""
from __future__ import annotations

import sqlite3

# Every read returns the author's display name alongside the row, so callers
# never have to resolve author_id -> name themselves.
_SELECT = (
    "SELECT c.*, u.name AS author_name "
    "FROM comments c JOIN users u ON u.id = c.author_id"
)


def add_comment(
    conn: sqlite3.Connection, *, issue_id: int, author_id: int, body: str
) -> dict:
    """Append a comment and return it. Raises sqlite3.IntegrityError if the
    issue or author doesn't exist (the foreign keys refuse the orphan)."""
    cur = conn.execute(
        "INSERT INTO comments (issue_id, author_id, body) VALUES (?, ?, ?)",
        (issue_id, author_id, body),
    )
    conn.commit()
    return get_comment(conn, cur.lastrowid)


def get_comment(conn: sqlite3.Connection, comment_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE c.id = ?", (comment_id,)).fetchone()
    return dict(row) if row else None


def list_comments(conn: sqlite3.Connection, issue_id: int) -> list[dict]:
    """An issue's comments, oldest first (id order = chronological)."""
    rows = conn.execute(
        f"{_SELECT} WHERE c.issue_id = ? ORDER BY c.id", (issue_id,)
    ).fetchall()
    return [dict(row) for row in rows]
