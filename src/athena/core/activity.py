"""Data access for the activity log: an append-only audit trail.

Every meaningful write in Athena (an issue created, a status changed, an
assignment) records one row here through record(), stamped with WHO did it. This
is the history the architecture promises — "Grok closed AEGIS-88" as a recorded
fact, not a guess. All activity SQL lives here, mirroring aegis/issues.py and
aegis/comments.py.
"""

from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO
import sqlite3

from athena.core import notifications

# Every read returns the actor's display name alongside the row, so a feed can
# render "Kevin closed AEGIS-12" without a second lookup.
_SELECT = (
    "SELECT a.*, u.name AS actor_name FROM activity a JOIN users u ON u.id = a.actor_id"
)


def _like_pattern(value: str) -> str:
    """Return a literal LIKE pattern for operator search text."""
    escaped = (
        value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


_CSV_FIELDS = [
    "id",
    "created_at",
    "actor_id",
    "actor_name",
    "verb",
    "target_kind",
    "target_id",
    "detail",
]


def to_csv(rows: list[dict]) -> str:
    """Serialize activity rows to a stable operator-export CSV."""
    out = StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=_CSV_FIELDS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


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
    # Fan the event out to the inbox of anyone watching this target (not the actor).
    # notify_watchers doesn't commit, so the event row and its notifications land in
    # one commit — they appear together or not at all.
    notifications.notify_watchers(
        conn,
        event_id=cur.lastrowid,
        actor_id=actor_id,
        target_kind=target_kind,
        target_id=target_id,
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
    actor_id: int | None = None,
    actor_is_agent: bool | None = None,
    verb: str | None = None,
    search: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Activity newest first. Every filter is optional and independent: pass
    target_kind+target_id for one target's timeline, target_kind alone to scope
    the global feed to a kind, actor_id/verb/search to narrow who/what. actor_is_agent
    is the actor-type lens — True for agents only, False for humans only — answering
    "what did the agents do?" distinctly from human activity. before_id is the paging
    cursor — only rows older than it (a.id < before_id), so the caller can walk back
    through history one page at a time on a stable, append-only ordering."""
    clauses: list[str] = []
    params: list = []
    if target_kind is not None:
        clauses.append("a.target_kind = ?")
        params.append(target_kind)
    if target_id is not None:
        clauses.append("a.target_id = ?")
        params.append(target_id)
    if actor_id is not None:
        clauses.append("a.actor_id = ?")
        params.append(actor_id)
    if actor_is_agent is not None:
        # u is already joined for actor_name, so the lens is a cheap predicate on it.
        clauses.append("u.is_agent = ?")
        params.append(1 if actor_is_agent else 0)
    if verb is not None:
        clauses.append("a.verb = ?")
        params.append(verb)
    if search is not None and search.strip():
        pattern = _like_pattern(search)
        clauses.append(
            "("
            "u.name LIKE ? ESCAPE '\\' OR "
            "a.verb LIKE ? ESCAPE '\\' OR "
            "a.target_kind LIKE ? ESCAPE '\\' OR "
            "CAST(a.target_id AS TEXT) LIKE ? ESCAPE '\\' OR "
            "a.detail LIKE ? ESCAPE '\\' OR "
            "a.created_at LIKE ? ESCAPE '\\' OR "
            "(a.target_kind || ' #' || a.target_id) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern] * 7)
    if before_id is not None:
        clauses.append("a.id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id DESC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def list_events(
    conn: sqlite3.Connection,
    *,
    after_id: int | None = None,
    target_kind: str | None = None,
    target_id: int | None = None,
    actor_id: int | None = None,
    actor_is_agent: bool | None = None,
    verb: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Events in FORWARD (oldest-first) order for cursor consumption — the agent/
    integration view of the same append-only trail `list_activity` serves to humans.

    The audit log IS the event log: every recorded action already has a monotonic
    id, an actor, a verb, and a target. This inverts list_activity's cursor so a
    consumer can resume exactly where it left off: only rows with a.id > after_id,
    ordered ASC, so processing them in order and remembering the last id seen is a
    complete, gap-free subscription. (list_activity pages BACKWARD with before_id /
    DESC for a human scrolling recent history; an agent draining a stream wants the
    opposite.) Filters mirror list_activity so a consumer can narrow to one kind,
    one target, one actor, one verb, or one actor type (actor_is_agent: True for
    agents only, False for humans only) — so an integration can subscribe to just
    the agents' stream, or just the humans'."""
    clauses: list[str] = []
    params: list = []
    if after_id is not None:
        clauses.append("a.id > ?")
        params.append(after_id)
    if target_kind is not None:
        clauses.append("a.target_kind = ?")
        params.append(target_kind)
    if target_id is not None:
        clauses.append("a.target_id = ?")
        params.append(target_id)
    if actor_id is not None:
        clauses.append("a.actor_id = ?")
        params.append(actor_id)
    if actor_is_agent is not None:
        # u is already joined for actor_name, so the lens is a cheap predicate on it.
        clauses.append("u.is_agent = ?")
        params.append(1 if actor_is_agent else 0)
    if verb is not None:
        clauses.append("a.verb = ?")
        params.append(verb)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id ASC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(value: str) -> datetime | None:
    """Parse the trail's stored timestamp ('YYYY-MM-DD HH:MM:SS', as datetime('now')
    writes it). Returns None on anything that doesn't match — a malformed stamp must
    not crash run reconstruction; it just can't contribute a gap."""
    try:
        return datetime.strptime(value, _TS_FORMAT)
    except (ValueError, TypeError):
        return None


def _run_summary(events: list[dict]) -> dict:
    """Wrap a contiguous group of one actor's events as a run: its span, its size,
    and the events themselves (oldest-first) so a caller can replay the sequence."""
    first, last = events[0], events[-1]
    return {
        "actor_id": first["actor_id"],
        "actor_name": first["actor_name"],
        "started_at": first["created_at"],
        "ended_at": last["created_at"],
        "first_id": first["id"],
        "last_id": last["id"],
        "event_count": len(events),
        "events": events,
    }


def reconstruct_runs(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    gap_seconds: int = 1800,
    limit: int = 200,
) -> list[dict]:
    """Reconstruct one actor's recent activity into RUNS — a reading lens over the
    append-only log, NOT a stored concept. A run is a maximal sequence of that
    actor's events with no gap longer than gap_seconds between consecutive events:
    the work the actor did in one sitting before going quiet. This is the first step
    toward replaying "what did this agent do" as discrete sessions, derived from the
    audit trail we already keep — no run id is recorded (yet), so the boundary is the
    gap, not a persisted marker.

    Reconstructed from the actor's most recent `limit` events, newest run first;
    WITHIN a run, events stay oldest-first so the run reads in the order it happened.
    Because list_activity already scopes to this actor, other actors' events neither
    appear in a run nor split one — a run is one actor's uninterrupted stretch of
    work, regardless of what anyone else did in between."""
    # Pull the actor's most recent events (newest-first), then walk them oldest-first
    # so each gap compares an event to the one immediately before it in real time.
    ascending = list(reversed(list_activity(conn, actor_id=actor_id, limit=limit)))

    runs: list[dict] = []
    current: list[dict] = []
    prev_ts: datetime | None = None
    for event in ascending:
        ts = _parse_ts(event["created_at"])
        if (
            current
            and prev_ts is not None
            and ts is not None
            and (ts - prev_ts).total_seconds() > gap_seconds
        ):
            runs.append(_run_summary(current))
            current = []
        current.append(event)
        prev_ts = ts
    if current:
        runs.append(_run_summary(current))
    # Newest run first, matching how the feeds present recent activity.
    runs.reverse()
    return runs


def distinct_verbs(conn: sqlite3.Connection) -> list[str]:
    """The verbs that actually occur in the trail, alphabetical. Powers the feed's
    verb filter from real data — never a hardcoded list that could drift from what
    the recorders emit."""
    rows = conn.execute("SELECT DISTINCT verb FROM activity ORDER BY verb").fetchall()
    return [row["verb"] for row in rows]


def distinct_target_kinds(conn: sqlite3.Connection) -> list[str]:
    """The target kinds that actually occur in the trail, alphabetical. Same
    honesty rule as distinct_verbs — only kinds something has recorded against."""
    rows = conn.execute(
        "SELECT DISTINCT target_kind FROM activity ORDER BY target_kind"
    ).fetchall()
    return [row["target_kind"] for row in rows]
