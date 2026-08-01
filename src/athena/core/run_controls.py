"""Data access for run controls — the operator's cooperative levers on a live run.

This module owns every SQL statement that touches ``run_controls`` (mirroring
``workers.py`` for ``agent_workers``). A control is a request record: the operator
asked, the bound agent answered or did not. Lifecycle facts are separate
timestamp columns, transitions are compare-and-set UPDATEs whose predicates make
raced settlements 0-row no-ops, and the "expired" state is DERIVED here at read
time from the server clock — never stored, so an unanswered control can age into
expiry without anyone writing a claim nobody made.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3

from athena import config

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

KIND_STEER = "steer"
KIND_REQUEST_CANCEL = "request_cancel"
KIND_REQUEST_FRESH_CONTEXT = "request_fresh_context"
CONTROL_KINDS = (KIND_STEER, KIND_REQUEST_CANCEL, KIND_REQUEST_FRESH_CONTEXT)

SETTLEMENT_COMPLETED = "completed"
SETTLEMENT_DECLINED = "declined"

#: Derived lifecycle states, in the order a control moves through them. These are
#: read-time projections over the stored facts; only the two settlements are ever
#: written, and "expired" is always the clock's verdict, not a stored claim.
STATE_REQUESTED = "requested"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_COMPLETED = "completed"
STATE_DECLINED = "declined"
STATE_EXPIRED = "expired"
CONTROL_STATES = (
    STATE_REQUESTED,
    STATE_ACKNOWLEDGED,
    STATE_COMPLETED,
    STATE_DECLINED,
    STATE_EXPIRED,
)
#: The extra list filter for "unsettled and not yet expired" — the bound agent's
#: inbox question and the run panel's headline.
STATE_FILTER_OPEN = "open"

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 86_400

_SELECT = (
    "SELECT c.id, c.schema_version, c.run_id, c.agent_id, "
    "agent.name AS agent_name, c.worker_id, w.worker_key, c.kind, c.payload, "
    "c.requested_by, requester.name AS requested_by_name, c.idempotency_key, "
    "c.created_at, "
    "c.expires_at, c.acknowledged_at, c.settled_at, c.settled_by, c.settlement, "
    "c.result_summary, c.result_payload, c.requested_event_id, "
    "c.acknowledged_event_id, c.settled_event_id "
    "FROM run_controls c "
    "JOIN users agent ON agent.id = c.agent_id "
    "JOIN users requester ON requester.id = c.requested_by "
    "LEFT JOIN agent_workers w ON w.id = c.worker_id "
)


def _utc_now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def stamp(now: datetime | None = None) -> str:
    """The server-clock instant in the storage format every timestamp here uses.

    Injectable for tests, exactly like the worker staleness clock: comparisons
    between this stamp and stored ``expires_at`` values are lexicographic-safe
    because both share one fixed UTC second-resolution format."""
    return _utc_now(now).strftime(_TS_FORMAT)


def insert_control(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    agent_id: int,
    worker_id: int | None,
    kind: str,
    payload: str,
    requested_by: int,
    idempotency_key: str,
    ttl_seconds: int,
    now_stamp: str,
) -> int:
    """Insert one control row and return its id. Does not commit.

    ``created_at`` and ``expires_at`` derive from the same caller-supplied stamp
    so an injected test clock produces a self-consistent row."""
    cur = conn.execute(
        "INSERT INTO run_controls "
        "(run_id, agent_id, worker_id, kind, payload, requested_by, "
        "idempotency_key, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        "datetime(?, printf('+%d seconds', ?)))",
        (
            run_id,
            agent_id,
            worker_id,
            kind,
            payload,
            requested_by,
            idempotency_key,
            now_stamp,
            now_stamp,
            ttl_seconds,
        ),
    )
    control_id = cur.lastrowid
    assert control_id is not None
    return int(control_id)


def get_control(conn: sqlite3.Connection, control_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE c.id = ?", (control_id,)).fetchone()
    return None if row is None else dict(row)


def get_by_idempotency_key(
    conn: sqlite3.Connection, *, requested_by: int, idempotency_key: str
) -> dict | None:
    row = conn.execute(
        f"{_SELECT} WHERE c.requested_by = ? AND c.idempotency_key = ?",
        (requested_by, idempotency_key),
    ).fetchone()
    return None if row is None else dict(row)


def find_live_control(
    conn: sqlite3.Connection, *, run_id: str, kind: str, now_stamp: str
) -> dict | None:
    """The unsettled, unexpired control of this kind on this run, if one exists.

    "Live" includes the clock, which a partial unique index cannot express, so
    at-most-one-live is enforced by the create command under its immediate
    transaction with this read."""
    row = conn.execute(
        f"{_SELECT} WHERE c.run_id = ? AND c.kind = ? "
        "AND c.settled_at IS NULL AND c.expires_at > ? "
        "ORDER BY c.id DESC LIMIT 1",
        (run_id, kind, now_stamp),
    ).fetchone()
    return None if row is None else dict(row)


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be positive")
    return min(limit, MAX_LIST_LIMIT)


def _state_clause(state: str, now_param: str) -> str:
    """The SQL predicate for one derived state, filtered BEFORE LIMIT so a page
    of "requested" controls is a real page, not the survivors of one."""
    if state == STATE_COMPLETED:
        return "c.settlement = 'completed'"
    if state == STATE_DECLINED:
        return "c.settlement = 'declined'"
    if state == STATE_EXPIRED:
        return f"c.settled_at IS NULL AND c.expires_at <= {now_param}"
    if state == STATE_ACKNOWLEDGED:
        return (
            f"c.settled_at IS NULL AND c.expires_at > {now_param} "
            "AND c.acknowledged_at IS NOT NULL"
        )
    if state == STATE_REQUESTED:
        return (
            f"c.settled_at IS NULL AND c.expires_at > {now_param} "
            "AND c.acknowledged_at IS NULL"
        )
    if state == STATE_FILTER_OPEN:
        return f"c.settled_at IS NULL AND c.expires_at > {now_param}"
    raise ValueError(f"unknown control state filter: {state}")


def list_rows(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
    agent_id: int | None = None,
    state: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    now_stamp: str,
) -> list[dict]:
    """Bounded newest-first raw rows. Callers own visibility; this owns SQL."""
    bounded = _bounded_limit(limit)
    clauses: list[str] = []
    params: list[object] = []
    if run_id is not None:
        clauses.append("c.run_id = ?")
        params.append(run_id)
    if agent_id is not None:
        clauses.append("c.agent_id = ?")
        params.append(agent_id)
    if state is not None:
        clauses.append(f"({_state_clause(state, '?')})")
        # Every state predicate uses the clock at most once; bind it exactly as
        # many times as the clause mentions it.
        params.extend([now_stamp] * _state_clause(state, "?").count("?"))
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    params.append(bounded)
    rows = conn.execute(
        f"{_SELECT} {where}ORDER BY c.id DESC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def count_open(conn: sqlite3.Connection, *, now_stamp: str) -> int:
    """How many controls are live right now — unsettled and unexpired.

    The fleet-attention rollup's number. It uses the same state predicate the
    list reads use, so the card can never disagree with the page it links to
    (/admin/run-controls), which renders rows from the identical clause."""
    clause = _state_clause(STATE_FILTER_OPEN, "?")
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM run_controls c WHERE {clause}",
        [now_stamp] * clause.count("?"),
    ).fetchone()
    return int(row["n"])


def state_counts_by_agent(
    conn: sqlite3.Connection, *, now_stamp: str
) -> dict[int, dict[str, int]]:
    """Per-agent control tallies for the answerability projection.

    One pass over the table; the same derived-state predicates every list read
    uses, expressed as CASE buckets. `expired_unanswered` is the derivation the
    docs promise — the clock's verdict on an unsettled row, never a stored
    claim."""
    rows = conn.execute(
        "SELECT agent_id, "
        "SUM(CASE WHEN settled_at IS NULL AND expires_at > :now THEN 1 ELSE 0 END)"
        " AS open, "
        "SUM(CASE WHEN settled_at IS NULL AND expires_at <= :now THEN 1 ELSE 0 END)"
        " AS expired_unanswered, "
        "SUM(CASE WHEN settlement = 'completed' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN settlement = 'declined' THEN 1 ELSE 0 END) AS declined "
        "FROM run_controls GROUP BY agent_id",
        {"now": now_stamp},
    ).fetchall()
    return {
        int(row["agent_id"]): {
            "open": int(row["open"]),
            "expired_unanswered": int(row["expired_unanswered"]),
            "completed": int(row["completed"]),
            "declined": int(row["declined"]),
        }
        for row in rows
    }


def cas_acknowledge(
    conn: sqlite3.Connection, *, control_id: int, now_stamp: str
) -> bool:
    """Record the agent's receipt claim, first-writer-wins. Does not commit.

    The predicate is the whole contract: only an unsettled, unacknowledged,
    unexpired control can be acknowledged, and a raced second acknowledgement is
    a 0-row no-op the caller reads from the return value."""
    cur = conn.execute(
        "UPDATE run_controls SET acknowledged_at = ? "
        "WHERE id = ? AND settled_at IS NULL AND acknowledged_at IS NULL "
        "AND expires_at > ?",
        (now_stamp, control_id, now_stamp),
    )
    return cur.rowcount > 0


def cas_settle(
    conn: sqlite3.Connection,
    *,
    control_id: int,
    settled_by: int,
    settlement: str,
    result_summary: str,
    result_payload: str | None,
    now_stamp: str,
) -> bool:
    """Record the agent's settlement claim, first-writer-wins. Does not commit."""
    cur = conn.execute(
        "UPDATE run_controls SET settled_at = ?, settled_by = ?, settlement = ?, "
        "result_summary = ?, result_payload = ? "
        "WHERE id = ? AND settled_at IS NULL AND expires_at > ?",
        (
            now_stamp,
            settled_by,
            settlement,
            result_summary,
            result_payload,
            control_id,
            now_stamp,
        ),
    )
    return cur.rowcount > 0


def set_requested_event(
    conn: sqlite3.Connection, *, control_id: int, event_id: int
) -> None:
    conn.execute(
        "UPDATE run_controls SET requested_event_id = ? WHERE id = ?",
        (event_id, control_id),
    )


def set_acknowledged_event(
    conn: sqlite3.Connection, *, control_id: int, event_id: int
) -> None:
    conn.execute(
        "UPDATE run_controls SET acknowledged_event_id = ? WHERE id = ?",
        (event_id, control_id),
    )


def set_settled_event(
    conn: sqlite3.Connection, *, control_id: int, event_id: int
) -> None:
    conn.execute(
        "UPDATE run_controls SET settled_event_id = ? WHERE id = ?",
        (event_id, control_id),
    )


def derived_state(row: dict, *, now: datetime | None = None) -> str:
    """What the stored facts plus the clock make of one control, and nothing more."""
    if row["settlement"] is not None:
        return str(row["settlement"])
    if str(row["expires_at"]) <= stamp(now):
        return STATE_EXPIRED
    if row["acknowledged_at"] is not None:
        return STATE_ACKNOWLEDGED
    return STATE_REQUESTED


def projection(row: dict, *, now: datetime | None = None) -> dict:
    """One control as its readers see it: stored claims plus derived state.

    ``handoff`` is the parsed fresh-context result object or None; the stored
    JSON text never leaves this layer raw. Nothing here is credential metadata,
    so the projection is the full row."""
    state = derived_state(row, now=now)
    expires = datetime.strptime(row["expires_at"], _TS_FORMAT).replace(tzinfo=UTC)
    remaining = (expires - _utc_now(now)).total_seconds()
    handoff = None
    if row["result_payload"] is not None:
        handoff = json.loads(row["result_payload"])
    projected = {key: row[key] for key in row if key != "result_payload"}
    projected.update(
        {
            "state": state,
            "expired": state == STATE_EXPIRED,
            "expires_in_seconds": max(0, int(remaining))
            if row["settlement"] is None
            else 0,
            "handoff": handoff,
        }
    )
    return projected


def default_ttl_seconds() -> int:
    """The configured default lifetime, clamped into the protocol window.

    `_int_env` already enforces the floor; the ceiling is clamped here so a
    deployment that sets an enormous ATHENA_RUN_CONTROL_TTL_SECONDS cannot mint
    controls that outlive the documented 60–86400 bound every explicit caller
    is held to."""
    return min(max(config.RUN_CONTROL_TTL_SECONDS, MIN_TTL_SECONDS), MAX_TTL_SECONDS)
