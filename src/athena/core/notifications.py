"""Watching + the per-user inbox — the human twin of webhooks.

When something happens to a target a user watches, they get an inbox entry that
points at the activity event which caused it. This module owns:

  * watches (who follows what) — manual, plus auto-watch from the activity
    recorders (a creator/commenter/assignee starts watching);
  * the fan-out: notify_watchers() turns one event into inbox rows for that
    target's watchers — called once from core.activity.record, so EVERY recorded
    event feeds the inbox, with no per-call-site wiring;
  * inbox reads (list, unread count) and state (mark read).

It deliberately imports no other module (not even activity): notify_watchers is
handed the event id, and the inbox read JOINs the activity table in SQL. So
core.activity can call into here without an import cycle.
"""

from __future__ import annotations

import sqlite3

# Targets a user can watch. A watch on anything else is meaningless; the boundary
# rejects unknown kinds.
WATCHABLE_KINDS = ("issue", "page")


# --- watches ----------------------------------------------------------------


def watch(
    conn: sqlite3.Connection, user_id: int, target_kind: str, target_id: int
) -> None:
    """Start watching a target (idempotent — watching twice is a no-op)."""
    conn.execute(
        "INSERT OR IGNORE INTO watches (user_id, target_kind, target_id) "
        "VALUES (?, ?, ?)",
        (user_id, target_kind, target_id),
    )
    conn.commit()


def unwatch(
    conn: sqlite3.Connection, user_id: int, target_kind: str, target_id: int
) -> bool:
    """Stop watching. Returns False if the user wasn't watching it."""
    cur = conn.execute(
        "DELETE FROM watches WHERE user_id = ? AND target_kind = ? AND target_id = ?",
        (user_id, target_kind, target_id),
    )
    conn.commit()
    return cur.rowcount > 0


def is_watching(
    conn: sqlite3.Connection, user_id: int, target_kind: str, target_id: int
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM watches "
            "WHERE user_id = ? AND target_kind = ? AND target_id = ?",
            (user_id, target_kind, target_id),
        ).fetchone()
        is not None
    )


# --- fan-out (called from core.activity.record) -----------------------------


def notify_watchers(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    actor_id: int,
    target_kind: str,
    target_id: int,
) -> None:
    """Create an inbox row for every watcher of this target EXCEPT the actor (you
    don't get notified of your own action). Does NOT commit — the caller
    (activity.record) commits the event and its notifications together. The UNIQUE
    (user_id, event_id) makes a re-run harmless."""
    watchers = conn.execute(
        "SELECT user_id FROM watches "
        "WHERE target_kind = ? AND target_id = ? AND user_id != ?",
        (target_kind, target_id, actor_id),
    ).fetchall()
    for row in watchers:
        conn.execute(
            "INSERT OR IGNORE INTO notifications (user_id, event_id) VALUES (?, ?)",
            (row["user_id"], event_id),
        )


# --- inbox reads + state ----------------------------------------------------

# Each inbox row carries the underlying event's fields (so the inbox renders the
# same way the activity feed does) plus the notification's own id/read state.
_INBOX_SELECT = (
    "SELECT n.id, n.event_id, n.read_at, n.created_at, "
    "a.actor_id, au.name AS actor_name, a.verb, a.target_kind, a.target_id, "
    "a.detail, a.created_at AS event_at "
    "FROM notifications n "
    "JOIN activity a ON a.id = n.event_id "
    "JOIN users au ON au.id = a.actor_id"
)


def list_notifications(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """A user's inbox, newest first. unread_only narrows to the unread ones."""
    where = "WHERE n.user_id = ?"
    params: list = [user_id]
    if unread_only:
        where += " AND n.read_at IS NULL"
    rows = conn.execute(
        f"{_INBOX_SELECT} {where} ORDER BY n.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def unread_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM notifications "
        "WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    ).fetchone()["n"]


def mark_read(conn: sqlite3.Connection, user_id: int, notification_id: int) -> bool:
    """Mark one notification read. Scoped to the owner, so a user can't touch
    another's inbox. Returns False if no such unread notification of theirs."""
    cur = conn.execute(
        "UPDATE notifications SET read_at = datetime('now') "
        "WHERE id = ? AND user_id = ? AND read_at IS NULL",
        (notification_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_all_read(conn: sqlite3.Connection, user_id: int) -> int:
    """Mark every unread notification read; returns how many were cleared."""
    cur = conn.execute(
        "UPDATE notifications SET read_at = datetime('now') "
        "WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    )
    conn.commit()
    return cur.rowcount
