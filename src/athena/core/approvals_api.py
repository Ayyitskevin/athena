"""The approvals REST API — the operator's decision queue.

Approvals are core (any module's gated action lands here), so this lives in
`core/` next to the state it serves, matching `users_api` / `tokens_api`.

The queue is admin-only: deciding an approval is an operator authority, and the
listing names who wanted to do what to which target. A gated agent does not read
this surface — it learns its answer from retrying the write.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from athena.core import approvals, users
from athena.core.deps import get_conn
from athena.core.identity import admin_actor

router = APIRouter(prefix="/approvals", tags=["core"])


class ApprovalOut(BaseModel):
    id: int
    action_kind: str
    target_kind: str
    target_id: int
    requested_by: int
    run_id: str | None = None
    state: str
    decided_by: int | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    consumed_at: str | None = None
    created_at: str


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = None


class ApprovalPolicyUpdate(BaseModel):
    action_kind: str


@router.get("", response_model=list[ApprovalOut])
def index(
    response: Response,
    state: Literal["pending", "approved", "rejected"] | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """The operator's approval queue — pending first, then newest.

    This is a steer-by-exception surface: `?state=pending` is the list of
    decisions actually waiting on a human. Admin-only and never cached, because
    each row names an actor and a target."""
    response.headers["Cache-Control"] = "private, no-store"
    return [
        request.public()
        for request in approvals.list_requests(conn, state=state, limit=limit)
    ]


@router.get("/{request_id}", response_model=ApprovalOut)
def show(
    request_id: int,
    response: Response,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    request = approvals.get_request(conn, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="no such approval request")
    return request.public()


@router.post("/{request_id}/decision", response_model=ApprovalOut)
def decide(
    request_id: int,
    payload: ApprovalDecision,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Approve or reject a pending request, atomically with its audit event.

    Approving does NOT perform the action: it opens the gate for exactly one
    retry by the original requester against the same target. The agent's retry
    re-validates everything — visibility, scope, the blocked-close policy, its
    budget, and any `If-Match` — so an approval authorizes an intent, never a
    stored side effect. Deciding an already-settled request is a 409 rather than
    a silent flip of an answer the agent may have acted on."""
    try:
        decided = approvals.decide(
            conn,
            actor_id=actor["id"],
            request_id=request_id,
            decision=payload.decision,
            note=payload.note,
        )
    except approvals.ApprovalDecisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return decided.public()


@router.get("/policies/{user_id}", response_model=list[str])
def read_policy(
    user_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[str]:
    """Which action kinds require approval for this user (empty = ungated)."""
    if users.get_user(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="no such user")
    return approvals.gated_kinds(conn, user_id)


@router.put("/policies/{user_id}", response_model=list[str])
def set_policy(
    user_id: int,
    payload: ApprovalPolicyUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[str]:
    """Gate an action kind for a user. Idempotent; the vocabulary is closed, so an
    unknown action kind is a 422 rather than a silently inert policy."""
    if users.get_user(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="no such user")
    try:
        approvals.set_policy(
            conn,
            actor_id=actor["id"],
            target_user_id=user_id,
            action_kind=payload.action_kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return approvals.gated_kinds(conn, user_id)


@router.delete("/policies/{user_id}/{action_kind}", response_model=list[str])
def clear_policy(
    user_id: int,
    action_kind: str,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[str]:
    """Ungate an action kind for a user. Idempotent."""
    if users.get_user(conn, user_id) is None:
        raise HTTPException(status_code=404, detail="no such user")
    approvals.clear_policy(
        conn,
        actor_id=actor["id"],
        target_user_id=user_id,
        action_kind=action_kind,
    )
    return approvals.gated_kinds(conn, user_id)
