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

import re
import sqlite3
from typing import cast

from athena.core import access

# Targets a user can watch. A watch on anything else is meaningless; the boundary
# rejects unknown kinds. 'space' is the SHARED-MEMORY subscription: it covers the
# space's own lifecycle events AND every event on a page inside it (see
# notify_watchers) — the "the handbook changed" habit, for a fleet that treats a
# space as its collective notebook.
WATCHABLE_KINDS = ("issue", "page", "space")

# Sentinel for the inbox reads' `actor`: "no visibility gating" (internal callers /
# tests). Distinct from actor=None. Notifications are created without an access check
# (a watch or a mention can land on a target that later goes private), so the inbox
# reads gate at READ time: a notification whose target the owner can no longer see is
# filtered out here rather than deleted.
_UNGATED = object()

# A mention is [[user:N]] in body/comment text — the same bracket grammar as the
# [[issue:N]]/[[page:N]] cross-links, but for people. The links/backlinks system
# only indexes issue/page kinds, so this token is invisible to it; it exists only
# to notify the named user.
_MENTION_RE = re.compile(r"\[\[user:(\d+)\]\]")


# --- watches ----------------------------------------------------------------


def watch(
    conn: sqlite3.Connection,
    user_id: int,
    target_kind: str,
    target_id: int,
    *,
    commit: bool = True,
) -> None:
    """Start watching a target (idempotent — watching twice is a no-op).

    Commands pass ``commit=False`` when the watch is part of an audited write.
    """
    conn.execute(
        "INSERT OR IGNORE INTO watches (user_id, target_kind, target_id) "
        "VALUES (?, ?, ?)",
        (user_id, target_kind, target_id),
    )
    if commit:
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


def delete_watches_for(
    conn: sqlite3.Connection, target_kind: str, target_id: int
) -> int:
    """Delete every watch ON a target and return how many were removed. Does NOT
    commit — it is meant to run INSIDE the owning delete's transaction so the watches
    vanish atomically with the target. Called when a page is deleted: a watch keys
    its target polymorphically (no foreign key to lean on), so without this the row
    dangles, pointing at a target that no longer exists. Activity-target
    reservations prevent numeric-id reuse as a second defense, but deleting the
    subscription is still the only honest lifecycle. Notifications stay intact:
    they reference the append-only activity log, which outlives the target, so they
    never dangle — deleting them would erase valid inbox history."""
    cur = conn.execute(
        "DELETE FROM watches WHERE target_kind = ? AND target_id = ?",
        (target_kind, target_id),
    )
    return cur.rowcount


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
    (user_id, event_id) makes a re-run harmless.

    Two passes, one rule: a watch reaches an event if the event's target IS the
    watched thing, or is a page INSIDE a watched space. The space pass is the only
    indirect fan-out in the system, and it lives here — inside the single fan-out
    owner — rather than as a second call site, so there stays exactly one place an
    event becomes inbox rows. It costs one indexed lookup per page event.

    Reading the `pages` table from core is the same read-only borrow `core.access`
    already makes of `spaces`/`pages` for visibility: mentor stays the only WRITER of
    those rows. Resolving the space here (rather than having the caller pass it) is
    what keeps activity.record's signature honest — it knows about targets, not about
    which module owns a container.

    A user watching both the page and its space gets ONE notification, not two: the
    UNIQUE (user_id, event_id) collapses the passes. Notifications are written
    ungated — the inbox reads gate visibility (see list_notifications), so a watcher
    who cannot see the space never renders what landed."""
    recipients = [
        row["user_id"]
        for row in conn.execute(
            "SELECT user_id FROM watches "
            "WHERE target_kind = ? AND target_id = ? AND user_id != ?",
            (target_kind, target_id, actor_id),
        )
    ]
    if target_kind == "page":
        recipients.extend(
            row["user_id"]
            for row in conn.execute(
                "SELECT w.user_id FROM watches w "
                "JOIN pages p ON p.space_id = w.target_id "
                "WHERE w.target_kind = 'space' AND p.id = ? AND w.user_id != ?",
                (target_id, actor_id),
            )
        )
    for user_id in recipients:
        conn.execute(
            "INSERT OR IGNORE INTO notifications (user_id, event_id) VALUES (?, ?)",
            (user_id, event_id),
        )


# --- mentions ---------------------------------------------------------------


def parse_mentions(text: str | None) -> list[int]:
    """The distinct user ids named by [[user:N]] in text, in first-seen order.
    Pure text parsing — it doesn't check the ids are real users (that's the
    caller's job at write time)."""
    if not text:
        return []
    seen: list[int] = []
    for match in _MENTION_RE.finditer(text):
        uid = int(match.group(1))
        if uid not in seen:
            seen.append(uid)
    return seen


def process_mentions(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    actor_id: int,
    text: str | None,
    commit: bool = True,
) -> None:
    """Notify every real user named by [[user:N]] in `text` (except the actor) about
    this event, and start them watching its target so they follow the thread.

    The mention notification is created HERE rather than via notify_watchers: at the
    moment the event was recorded the mentioned user wasn't watching yet, so the
    generic fan-out missed them. The UNIQUE (user_id, event_id) makes this safe even
    if they were already a watcher (no double notification)."""
    uids = parse_mentions(text)
    if not uids:
        return
    event = conn.execute(
        "SELECT target_kind, target_id FROM activity WHERE id = ?", (event_id,)
    ).fetchone()
    if event is None:
        return
    for uid in uids:
        if uid == actor_id:
            continue  # mentioning yourself isn't a notification
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
            continue  # a mention of a non-existent user is just text
        conn.execute(
            "INSERT OR IGNORE INTO notifications (user_id, event_id) VALUES (?, ?)",
            (uid, event_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO watches (user_id, target_kind, target_id) "
            "VALUES (?, ?, ?)",
            (uid, event["target_kind"], event["target_id"]),
        )
    if commit:
        conn.commit()


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
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """A user's inbox, newest first. unread_only narrows to the unread ones. `actor`
    (the inbox owner viewing it) gates out notifications whose target they can no
    longer see; _UNGATED leaves it ungated for internal callers/tests."""
    where = "WHERE n.user_id = ?"
    params: list = [user_id]
    if unread_only:
        where += " AND n.read_at IS NULL"
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            where += f" AND {gate}"
            params.extend(gate_params)
    rows = conn.execute(
        f"{_INBOX_SELECT} {where} ORDER BY n.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def unread_count(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    actor: dict | None | object = _UNGATED,
) -> int:
    """How many unread notifications the user has — the nav badge. Gated like the
    inbox itself (an unread notification on a target the owner can't see doesn't count)
    by joining the activity row; _UNGATED skips the join and counts them all."""
    if actor is _UNGATED:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM notifications "
            "WHERE user_id = ? AND read_at IS NULL",
            (user_id,),
        ).fetchone()["n"]
    gate, gate_params = access.event_visibility_clause(
        conn, cast(dict | None, actor), alias="a"
    )
    where = "WHERE n.user_id = ? AND n.read_at IS NULL"
    params: list = [user_id]
    if gate:
        where += f" AND {gate}"
        params.extend(gate_params)
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM notifications n "
        f"JOIN activity a ON a.id = n.event_id {where}",
        params,
    ).fetchone()["n"]


def mark_read(
    conn: sqlite3.Connection,
    user_id: int,
    notification_id: int,
    *,
    actor: dict | None | object = _UNGATED,
) -> bool:
    """Mark one visible notification read; hidden rows remain untouched."""
    where = "WHERE id = ? AND user_id = ? AND read_at IS NULL"
    params: list = [notification_id, user_id]
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            where += f" AND event_id IN (SELECT a.id FROM activity a WHERE {gate})"
            params.extend(gate_params)
    cur = conn.execute(
        f"UPDATE notifications SET read_at = datetime('now') {where}",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


def mark_all_read(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    actor: dict | None | object = _UNGATED,
) -> int:
    """Mark every visible unread notification read and return that exact count."""
    where = "WHERE user_id = ? AND read_at IS NULL"
    params: list = [user_id]
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            where += f" AND event_id IN (SELECT a.id FROM activity a WHERE {gate})"
            params.extend(gate_params)
    cur = conn.execute(
        f"UPDATE notifications SET read_at = datetime('now') {where}",
        params,
    )
    conn.commit()
    return cur.rowcount
