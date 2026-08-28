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
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
from typing import cast

from athena.core import access, links

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
#
# Owned here because notifying is what a mention is FOR. `web/render.py` imports
# this exact object rather than keeping its own copy — the two were identical
# literals under a comment asserting they were shared, which is a claim source code
# cannot keep on its own. Digit-bounded via links.ID_DIGITS for the reason
# documented there: an unbounded run made int() raise on text any author can type.
MENTION_RE = re.compile(rf"\[\[user:({links.ID_DIGITS})\]\]")


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
    for match in MENTION_RE.finditer(text):
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


# --- priority / mute / digest projections -----------------------------------

# Notification preferences use the same four explicit values as Aegis issue
# priority. ``normal`` is projection-only: it means no preference and no target
# priority. tests/test_notification_priority.py guards the shared values against
# drift without making core depend on Aegis.
PRIORITY_ORDER = ("low", "normal", "medium", "high", "urgent")
PREFERENCE_PRIORITIES = ("low", "medium", "high", "urgent")

# Closed ranks for filtering and sorting. Stored unknowns are handled by
# ``_resolve_priority`` and conservatively surface as urgent.
_PRIORITY_RANK = {name: idx for idx, name in enumerate(PRIORITY_ORDER)}

# Bounds for digest windows: a window smaller than 1 minute is meaningless and
# larger than one week hides signal. Values outside this range are rejected.
MIN_DIGEST_MINUTES = 1
MAX_DIGEST_MINUTES = 7 * 24 * 60
MAX_MUTE_UNTIL_CHARS = 64

# SQLite stores datetimes as 'YYYY-MM-DD HH:MM:SS' in UTC. We accept ISO-8601
# inputs (with or without timezone) and write them back in the storage format.
_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

PriorityResolver = Callable[
    [sqlite3.Connection, set[tuple[str, int]]],
    Mapping[tuple[str, int], str | None],
]


class WatchNotFound(LookupError):
    """A preference write named a target the actor is not watching."""


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601-ish timestamp into an offset-aware UTC datetime.
    Returns None for missing/malformed values so callers fail closed (no
    suppression) rather than silently drop."""
    if not value:
        return None
    # Python 3.11+ handles both 'Z' and offset-aware ISO strings via fromisoformat.
    # Older forms may arrive with a space instead of 'T'.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(dt: datetime) -> str:
    """Store a datetime in the canonical SQLite format."""
    return dt.astimezone(timezone.utc).strftime(_SQLITE_TS_FORMAT)


def _now() -> datetime:
    """Current UTC time — isolated for tests."""
    return datetime.now(timezone.utc)


# --- preferences ------------------------------------------------------------


def set_preference(
    conn: sqlite3.Connection,
    user_id: int,
    target_kind: str,
    target_id: int,
    *,
    priority: str | None = None,
    mute_until: str | None = None,
    digest_window_minutes: int | None = None,
) -> dict:
    """Create or replace per-watch notification preferences for a user.

    ``priority`` overrides the target's own priority (e.g. issue.priority) when
    resolving this watch. ``mute_until`` is an ISO-8601 datetime; notifications
    whose event_at is before it are suppressed. ``digest_window_minutes`` groups
    notifications into buckets of that length.

    Validation is boundary-strict: unknown priorities are normalized, malformed
    mute_until is rejected by raising ValueError, and digest windows outside
    [1, 10080] are rejected. The caller (the API layer) translates these into
    422 responses.
    """
    if priority is not None and priority not in PREFERENCE_PRIORITIES:
        raise ValueError(f"priority must be one of: {', '.join(PREFERENCE_PRIORITIES)}")

    parsed_mute: datetime | None = None
    if mute_until is not None:
        if len(mute_until) > MAX_MUTE_UNTIL_CHARS:
            raise ValueError(
                f"mute_until must be at most {MAX_MUTE_UNTIL_CHARS} characters"
            )
        parsed_mute = _parse_iso_timestamp(mute_until)
        if parsed_mute is None:
            raise ValueError("mute_until must be a valid ISO-8601 datetime")

    if digest_window_minutes is not None and not (
        MIN_DIGEST_MINUTES <= digest_window_minutes <= MAX_DIGEST_MINUTES
    ):
        raise ValueError(
            f"digest_window_minutes must be between {MIN_DIGEST_MINUTES} "
            f"and {MAX_DIGEST_MINUTES}"
        )

    cursor = conn.execute(
        """
        INSERT INTO watch_preferences
            (user_id, target_kind, target_id, priority, mute_until,
             digest_window_minutes, updated_at)
        SELECT ?, ?, ?, ?, ?, ?, datetime('now')
        FROM watches
        WHERE user_id = ? AND target_kind = ? AND target_id = ?
        ON CONFLICT (user_id, target_kind, target_id) DO UPDATE SET
            priority = excluded.priority,
            mute_until = excluded.mute_until,
            digest_window_minutes = excluded.digest_window_minutes,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            target_kind,
            target_id,
            priority,
            _format_timestamp(parsed_mute) if parsed_mute else None,
            digest_window_minutes,
            user_id,
            target_kind,
            target_id,
        ),
    )
    if cursor.rowcount == 0:
        raise WatchNotFound
    conn.commit()
    preference = get_preference(conn, user_id, target_kind, target_id)
    if preference is None:  # pragma: no cover - the successful write created it
        raise RuntimeError("preference write succeeded without a readable row")
    return preference


def get_preference(
    conn: sqlite3.Connection, user_id: int, target_kind: str, target_id: int
) -> dict | None:
    """The user's preferences for one watch, or None if none exist."""
    row = conn.execute(
        """
        SELECT user_id, target_kind, target_id, priority, mute_until,
               digest_window_minutes, created_at, updated_at
        FROM watch_preferences
        WHERE user_id = ? AND target_kind = ? AND target_id = ?
        """,
        (user_id, target_kind, target_id),
    ).fetchone()
    return dict(row) if row else None


def delete_preference(
    conn: sqlite3.Connection, user_id: int, target_kind: str, target_id: int
) -> bool:
    """Remove preferences for a watch. Returns True if a row was deleted."""
    cur = conn.execute(
        "DELETE FROM watch_preferences "
        "WHERE user_id = ? AND target_kind = ? AND target_id = ?",
        (user_id, target_kind, target_id),
    )
    conn.commit()
    return cur.rowcount > 0


# --- projection helpers -----------------------------------------------------


def _resolve_priority(
    preference_priority: str | None,
    target_priority: str | None,
) -> tuple[str, str, bool]:
    """Resolve priority and provenance without hiding corrupt input.

    Unknown stored values become urgent. That is deliberately conservative: a
    stale value may be noisy, but it can never fall below a caller's filter and
    silently suppress the notification.
    """
    if preference_priority is not None:
        if preference_priority in PREFERENCE_PRIORITIES:
            return preference_priority, "preference", True
        return "urgent", "invalid_preference", False
    if target_priority is not None:
        if target_priority in PREFERENCE_PRIORITIES:
            return target_priority, "target", True
        return "urgent", "invalid_target", False
    return "normal", "default", True


def _is_muted(mute_until: str | None, now: datetime) -> bool:
    """True if now() is strictly before mute_until. Malformed/unset values are
    treated as not muted — fail closed (let the notification through)."""
    boundary = _parse_iso_timestamp(mute_until)
    if boundary is None:
        return False
    return now < boundary


def _digest_bucket(event_at: str, window_minutes: int) -> str:
    """Bucket an event timestamp into a digest window. The bucket key is the
    window's start time as an ISO-8601 string, so notifications in the same
    window share a key and can be grouped or collapsed by the caller."""
    ts = _parse_iso_timestamp(event_at)
    if ts is None:
        # A malformed event_at lands in its own bucket rather than crashing.
        return event_at
    epoch = ts.replace(tzinfo=timezone.utc)
    window_seconds = window_minutes * 60
    bucket_start = datetime.fromtimestamp(
        (epoch.timestamp() // window_seconds) * window_seconds, tz=timezone.utc
    )
    return bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- priority inbox projection ----------------------------------------------


def list_priority_notifications(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    unread_only: bool = False,
    min_priority: str | None = None,
    include_muted: bool = False,
    digest: bool = False,
    limit: int | None = 50,
    actor: dict | None | object = _UNGATED,
    resolve_priorities: PriorityResolver | None = None,
) -> dict:
    """Read-only priority/mute/digest projection over the user's inbox.

    Returns a dict with ``observed_at`` (stable UTC snapshot time for this read)
    and ``items`` (a list of notification rows annotated with ``priority``,
    ``muted``, ``digest_bucket``, and ``source``).

    - ``priority`` resolves per watch: watch_preferences.priority > issue.priority
      > 'normal'. Non-issue targets use 'normal' unless a preference exists.
    - ``muted`` is True when the watch's mute_until is in the future. By default
      muted items are excluded; pass ``include_muted=True`` to see them.
    - ``digest_bucket`` is present when the watch has a digest window and ``digest``
      is True; otherwise it is None.
    - ``source`` preserves the owning target id/kind and, for issues, the issue's
      own priority and the preference id that shaped this row.

    Visibility gating is applied exactly like ``list_notifications``: events on
    targets the actor can no longer see are dropped."""
    observed_at = _now()
    observed_at_str = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Priority and mute are read-time preferences, so SQL cannot safely limit the
    # inbox before those filters run. Read the complete visible inbox and apply
    # the caller's limit only after projection; a bounded overfetch could silently
    # hide an older urgent item behind newer low-priority or muted rows.
    raw = list_notifications(
        conn, user_id, unread_only=unread_only, limit=-1, actor=actor
    )

    targets = {(row["target_kind"], row["target_id"]) for row in raw}
    target_priorities = (
        dict(resolve_priorities(conn, targets))
        if resolve_priorities and targets
        else {}
    )

    # Load the user's complete (normally tiny) preference set once. Building one
    # OR expression per inbox row can exceed SQLite's expression-depth limit and
    # duplicates work when many notifications share a target.
    if raw:
        pref_rows = conn.execute(
            "SELECT target_kind, target_id, priority, mute_until, "
            "digest_window_minutes FROM watch_preferences "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        preferences = {
            (row["target_kind"], row["target_id"]): dict(row) for row in pref_rows
        }
    else:
        preferences = {}

    if min_priority is not None and min_priority not in PRIORITY_ORDER:
        raise ValueError(f"min_priority must be one of: {', '.join(PRIORITY_ORDER)}")
    min_rank = _PRIORITY_RANK[min_priority] if min_priority is not None else 0

    items: list[dict] = []
    for row in raw:
        target_kind = row["target_kind"]
        target_id = row["target_id"]
        pref = preferences.get((target_kind, target_id), {})

        target_priority = target_priorities.get((target_kind, target_id))
        priority, priority_source, priority_valid = _resolve_priority(
            pref.get("priority"),
            target_priority,
        )
        if _PRIORITY_RANK[priority] < min_rank:
            continue

        mute_until = pref.get("mute_until")
        mute_valid = mute_until is None or _parse_iso_timestamp(mute_until) is not None
        muted = _is_muted(mute_until, observed_at)
        if muted and not include_muted:
            continue

        window = pref.get("digest_window_minutes")
        window_valid = window is None or (
            isinstance(window, int)
            and MIN_DIGEST_MINUTES <= window <= MAX_DIGEST_MINUTES
        )
        valid_window = window if window_valid else None
        delivery_state = "muted" if muted else "digest" if valid_window else "immediate"
        bucket = None
        if digest and isinstance(valid_window, int):
            bucket = _digest_bucket(row["event_at"], valid_window)

        preference_valid = priority_valid and mute_valid and window_valid

        item = {
            **row,
            "observed_at": observed_at_str,
            "priority": priority,
            "muted": muted,
            "delivery_state": delivery_state,
            "digest_bucket": bucket,
            "source": {
                "target_kind": target_kind,
                "target_id": target_id,
                "issue_priority": target_priority if target_kind == "issue" else None,
                "preference_set": bool(pref),
                "preference_valid": preference_valid,
                "priority_source": priority_source,
            },
        }
        items.append(item)
        if limit is not None and len(items) >= limit:
            break

    return {"observed_at": observed_at_str, "items": items}


def priority_summary(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    unread_only: bool = False,
    actor: dict | None | object = _UNGATED,
    resolve_priorities: PriorityResolver | None = None,
) -> dict:
    """Count notifications per resolved priority, with mute state separated.

    Useful for a 'Now' cockpit that tells the operator 'you have 3 urgent unread
    items, 1 of them muted'. Counts are visibility-gated like the inbox."""
    projection = list_priority_notifications(
        conn,
        user_id,
        unread_only=unread_only,
        include_muted=True,
        limit=None,
        actor=actor,
        resolve_priorities=resolve_priorities,
    )
    summary: dict[str, dict[str, int]] = {}
    for item in projection["items"]:
        priority = item["priority"]
        bucket = summary.setdefault(priority, {"total": 0, "muted": 0})
        bucket["total"] += 1
        if item["muted"]:
            bucket["muted"] += 1
    return {"observed_at": projection["observed_at"], "by_priority": summary}
