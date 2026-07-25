"""Dispatch REST: asking an executor to do work, and hearing back.

Two directions, authorized in completely different ways.

*Outbound* (`POST /issues/{id}/dispatch`) is an ordinary authenticated write: the
command checks role, scope, visibility, budget, and any approval gate.

*Inbound* (`POST /callbacks/icarus`) has **no Athena credential at all**. The
executor is not an Athena user and holds no token; it authenticates with an HMAC
over the exact request body, using the shared secret. That is why the callback
route is deliberately narrow: it can attach evidence and a terminal outcome to a
dispatch Athena already created, and it can do nothing else. It cannot create work,
change an issue, or name an actor.
"""

from __future__ import annotations

import hmac
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from athena import config
from athena.aegis import icarus_commands
from athena.core import activity, db, dispatch, run_context, webhooks
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(tags=["aegis"])


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
    evidence_ref: str | None = Field(default=None, max_length=dispatch.MAX_REF_CHARS)
    completion_ref: str | None = Field(default=None, max_length=dispatch.MAX_REF_CHARS)
    # Absent for a progress report; present for a terminal one.
    outcome: Literal["completed", "failed"] | None = None


def _refuse(exc: dispatch.DispatchError) -> HTTPException:
    return HTTPException(
        status_code=dispatch.STATUS_BY_KIND[exc.kind], detail=exc.detail
    )


@router.post("/issues/{issue_id}/dispatch", response_model=DispatchOut, status_code=201)
def create_dispatch(
    issue_id: int,
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
    except dispatch.DispatchError as exc:
        raise _refuse(exc) from exc


@router.get("/dispatches", response_model=list[DispatchOut])
def index(
    work_item_id: int | None = Query(None),
    state: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """What Athena has handed out, newest first."""
    try:
        return dispatch.list_dispatches(
            conn, work_item_id=work_item_id, state=state, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/dispatches/{dispatch_id}", response_model=DispatchOut)
def show(
    dispatch_id: int,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    record = dispatch.get_dispatch(conn, dispatch_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such dispatch")
    return record


@router.post("/callbacks/icarus", status_code=202)
async def icarus_callback(
    request: Request,
    payload: CallbackIn,
    x_athena_signature: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Accept evidence or a terminal outcome from the executor.

    Authenticated by HMAC over the exact bytes received — not by any Athena
    credential, because the executor has none. The signature is compared in
    constant time, and an unsigned or mis-signed callback is refused before
    anything is looked up, so this endpoint cannot be used to probe which
    dispatches exist.

    **Idempotent.** A replayed progress callback re-records the same evidence
    pointer; a replayed terminal callback after the dispatch already settled is
    accepted and ignored. Executors retry, and a retry must not fork the record.

    **The policy digest is checked, and a mismatch is recorded rather than
    rejected.** If the executor claims it acted under authorization that differs
    from what Athena dispatched, that is precisely the event an operator needs to
    see — dropping it would destroy the evidence the digest exists to produce.
    """
    if not config.icarus_configured():
        raise HTTPException(status_code=503, detail="no execution fleet is configured")
    body = await request.body()
    expected = webhooks.sign(config.ICARUS_SECRET, body)
    if not x_athena_signature or not hmac.compare_digest(x_athena_signature, expected):
        raise HTTPException(status_code=401, detail="invalid signature")

    record = dispatch.get_by_icarus_run(conn, payload.icarus_run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such dispatch")

    digest_matches = hmac.compare_digest(payload.policy_digest, record["policy_digest"])
    # Stamp everything recorded here with the dispatch's own run, so the executor's
    # reports join the control-plane trail as that run's events and appear in its
    # replay and lineage.
    # set_run_id, not set_client_run_id: `icarus:` is a RESERVED namespace, and the
    # client form drops reserved ids to None precisely so a caller cannot forge one.
    # Athena is the one stamping here — it minted this run, it created this dispatch,
    # and it authenticated the callback before reaching this line.
    token = run_context.set_run_id(record["run_id"])
    try:
        with db.transaction(conn, immediate=True):
            if not digest_matches:
                activity.record(
                    conn,
                    actor_id=record["dispatched_by"],
                    verb=dispatch.VERB_DIGEST_MISMATCH,
                    target_kind="issue",
                    target_id=record["work_item_id"],
                    detail=(
                        f"dispatched {record['policy_digest'][:16]}…, "
                        f"reported {payload.policy_digest[:16]}…"
                    ),
                    commit=False,
                )
            if payload.evidence_ref:
                dispatch.record_evidence(
                    conn,
                    dispatch_id=record["id"],
                    evidence_ref=payload.evidence_ref,
                )
                activity.record(
                    conn,
                    actor_id=record["dispatched_by"],
                    verb=dispatch.VERB_EVIDENCE,
                    target_kind="issue",
                    target_id=record["work_item_id"],
                    detail=payload.evidence_ref[: dispatch.MAX_REF_CHARS],
                    commit=False,
                )
            if payload.outcome is not None and record["state"] not in (
                dispatch.TERMINAL_STATES
            ):
                dispatch.record_terminal(
                    conn,
                    dispatch_id=record["id"],
                    state=payload.outcome,
                    completion_ref=payload.completion_ref,
                )
                activity.record(
                    conn,
                    actor_id=record["dispatched_by"],
                    verb=dispatch.VERB_TERMINAL,
                    target_kind="issue",
                    target_id=record["work_item_id"],
                    detail=f"{payload.outcome}: {payload.completion_ref or 'no ref'}"[
                        : dispatch.MAX_REF_CHARS
                    ],
                    commit=False,
                )
    finally:
        run_context.reset_run_id(token)
    return {
        "dispatch_id": record["id"],
        "policy_digest_matches": digest_matches,
    }
