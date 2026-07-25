"""The worker registry: where an agent says it runs, and one thing you can say back.

Athena models WHO an agent is — users, tokens, scopes — and check-ins
(`agent_run_checkins`) prove a credential reported a RUN recently. Neither answers
"which of my agent processes are up, on what box, and can I tell one to stop?"

A worker is a row that heartbeats. There is no cluster, no scheduler, and no
leader election (steering rule 4): this stores what a worker reports about itself,
plus one instruction the operator may leave for it.

**Two words this module refuses to say.** *Alive* — a heartbeat proves a worker
REPORTED at a moment, never that its process exists now, so presence is derived
from the server clock at read time and is only ever `reporting_recently` or
`stale`. *Killed* — Athena cannot signal a foreign OS process, so a kill is a
request the worker must come back for. The three columns behind it are three
distinct facts, kept apart on purpose: the operator ASKED, the worker SAID it
heard, the worker SAID it stopped. A worker that goes quiet after a kill request
is stale with an unacknowledged request — never "terminated".

Like check-ins, the registry row is mutable operational state, not audit history.
The append-only trail keeps the kill request, the acknowledgment, and the stop as
events; this table only says where things stand right now.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena import config

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_LIST_LIMIT = 500

REPORTING_RECENTLY = "reporting_recently"
STALE = "stale"

# What the operator's instruction has come to, so far. Read them as a story about
# who said what, not as process states — only `requested` is Athena's own fact.
KILL_NONE = "none"
KILL_REQUESTED = "requested"
KILL_ACKNOWLEDGED = "acknowledged"
KILL_STOPPED = "stopped"

#: Reported by a worker that was told to stop, said it heard, and is STILL
#: reporting. Not an error state Athena can resolve — it is the operator's to act
#: on, and hiding it would be the whole point of the feature undone.
KILL_DEFIED = "acknowledged_but_reporting"

_SAFE_COLUMNS = (
    "w.id, w.agent_id, w.worker_key, w.node_label, w.capabilities, "
    "w.first_seen_at, w.last_seen_at, w.kill_requested_at, w.kill_requested_by, "
    "w.kill_acknowledged_at, w.stopped_at"
)


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be positive")
    return min(limit, _MAX_LIST_LIMIT)


def _stale_after_seconds(value: int | None) -> int:
    resolved = config.WORKER_STALE_SECONDS if value is None else value
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


def _kill_state(row: dict, *, reporting_state: str) -> str:
    if row["kill_requested_at"] is None:
        return KILL_NONE
    if row["stopped_at"] is not None:
        return KILL_STOPPED
    if row["kill_acknowledged_at"] is None:
        return KILL_REQUESTED
    # It heard, and it is still talking. Surfacing this plainly is the point: an
    # operator who asked a worker to stop needs to know when it did not.
    if reporting_state == REPORTING_RECENTLY:
        return KILL_DEFIED
    return KILL_ACKNOWLEDGED


def _projection(
    row: dict, *, stale_seconds: int | None = None, now: datetime | None = None
) -> dict:
    """One worker as an operator sees it: what it reported, plus what the server
    clock makes of it. Never `last_token_id` — credential metadata stays internal,
    exactly as it does for check-ins."""
    last_seen = datetime.strptime(row["last_seen_at"], _TS_FORMAT).replace(tzinfo=UTC)
    elapsed = max(0.0, (_utc_now(now) - last_seen).total_seconds())
    reporting_state = (
        REPORTING_RECENTLY if elapsed <= _stale_after_seconds(stale_seconds) else STALE
    )
    return {
        **row,
        "capabilities": split_capabilities(row["capabilities"]),
        "age_seconds": int(elapsed),
        "reporting_state": reporting_state,
        "kill_state": _kill_state(row, reporting_state=reporting_state),
        # The one flag the worker itself acts on. True from the moment the operator
        # asks until the worker reports stopped, so a worker that acknowledged and
        # kept running keeps being told.
        "kill_requested": row["kill_requested_at"] is not None
        and row["stopped_at"] is None,
    }


def join_capabilities(values: object) -> str:
    """Normalize self-declared capabilities into stored text.

    Bounded, de-duplicated, order-preserving, and never parsed as anything but
    labels: Athena does not route or authorize on what a worker claims it can do,
    so this validates shape and nothing more."""
    if values is None:
        return ""
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError("capabilities must be a list of strings")
    seen: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("capabilities must be a list of strings")
        label = value.strip()
        if not label:
            continue
        if len(label) > 100 or any(character in label for character in ",\n\r"):
            raise ValueError("each capability must be <= 100 characters without commas")
        if label not in seen:
            seen.append(label)
    if len(seen) > 20:
        raise ValueError("at most 20 capabilities")
    joined = ",".join(seen)
    if len(joined) > 2000:
        raise ValueError("capabilities are too long")
    return joined


def split_capabilities(stored: str) -> list[str]:
    return [label for label in (stored or "").split(",") if label]


def upsert_worker(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    worker_key: str,
    node_label: str,
    capabilities: str,
    token_id: int,
) -> int:
    """Register or refresh one worker, returning its id. Does not commit.

    A worker that reports RUNNING again clears only ``stopped_at``: fresher evidence
    contradicts a stop it claimed earlier. The kill request and acknowledgment
    survive deliberately — a process restart must never quietly cancel an
    instruction the operator gave, and the resulting "told to stop, still
    reporting" reading is exactly what they need to see."""
    conn.execute(
        "INSERT INTO agent_workers "
        "(agent_id, worker_key, node_label, capabilities, last_token_id) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(agent_id, worker_key) DO UPDATE SET "
        "last_seen_at = datetime('now'), node_label = excluded.node_label, "
        "capabilities = excluded.capabilities, "
        "last_token_id = excluded.last_token_id, stopped_at = NULL",
        (agent_id, worker_key, node_label, capabilities, token_id),
    )
    row = conn.execute(
        "SELECT id FROM agent_workers WHERE agent_id = ? AND worker_key = ?",
        (agent_id, worker_key),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def count_workers(conn: sqlite3.Connection, *, agent_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM agent_workers WHERE agent_id = ?", (agent_id,)
        ).fetchone()["n"]
    )


def get_worker(
    conn: sqlite3.Connection,
    worker_id: int,
    *,
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> dict | None:
    row = conn.execute(
        f"SELECT {_SAFE_COLUMNS}, u.name AS agent_name, u.email AS agent_email "
        "FROM agent_workers w JOIN users u ON u.id = w.agent_id WHERE w.id = ?",
        (worker_id,),
    ).fetchone()
    if row is None:
        return None
    return _projection(dict(row), stale_seconds=stale_seconds, now=now)


def list_workers(
    conn: sqlite3.Connection,
    *,
    agent_id: int | None = None,
    limit: int = 100,
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """The fleet, most recently reporting first — optionally for one agent."""
    bounded = _bounded_limit(limit)
    clauses: list[str] = []
    params: list[int] = []
    if agent_id is not None:
        clauses.append("w.agent_id = ?")
        params.append(int(agent_id))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(bounded)
    rows = conn.execute(
        f"SELECT {_SAFE_COLUMNS}, u.name AS agent_name, u.email AS agent_email "
        f"FROM agent_workers w JOIN users u ON u.id = w.agent_id{where} "
        "ORDER BY w.last_seen_at DESC, w.id LIMIT ?",
        params,
    ).fetchall()
    return [
        _projection(dict(row), stale_seconds=stale_seconds, now=now) for row in rows
    ]


def set_kill_requested(
    conn: sqlite3.Connection, *, worker_id: int, actor_id: int
) -> None:
    """Record the operator's instruction. Does not commit."""
    conn.execute(
        "UPDATE agent_workers SET kill_requested_at = datetime('now'), "
        "kill_requested_by = ? WHERE id = ?",
        (actor_id, worker_id),
    )


def clear_kill_requested(conn: sqlite3.Connection, *, worker_id: int) -> None:
    """Withdraw an instruction the worker has not answered yet. Does not commit."""
    conn.execute(
        "UPDATE agent_workers SET kill_requested_at = NULL, kill_requested_by = NULL, "
        "kill_acknowledged_at = NULL WHERE id = ?",
        (worker_id,),
    )


def set_kill_acknowledged(conn: sqlite3.Connection, *, worker_id: int) -> None:
    """The worker says it received the request. First answer wins — a later
    heartbeat must not keep moving the moment it heard. Does not commit."""
    conn.execute(
        "UPDATE agent_workers SET kill_acknowledged_at = datetime('now') "
        "WHERE id = ? AND kill_requested_at IS NOT NULL "
        "AND kill_acknowledged_at IS NULL",
        (worker_id,),
    )


def set_stopped(conn: sqlite3.Connection, *, worker_id: int) -> None:
    """The worker says it stopped. Its own claim, not an observation — a worker
    that reports running again clears this. Does not commit."""
    conn.execute(
        "UPDATE agent_workers SET stopped_at = datetime('now') WHERE id = ?",
        (worker_id,),
    )
