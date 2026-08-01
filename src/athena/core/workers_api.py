"""The worker registry REST API.

Upward, a worker refreshes its own row and is told whether the operator asked it
to stop. Downward, an admin leaves that instruction. The registry answers "which
of my agent processes are reporting, on what box, and which were told to stop" —
and refuses to answer "is it alive", because Athena cannot see a foreign process.

Authorization lives in `core/worker_commands.py`, so REST and MCP reach the same
rules; these routes translate transport only.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, WithJsonSchema

from athena.core import worker_commands
from athena.core.ids import RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/workers", tags=["core"])

# Opaque at the schema layer for the same reason run ids are: the command is the
# single validation boundary, and echoing a hostile string back through
# RequestValidationError is a transport problem, not a contract.
WorkerKey = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "pattern": r"^[^\x00-\x1F\x7F]+$",
        }
    ),
]


class WorkerHeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_key: WorkerKey
    node_label: str | None = None
    capabilities: list[str] | None = None
    # The worker's own report about itself. 'stopping' acknowledges a kill request;
    # 'stopped' claims it finished. Both are claims, recorded as claims.
    state: Literal["running", "stopping", "stopped"] = "running"


class WorkerOut(BaseModel):
    id: int
    agent_id: int
    agent_name: str
    agent_email: str
    worker_key: str
    node_label: str
    # Self-declared. Athena never routes, schedules, or authorizes on these.
    capabilities: list[str]
    first_seen_at: str
    last_seen_at: str
    age_seconds: int
    # Cooperative presence, NOT process liveness: 'stale' means this worker stopped
    # reporting, which is all Athena can honestly observe.
    reporting_state: Literal["reporting_recently", "stale"]
    # The operator asked, and (once set) the worker said it heard and then stopped.
    # Three separate facts, because collapsing them would invent certainty.
    kill_requested_at: str | None = None
    kill_requested_by: int | None = None
    kill_acknowledged_at: str | None = None
    stopped_at: str | None = None
    kill_state: Literal[
        "none", "requested", "acknowledged", "acknowledged_but_reporting", "stopped"
    ]
    # The flag the worker itself acts on: true from the request until it reports
    # stopped, so one that acknowledged and kept running keeps being told.
    kill_requested: bool


def _refuse(exc: worker_commands.WorkerCommandError) -> HTTPException:
    return HTTPException(
        status_code=worker_commands.STATUS_BY_KIND[exc.kind], detail=exc.detail
    )


@router.put("/heartbeat", response_model=WorkerOut)
def heartbeat(
    payload: WorkerHeartbeatIn,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Register or refresh this agent's worker, and learn whether to stop.

    A worker names only itself: the identity comes from the bearer token and every
    timestamp from the server clock. The response is the whole actionable half of
    the kill contract — Athena cannot signal the process, so the worker finds out
    by asking."""
    try:
        return worker_commands.heartbeat(
            conn,
            actor=actor,
            worker_key=payload.worker_key,
            node_label=payload.node_label,
            capabilities=payload.capabilities,
            state=payload.state,
        )
    except worker_commands.WorkerCommandError as exc:
        raise _refuse(exc) from exc


@router.get("", response_model=list[WorkerOut])
def index(
    agent_id: int | None = Query(None, description="only this agent's workers"),
    limit: int = Query(100, ge=1, le=500),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """The fleet for an admin; your own workers otherwise — an agent must be able
    to see the instruction left for it."""
    return worker_commands.readable_workers(
        conn, actor=actor, agent_id=agent_id, limit=limit
    )


@router.get("/{worker_id}", response_model=WorkerOut)
def show(
    worker_id: RowIdPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    try:
        return worker_commands.visible_worker(conn, actor=actor, worker_id=worker_id)
    except worker_commands.WorkerCommandError as exc:
        raise _refuse(exc) from exc


@router.post("/{worker_id}/kill", response_model=WorkerOut)
def request_kill(
    worker_id: RowIdPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Ask a worker to stop (admin), atomically with its audit event.

    200, not 202 — the *instruction* is recorded, which is what this endpoint
    promises. Whether the process stops is the worker's answer to give, and
    `kill_state` reports only what has actually been said so far. Idempotent:
    re-asking an unanswered worker does not reset how long it has been ignoring
    you."""
    try:
        return worker_commands.request_kill(conn, actor=actor, worker_id=worker_id)
    except worker_commands.WorkerCommandError as exc:
        raise _refuse(exc) from exc


@router.delete("/{worker_id}/kill", response_model=WorkerOut)
def cancel_kill(
    worker_id: RowIdPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Withdraw a kill request the worker has not acknowledged yet (admin).

    Refused once acknowledged: the worker may already be shutting down, and
    "never mind" would be a claim about the world Athena cannot make."""
    try:
        return worker_commands.cancel_kill(conn, actor=actor, worker_id=worker_id)
    except worker_commands.WorkerCommandError as exc:
        raise _refuse(exc) from exc
