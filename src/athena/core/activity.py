"""Data access for the activity log: an append-only audit trail.

Every meaningful write in Athena (an issue created, a status changed, an
assignment) records one row here through record(), stamped with WHO did it. This
is the history the architecture promises — "Grok closed AEGIS-88" as a recorded
fact, not a guess. All activity SQL lives here, mirroring aegis/issues.py and
aegis/comments.py.
"""
from __future__ import annotations

import sqlite3

# Every read returns the actor's display name alongside the row, so a feed can
# render "Kevin closed AEGIS-12" without a second lookup.
_SELECT = (
    "SELECT a.*, u.name AS actor_name "
    "FROM activity a JOIN users u ON u.id = a.actor_id"
)


def record(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    verb: str,
    target_kind: str,
    target_id: int,
    detail: str = "",
) -> dict:
    """Append one activity row and return it. Raises sqlite3.IntegrityError if
    actor_id isn't a real user (the foreign key refuses the orphan). Callers pass
    a controlled verb; this layer only writes."""
    cur = conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (actor_id, verb, target_kind, target_id, detail),
    )
    conn.commit()
    return get_activity(conn, cur.lastrowid)


def get_activity(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE a.id = ?", (activity_id,)).fetchone()
    return dict(row) if row else None


def list_activity(
    conn: sqlite3.Connection,
    *,
    target_kind: str | None = None,
    target_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Activity newest first. With target_kind+target_id, just that target's
    history (one issue's timeline); without, the global feed across everything."""
    where = ""
    params: list = []
    if target_kind is not None and target_id is not None:
        where = " WHERE a.target_kind = ? AND a.target_id = ?"
        params += [target_kind, target_id]
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id DESC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]
