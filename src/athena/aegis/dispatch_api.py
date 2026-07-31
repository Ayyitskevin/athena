"""Dispatch REST: asking an executor to do work, and hearing back.

Two directions, authorized in completely different ways.

*Outbound* (`POST /issues/{id}/dispatch`) is an ordinary authenticated write: the
command checks role, scope, visibility, budget, and any approval gate.

*Reads* inherit the work item's visibility. List filtering is composed into SQL
before its bound, and detail resolves hidden and missing dispatches identically, so
executor metadata cannot become a side channel around private issues.

*Inbound* (`POST /callbacks/icarus`) has **no Athena credential at all**. The
executor is not an Athena user and holds no token; it authenticates with an HMAC
over the exact request body, using the shared secret. That is why the callback
route is deliberately narrow: it can attach evidence and a terminal outcome to a
dispatch Athena already created, and it can do nothing else. It cannot create work,
change an issue, or name an actor.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from athena import config
from athena.aegis import icarus_commands
from athena.core import dispatch, identity, tokens, webhooks
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(tags=["aegis"])

_STATUS_BY_KIND: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "unavailable": 503,
}


class DispatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=1, max_length=icarus_commands.MAX_REPO_CHARS)
    base_commit: str = Field(min_length=1, max_length=icarus_commands.MAX_COMMIT_CHARS)
    capability: Literal["repo.edit", "ci.run"]


class DispatchOut(BaseModel):
    id: int
    work_item_id: int
    # The reserved `icarus:` run Athena minted, and the run it descends from.
    run_id: str
    parent_run_id: str | None = None
    # The executor's own run id — its claim about itself, stored to correlate
    # callbacks. Null until it accepts, and forever if it never answers.
    icarus_run_id: str | None = None
    repo: str
    base_commit: str
    capability: str
    policy_digest: str
    approval_state: str
    idempotency_key: str
    # Opaque pointers the executor chose. Referenced, never copied.
    evidence_ref: str | None = None
    completion_ref: str | None = None
    # ATHENA'S KNOWLEDGE, not the executor's progress: `accepted` means "it said it
    # accepted", never "work is running".
    state: Literal[
        "pending_delivery", "accepted", "undeliverable", "completed", "failed"
    ]
    last_error: str | None = None
    dispatched_by: int
    created_at: str
    updated_at: str


class CallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icarus_run_id: str = Field(min_length=1, max_length=dispatch.MAX_REF_CHARS)
    # Echoed back so Athena can check the authorization state it dispatched under
    # against the one the executor claims it acted under.
    policy_digest: str = Field(min_length=1, max_length=200)
    evidence_ref: str | None = Field(
        default=None, min_length=1, max_length=dispatch.MAX_REF_CHARS
    )
    completion_ref: str | None = Field(
        default=None, min_length=1, max_length=dispatch.MAX_REF_CHARS
    )
    # Absent for a progress report; present for a terminal one.
    outcome: Literal["completed", "failed"] | None = None


class CallbackOut(BaseModel):
    dispatch_id: int
    policy_digest_matches: bool


class CallbackErrorOut(BaseModel):
    detail: str


_CALLBACK_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": CallbackErrorOut, "description": description}
    for status, description in {
        400: "The request Content-Length is invalid",
        401: "Missing or invalid HMAC signature",
        404: "Authenticated callback names no dispatch",
        409: "Authenticated callback conflicts with canonical evidence",
        413: "The request body exceeds Athena's global request-size limit",
        422: "Authenticated callback payload is invalid",
        429: "Anonymous direct-peer-IP budget exhausted",
        503: "No execution fleet is configured",
    }.items()
}


def _refuse(exc: icarus_commands.IcarusCommandError) -> HTTPException:
    return HTTPException(status_code=_STATUS_BY_KIND[exc.kind], detail=exc.detail)


async def _verified_callback(
    request: Request,
    x_athena_signature: str | None = Header(default=None, include_in_schema=False),
) -> CallbackIn:
    """Authenticate the raw bounded body before parsing it or opening SQLite."""
    if not config.icarus_configured():
        raise HTTPException(status_code=503, detail="no execution fleet is configured")
    body = await request.body()
    if not webhooks.verify(config.ICARUS_SECRET, body, x_athena_signature):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        return CallbackIn.model_validate_json(body)
    except ValidationError as exc:
        # Authentication succeeded, so validation may now be distinguished from
        # auth failure. Keep the detail generic: malformed Unicode from an
        # authenticated but buggy peer must not be echoed through the response.
        raise HTTPException(status_code=422, detail="invalid callback payload") from exc


def _dispatch_reader(actor: dict = Depends(current_actor)) -> dict:
    """Authenticated Aegis reader, including least-privilege worker tokens."""
    if not (
        identity.token_has_scope(actor, tokens.READ_SCOPE)
        or identity.token_has_scope(actor, tokens.ISSUE_WRITE_SCOPE)
    ):
        raise identity.ScopeDenied(actor, "read or issue:write")
    return actor


@router.post("/issues/{issue_id}/dispatch", response_model=DispatchOut, status_code=201)
def create_dispatch(
    issue_id: Annotated[int, Path(ge=1, le=dispatch.MAX_SQLITE_INTEGER)],
    payload: DispatchIn,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Ask the configured executor to do work on this issue.

    The record and its audit event commit first; the outbound call happens after,
    so the durable fact "Athena decided to dispatch this" survives whether or not
    the far side answers. 201 names the dispatch Athena created — not work anyone
    has started."""
    try:
        record = icarus_commands.request_dispatch(
            conn,
            actor=actor,
            work_item_id=issue_id,
            repo=payload.repo,
            base_commit=payload.base_commit,
            capability=payload.capability,
        )
        # Post-commit. A delivery failure is recorded on the dispatch, not raised:
        # the caller's write succeeded, and the state says what happened next.
        return icarus_commands.deliver_dispatch(conn, dispatch_id=record["id"])
    except icarus_commands.IcarusCommandError as exc:
        raise _refuse(exc) from exc


@router.get("/dispatches", response_model=list[DispatchOut])
def index(
    work_item_id: int | None = Query(None, ge=1, le=dispatch.MAX_SQLITE_INTEGER),
    state: str | None = Query(None),
    limit: int = Query(50, ge=1, le=dispatch.MAX_LIST_LIMIT),
    actor: dict = Depends(_dispatch_reader),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """What Athena has handed out, newest first."""
    try:
        return dispatch.list_dispatches(
            conn,
            actor=actor,
            work_item_id=work_item_id,
            state=state,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/dispatches/{dispatch_id}", response_model=DispatchOut)
def show(
    dispatch_id: Annotated[int, Path(ge=1, le=dispatch.MAX_SQLITE_INTEGER)],
    actor: dict = Depends(_dispatch_reader),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    record = dispatch.get_visible_dispatch(conn, dispatch_id, actor=actor)
    if record is None:
        raise HTTPException(status_code=404, detail="no such dispatch")
    return record


@router.post(
    "/callbacks/icarus",
    response_model=CallbackOut,
    status_code=202,
    responses=_CALLBACK_ERROR_RESPONSES,
    # Manual raw-body parsing is required for HMAC-before-JSON. Preserve the
    # public request contract that FastAPI would otherwise derive from a body
    # parameter.
    openapi_extra={
        "parameters": [
            {
                "name": "x-athena-signature",
                "in": "header",
                "required": True,
                "description": "sha256=<hex HMAC of the exact request body>",
                "schema": {"type": "string"},
            }
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CallbackIn.model_json_schema()}},
        },
    },
)
async def icarus_callback(
    payload: CallbackIn = Depends(_verified_callback),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Accept evidence or a terminal outcome from the executor.

    Authenticated by HMAC over the exact bytes received — not by any Athena
    credential, because the executor has none. The signature is compared in
    constant time, and an unsigned or mis-signed callback is refused before
    anything is looked up, so this endpoint cannot be used to probe which
    dispatches exist.

    **Idempotent.** The first evidence pointer is canonical. Its exact replay is a
    no-op; a different pointer conflicts while work is open because this protocol
    carries no sequence that could prove which is newer. Once terminal, outcome
    changes and evidence overwrites are absorbed. A legacy terminal callback that
    omitted evidence may still have its one null evidence slot filled by delayed
    progress. Executors retry and callbacks reorder, and neither may fork or roll
    back the record.

    **The policy digest is checked, and a mismatch is recorded rather than
    rejected.** If the executor claims it acted under authorization that differs
    from what Athena dispatched, that is precisely the event an operator needs to
    see — dropping it would destroy the evidence the digest exists to produce.
    """
    try:
        return icarus_commands.apply_callback(
            conn,
            icarus_run_id=payload.icarus_run_id,
            policy_digest=payload.policy_digest,
            evidence_ref=payload.evidence_ref,
            completion_ref=payload.completion_ref,
            outcome=payload.outcome,
        )
    except icarus_commands.IcarusCommandError as exc:
        raise _refuse(exc) from exc
