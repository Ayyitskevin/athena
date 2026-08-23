"""The per-reader cursor table: where one reader has drained the trail to.

Core owns the table because the position is about the ACTIVITY TRAIL, which is
core's. The desk that reads it composes work from both modules and therefore
lives in aegis (`aegis/desk.py`) — core may not import aegis.

PERSONAL STATE, NOT HISTORY: a read receipt records nothing about the fleet and
emits no activity event (COMMAND_MIGRATION.md's personal-state category and its
criteria). Advance-only is enforced here by the command that calls this and
again by the 0073 trigger.
"""

from __future__ import annotations

import sqlite3

#: The only cursor name v1 knows. Kept beside the migration's CHECK so the two
#: cannot drift apart silently.
DESK_CURSOR = "desk"


def get_cursor(conn: sqlite3.Connection, *, user_id: int, name: str) -> dict | None:
    """One reader's stored position, or None when they have never acknowledged.

    None is a distinct fact from zero: "has never read" is not "has read
    nothing new", and the desk keeps them apart.
    """
    row = conn.execute(
        "SELECT user_id, name, after_id, updated_at FROM agent_cursors "
        "WHERE user_id = ? AND name = ?",
        (user_id, name),
    ).fetchone()
    return None if row is None else dict(row)


def set_cursor(
    conn: sqlite3.Connection, *, user_id: int, name: str, after_id: int
) -> dict:
    """Move a reader's position forward. Does not commit; the caller owns the
    transaction so the read-then-write stays atomic.

    Advance-only is enforced by the 0073 trigger as well as by the command that
    calls this, because a position that could move backward would let a reader
    unsay an acknowledgement.
    """
    conn.execute(
        "INSERT INTO agent_cursors (user_id, name, after_id) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET "
        "after_id = excluded.after_id, updated_at = datetime('now')",
        (user_id, name, after_id),
    )
    current = get_cursor(conn, user_id=user_id, name=name)
    assert current is not None
    return current
