"""Data access for page comments — the discussion thread on a Mentor page.

The page twin of aegis/comments.py: a comment is append-only, belongs to one page
and one author, and listing joins the author's name so the UI can show "who said
it" without a second lookup. Author-ownership (only the author may edit/delete) is
enforced at the boundary, not here — this module only writes.
"""
from __future__ import annotations

import sqlite3

# Every read returns the author's display name alongside the row, so callers
# never have to resolve author_id -> name themselves.
_SELECT = (
    "SELECT c.*, u.name AS author_name "
    "FROM page_comments c JOIN users u ON u.id = c.author_id"
)


def add_comment(
    conn: sqlite3.Connection, *, page_id: int, author_id: int, body: str
) -> dict:
    """Append a comment and return it. Raises sqlite3.IntegrityError if the page
    or author doesn't exist (the foreign keys refuse the orphan)."""
    cur = conn.execute(
        "INSERT INTO page_comments (page_id, author_id, body) VALUES (?, ?, ?)",
        (page_id, author_id, body),
    )
    conn.commit()
    return get_comment(conn, cur.lastrowid)


def get_comment(conn: sqlite3.Connection, comment_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE c.id = ?", (comment_id,)).fetchone()
    return dict(row) if row else None


def update_comment(
    conn: sqlite3.Connection, comment_id: int, *, body: str
) -> dict | None:
    """Replace a comment's body and return it, or None if no comment has that id.
    Author-ownership (only the author may edit) is enforced at the boundary, not
    here — this function just writes."""
    cur = conn.execute(
        "UPDATE page_comments SET body = ? WHERE id = ?", (body, comment_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_comment(conn, comment_id)


def delete_comment(conn: sqlite3.Connection, comment_id: int) -> bool:
    """Delete a comment. Returns True if a row was removed, False if no comment
    had that id (so the caller can answer 404)."""
    cur = conn.execute("DELETE FROM page_comments WHERE id = ?", (comment_id,))
    conn.commit()
    return cur.rowcount > 0


def list_comments(conn: sqlite3.Connection, page_id: int) -> list[dict]:
    """A page's comments, oldest first (id order = chronological)."""
    rows = conn.execute(
        f"{_SELECT} WHERE c.page_id = ? ORDER BY c.id", (page_id,)
    ).fetchall()
    return [dict(row) for row in rows]
