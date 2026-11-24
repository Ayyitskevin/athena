"""Persistent, cooperative check-ins for agent runs.

The append-only activity log proves actions; this table proves only that an agent
credential checked in recently. Fresh/stale is derived from server timestamps at read
time. It is never a terminal state, process-health oracle, or takeover lease.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena import config

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_RECENT_LIMIT = 500

REPORTING_RECENTLY = "reporting_recently"
STALE = "stale"

_SAFE_COLUMNS = "agent_id, run_id, first_seen_at, last_seen_at"


def upsert_checkin(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    run_id: str,
    token_id: int,
) -> None:
    """Create or refresh one check-in without committing its owner's transaction."""
    conn.execute(
        "INSERT INTO agent_run_checkins "
        "(agent_id, run_id, last_token_id) VALUES (?, ?, ?) "
        "ON CONFLICT(agent_id, run_id) DO UPDATE SET "
        "last_seen_at = datetime('now'), last_token_id = excluded.last_token_id",
        (agent_id, run_id, token_id),
    )


def get_checkin(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    run_id: str,
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Return one safe check-in projection, excluding credential metadata."""
    row = conn.execute(
        f"SELECT {_SAFE_COLUMNS} FROM agent_run_checkins "
        "WHERE agent_id = ? AND run_id = ?",
        (agent_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return _with_reporting_state(dict(row), stale_seconds=stale_seconds, now=now)


def list_recent_checkins(
    conn: sqlite3.Connection,
    *,
    agent_id: int | None = None,
    limit: int = 100,
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return bounded newest-first admin projections, optionally for one agent.

    The list includes agent display identity for the admin cockpit. It never exposes
    ``last_token_id`` or any other credential metadata.
    """
    bounded_limit = _bounded_limit(limit)
    clauses = ["u.is_agent = 1"]
    params: list[int] = []
    if agent_id is not None:
        clauses.append("c.agent_id = ?")
        params.append(int(agent_id))
    params.append(bounded_limit)
    rows = conn.execute(
        "SELECT c.agent_id, u.name AS agent_name, u.email AS agent_email, "
        "c.run_id, c.first_seen_at, c.last_seen_at "
        "FROM agent_run_checkins c JOIN users u ON u.id = c.agent_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY c.last_seen_at DESC, c.agent_id, c.run_id LIMIT ?",
        params,
    ).fetchall()
    return [
        _with_reporting_state(dict(row), stale_seconds=stale_seconds, now=now)
        for row in rows
    ]


def list_latest_checkins(
    conn: sqlite3.Connection,
    *,
    agent_id: int | None = None,
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return exactly one newest safe check-in projection per reporting agent.

    Mission-control headline state belongs to an agent's latest report, not every
    retained historical run row. The correlated lookup considers the agent's full
    check-in history (not the bounded recent-history window) and breaks timestamp
    ties by run id so the projection is deterministic.
    """
    clauses = ["u.is_agent = 1"]
    params: list[int] = []
    if agent_id is not None:
        clauses.append("u.id = ?")
        params.append(int(agent_id))
    rows = conn.execute(
        "SELECT c.agent_id, u.name AS agent_name, u.email AS agent_email, "
        "c.run_id, c.first_seen_at, c.last_seen_at "
        "FROM users u JOIN agent_run_checkins c ON c.agent_id = u.id "
        "AND c.run_id = ("
        "SELECT newest.run_id FROM agent_run_checkins newest "
        "WHERE newest.agent_id = u.id "
        "ORDER BY newest.last_seen_at DESC, newest.run_id ASC LIMIT 1"
        ") "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY c.last_seen_at DESC, c.agent_id, c.run_id",
        params,
    ).fetchall()
    return [
        _with_reporting_state(dict(row), stale_seconds=stale_seconds, now=now)
        for row in rows
    ]


def list_checkins_for_runs(
    conn: sqlite3.Connection,
    *,
    run_ids: list[str],
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, list[dict]]:
    """The latest check-in projection for each requested run id, grouped by run.

    Returns only runs that actually have a check-in. This is the narrative's
    check-in lane: it reads the same table as the admin cockpit but scoped to the
    run ids an issue's trail carries, so a run story can say "this run last
    reported X seconds ago" without inventing a second check-in source."""
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        "SELECT c.agent_id, u.name AS agent_name, c.run_id, "
        "c.first_seen_at, c.last_seen_at "
        "FROM agent_run_checkins c JOIN users u ON u.id = c.agent_id "
        f"WHERE c.run_id IN ({placeholders}) "
        "ORDER BY c.run_id, c.last_seen_at DESC, c.agent_id",
        run_ids,
    ).fetchall()
    by_run: dict[str, list[dict]] = {}
    for row in rows:
        run_id = str(row["run_id"])
        by_run.setdefault(run_id, []).append(
            _with_reporting_state(dict(row), stale_seconds=stale_seconds, now=now)
        )
    return by_run


def checkin_agent_ids(conn: sqlite3.Connection, run_id: str) -> list[int]:
    """Every agent id that has checked in under one run id, ascending.

    Check-ins are keyed (agent_id, run_id), so two agents can legitimately report
    the same run id string; callers that need ONE owner (run controls) treat more
    than one row here — absent an authoritative run binding — as ambiguity to
    refuse, never a tie to break."""
    rows = conn.execute(
        "SELECT DISTINCT agent_id FROM agent_run_checkins "
        "WHERE run_id = ? ORDER BY agent_id",
        (run_id,),
    ).fetchall()
    return [int(row["agent_id"]) for row in rows]


def heartbeat_overdue(
    *,
    claimed_at: str,
    last_seen_at: str | None,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> bool:
    """Whether a tagged claim's heartbeat is overdue (U7).

    True once more than the stale threshold has passed since the possession was
    acquired (``claimed_at``: the possession's ``claimed`` event, which a renewal
    does not move) or since the run's last check-in, whichever is later. A claim
    taken a moment ago has not had time to check in, and an old check-in under
    the same run does not make a new possession overdue. A renewal is not an
    acquisition: a holder that keeps renewing and never checks in becomes overdue
    all the same.

    This is a separate fact from a check-in's ``reporting_state``, which measures
    only the check-in's age. They use the same threshold, so for a possession
    acquired before its run's last check-in they agree; for a fresh possession
    under a run whose last check-in is old, ``reporting_state`` reads ``stale``
    while the heartbeat is not yet overdue.
    """
    started = claimed_at if last_seen_at is None else max(claimed_at, last_seen_at)
    since = datetime.strptime(started, _TS_FORMAT).replace(tzinfo=UTC)
    elapsed = (_utc_now(now) - since).total_seconds()
    return elapsed > _stale_after_seconds(stale_seconds)


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be positive")
    return min(limit, _MAX_RECENT_LIMIT)


def _stale_after_seconds(value: int | None) -> int:
    resolved = config.AGENT_RUN_STALE_SECONDS if value is None else value
    if isinstance(resolved, bool) or not isinstance(resolved, int):
        raise ValueError("stale_seconds must be an integer")
    if resolved < 1:
        raise ValueError("stale_seconds must be positive")
    return resolved


def _utc_now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _with_reporting_state(
    row: dict,
    *,
    stale_seconds: int | None,
    now: datetime | None,
) -> dict:
    last_seen = datetime.strptime(row["last_seen_at"], _TS_FORMAT).replace(tzinfo=UTC)
    elapsed_seconds = max(0.0, (_utc_now(now) - last_seen).total_seconds())
    age_seconds = int(elapsed_seconds)
    state = (
        REPORTING_RECENTLY
        if elapsed_seconds <= _stale_after_seconds(stale_seconds)
        else STALE
    )
    return {**row, "age_seconds": age_seconds, "reporting_state": state}
