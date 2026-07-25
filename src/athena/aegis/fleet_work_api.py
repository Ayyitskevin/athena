"""Admin REST boundary for the fleet active-work projection."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from athena.aegis import fleet_work
from athena.core import identity, tokens
from athena.core.deps import get_conn


router = APIRouter(prefix="/fleet", tags=["aegis"])

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, X-Athena-Actor",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class ActiveWorkSemanticsOut(_StrictModel):
    claim_state: Literal["server_time_lease"]
    reporting_state: Literal["cooperative_exact_run_checkin"]
    does_not_assert: list[
        Literal[
            "process_alive",
            "work_executing",
            "issue_unblocked",
            "approval_granted",
        ]
    ]


class ActiveWorkIssueOut(_StrictModel):
    id: int
    key: str | None
    title: str
    status: str
    status_category: Literal["todo", "doing", "done"]
    priority: str
    project_id: int | None
    project_name: str | None
    assignee_id: int | None
    archived_at: str | None


class ActiveWorkHolderOut(_StrictModel):
    id: int
    name: str
    email: str
    role: Literal["admin", "member", "viewer"]
    paused_at: str | None
    eligible_for_issue: bool
    live_issue_write_token_count: int


class ActiveWorkLeaseOut(_StrictModel):
    claimed_at: str
    expires_at: str
    generation: str
    claim_state: Literal["active", "expired"]


class ActiveWorkRunOut(_StrictModel):
    claim_event_id: int | None
    run_id: str | None
    reporting_state: Literal["untagged", "not_reported", "reporting_recently", "stale"]
    last_seen_at: str | None
    evidence_links_available: bool
    age_seconds: int | None
    replay_ready: bool


class ActiveWorkBlockerOut(_StrictModel):
    id: int
    key: str | None
    title: str
    status: str


class ActiveWorkBlockersOut(_StrictModel):
    items: list[ActiveWorkBlockerOut]
    visible_total: int
    clipped: bool


class ClaimHandoffEventOut(_StrictModel):
    event_id: int
    actor_id: int
    actor_name: str
    created_at: str
    run_id: str | None


class OpenClaimHandoffOut(_StrictModel):
    handoff_token: str
    issue_id: int
    lease_generation: str
    schema_version: Literal[1]
    state: Literal["awaiting_resume"]
    reason: Literal["needs_input", "blocked", "capacity"]
    note: str | None
    attempted_work: str
    evidence: list[str]
    blocking_question: str
    resume_instructions: str
    yielded: ClaimHandoffEventOut
    resumed: None
    advisory_untrusted: Literal[True]


AttentionReason = Literal[
    "issue_archived",
    "issue_done",
    "lease_expired",
    "run_untagged",
    "holder_paused",
    "holder_read_only",
    "holder_ineligible",
    "no_live_issue_write_token",
    "checkin_missing",
    "checkin_stale",
    "visible_open_blockers",
    "open_claim_handoff",
]


class ActiveWorkItemOut(_StrictModel):
    issue: ActiveWorkIssueOut
    holder: ActiveWorkHolderOut
    lease: ActiveWorkLeaseOut
    run: ActiveWorkRunOut
    open_blockers: ActiveWorkBlockersOut
    open_claim_handoff: OpenClaimHandoffOut | None
    attention_state: Literal["observed", "needs_attention"]
    attention_reasons: list[AttentionReason]


class ActiveWorkSummaryOut(_StrictModel):
    scope: Literal["returned_items"]
    returned_count: int
    active_claim_count: int
    expired_claim_count: int
    needs_attention_count: int


class ActiveWorkOut(_StrictModel):
    schema_id: Literal["athena.fleet_active_work.v1"] = Field(alias="schema")
    scope: Literal["admin_fleet_view"]
    observed_at: str
    semantics: ActiveWorkSemanticsOut
    items: list[ActiveWorkItemOut]
    visible_total: int
    limit: int
    # Echoed back so a filtered page cannot be mistaken for the whole fleet.
    attention_state: Literal["needs_attention", "observed"] | None = None
    clipped: bool
    # How many rows the attention decision saw. A summary reading "0 need
    # attention" means "none of these did", and on a clipped fleet that is a
    # different statement from "none exist".
    examined_count: int
    summary: ActiveWorkSummaryOut


def _active_work_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    x_athena_actor: str | None = Header(default=None, alias=identity.ACTOR_HEADER),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Require an admin while keeping every credential outcome private."""

    try:
        actor = identity.current_actor(request, authorization, x_athena_actor, conn)
    except HTTPException as exc:
        headers = dict(exc.headers or {})
        headers.update(_PRIVATE_HEADERS)
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from exc
    if not identity.is_admin(actor):
        raise HTTPException(
            status_code=403,
            detail="admin role required",
            headers=_PRIVATE_HEADERS,
        )
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise identity.ScopeDenied(
            actor,
            tokens.ADMIN_SCOPE,
            headers=_PRIVATE_HEADERS,
        )
    return actor


def _query_from_request(request: Request) -> tuple[int | None, int, str | None]:
    try:
        return fleet_work.parse_query_pairs(list(request.query_params.multi_items()))
    except fleet_work.ActiveWorkQueryError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.detail,
            headers=_PRIVATE_HEADERS,
        ) from exc


@router.get("/active-work", response_model=ActiveWorkOut)
def active_work(
    request: Request,
    response: Response,
    _actor: dict = Depends(_active_work_admin),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Join issue claims, exact-run check-ins, blockers, and replay readiness.

    This cross-agent operational view is admin-only.  Its cooperative reporting
    signal never claims that an external process is alive or executing the issue.
    """
    response.headers.update(_PRIVATE_HEADERS)
    agent_id, limit, attention_state = _query_from_request(request)
    return fleet_work.build_active_work(
        conn, agent_id=agent_id, limit=limit, attention_state=attention_state
    )
