"""The run controls REST API.

Downward, an operator records a bounded control request against a live run —
steer it, ask it to wind down, or ask for a structured fresh-context handoff.
Upward, the run's bound agent reads what is addressed to it and answers:
acknowledge, decline, or complete. Nothing here signals a process; every state
word reports what somebody actually said, and an unanswered request simply
expires.

Authorization lives in `core/run_control_commands.py`, so REST and MCP reach the
same rules; these routes translate transport only.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from athena.core import run_control_commands, run_controls
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/run-controls", tags=["core"])

# Bounded so an id beyond SQLite's integer range is a client error at the
# schema, never an OverflowError inside the driver.
ControlId = Annotated[int, Path(ge=1, le=(1 << 63) - 1)]

# Opaque at the schema layer, like run ids everywhere else: the command is the
# single validation boundary, and echoing a hostile string back through
# RequestValidationError is a transport problem, not a contract.
RunId = Annotated[
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

# The fresh-context handoff arrives as a JSON object; the command validates its
# closed field set and bounds, so the schema documents rather than enforces.
Handoff = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                "unresolved_questions": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "maxLength": 500},
                },
                "athena_refs": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "string", "maxLength": 200},
                },
                "evidence_refs": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "maxLength": 500},
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        }
    ),
]


class RunControlCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: RunId
    kind: Literal["steer", "request_cancel", "request_fresh_context"]
    # Bounded operator guidance (required for steer) or optional reason.
    payload: str | None = None
    # Optional targeting metadata: which registered worker the operator means.
    # Workers hold no credential of their own, so this narrows intent, never
    # authority. Bounded to SQLite's integer range for the same reason the id
    # PATH parameters are (the adversarial-review 2^63 fix): an out-of-range id
    # must be a 422, not a driver OverflowError.
    worker_id: int | None = Field(None, ge=1, le=2**63 - 1)
    ttl_seconds: int | None = None
    # Domain single-flight key; minted server-side when omitted. Retrying with
    # the same key returns the same control; reusing it differently is refused.
    idempotency_key: str | None = None


class RunControlDeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class RunControlCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # steer / request_cancel complete with a bounded summary...
    summary: str | None = None
    # ...request_fresh_context completes with the structured handoff instead.
    handoff: Handoff | None = None


class RunControlHandoffOut(BaseModel):
    summary: str
    unresolved_questions: list[str]
    athena_refs: list[str]
    evidence_refs: list[str]


class RunControlOut(BaseModel):
    id: int
    schema_version: int
    run_id: str
    # The agent the control was admitted against; the only identity that may
    # settle it.
    agent_id: int
    agent_name: str
    worker_id: int | None = None
    worker_key: str | None = None
    kind: Literal["steer", "request_cancel", "request_fresh_context"]
    payload: str
    requested_by: int
    requested_by_name: str
    idempotency_key: str
    created_at: str
    expires_at: str
    # The agent's claims, each recorded once. Acknowledgement proves receipt;
    # completion is an identity-bound claim; neither proves a process effect.
    acknowledged_at: str | None = None
    settled_at: str | None = None
    settled_by: int | None = None
    settlement: Literal["completed", "declined"] | None = None
    result_summary: str
    handoff: RunControlHandoffOut | None = None
    requested_event_id: int | None = None
    acknowledged_event_id: int | None = None
    settled_event_id: int | None = None
    # Derived from the stored facts plus the server clock at read time.
    # 'expired' is always the clock's verdict, never a stored claim.
    state: Literal["requested", "acknowledged", "completed", "declined", "expired"]
    expired: bool
    expires_in_seconds: int


class RunControlCreatedOut(RunControlOut):
    # True when this response replays an identical earlier request under the
    # caller's idempotency key rather than recording a new control.
    replayed: bool


def _refuse(exc: run_control_commands.RunControlCommandError) -> HTTPException:
    return HTTPException(
        status_code=run_control_commands.STATUS_BY_KIND[exc.kind], detail=exc.detail
    )


@router.post("", response_model=RunControlCreatedOut, status_code=201)
def create(
    payload: RunControlCreateIn,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Record one control request against a run (admin), atomically audited.

    201, not 202 — the *request* is recorded, which is what this endpoint
    promises. The bound agent answers on its own schedule, or the request reads
    as expired once `expires_at` passes; Athena never claims the run did
    anything. A replay under the same idempotency key returns the existing
    control with `replayed: true`."""
    try:
        return run_control_commands.create_control(
            conn,
            actor=actor,
            run_id=payload.run_id,
            kind=payload.kind,
            payload=payload.payload,
            worker_id=payload.worker_id,
            ttl_seconds=payload.ttl_seconds,
            idempotency_key=payload.idempotency_key,
        )
    except run_control_commands.RunControlCommandError as exc:
        raise _refuse(exc) from exc


@router.get("", response_model=list[RunControlOut])
def index(
    run_id: RunId | None = Query(None, description="only this run's controls"),
    state: Literal[
        "requested", "acknowledged", "completed", "declined", "expired", "open"
    ]
    | None = Query(None, description="only controls in this derived state"),
    limit: int = Query(
        run_controls.DEFAULT_LIST_LIMIT, ge=1, le=run_controls.MAX_LIST_LIMIT
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """Every control for an admin; your own inbox otherwise — the bound agent
    must be able to read what it is asked to do. `state=open` is the inbox
    question: unsettled and not yet expired."""
    try:
        return run_control_commands.readable_controls(
            conn, actor=actor, run_id=run_id, state=state, limit=limit
        )
    except run_control_commands.RunControlCommandError as exc:
        raise _refuse(exc) from exc


@router.get("/{control_id}", response_model=RunControlOut)
def show(
    control_id: ControlId,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    try:
        return run_control_commands.visible_control(
            conn, actor=actor, control_id=control_id
        )
    except run_control_commands.RunControlCommandError as exc:
        raise _refuse(exc) from exc


@router.post("/{control_id}/acknowledge", response_model=RunControlOut)
def acknowledge(
    control_id: ControlId,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Record that the bound agent read this control. Receipt, nothing more.

    Idempotent: re-acknowledging returns current state without a new event."""
    try:
        return run_control_commands.acknowledge_control(
            conn, actor=actor, control_id=control_id
        )
    except run_control_commands.RunControlCommandError as exc:
        raise _refuse(exc) from exc


@router.post("/{control_id}/decline", response_model=RunControlOut)
def decline(
    payload: RunControlDeclineIn,
    control_id: ControlId,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Record the bound agent's refusal, with the reason the operator will read."""
    try:
        return run_control_commands.decline_control(
            conn, actor=actor, control_id=control_id, reason=payload.reason
        )
    except run_control_commands.RunControlCommandError as exc:
        raise _refuse(exc) from exc


@router.post("/{control_id}/complete", response_model=RunControlOut)
def complete(
    payload: RunControlCompleteIn,
    control_id: ControlId,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Record the bound agent's completion claim.

    An identity-bound claim that the agent did what was asked — never proof of
    an operating-system effect. Fresh-context completions carry the bounded
    structured handoff; the others a bounded summary."""
    try:
        return run_control_commands.complete_control(
            conn,
            actor=actor,
            control_id=control_id,
            summary=payload.summary,
            handoff=payload.handoff,
        )
    except run_control_commands.RunControlCommandError as exc:
        raise _refuse(exc) from exc
