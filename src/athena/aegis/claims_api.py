"""REST routes for issue contributors, delegation, and claim leases.

Split out of aegis/api.py. Attaches to the issue router defined there."""

from __future__ import annotations
import sqlite3
from typing import Literal
from fastapi import (
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from athena.aegis import (
    claim_handoffs,
    contributors,
    issue_commands,
    issues,
    lease_commands,
    leases,
)
from athena.core import (
    access,
)
from athena.core.ids import RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import issue_write_actor, optional_actor

from athena.aegis.api import (
    HandoffEvidenceItem,
    LeaseGenerationIn,
    router,
)
from athena.aegis.rest_support import (
    CLAIM_IF_MATCH_OPENAPI,
    PRIVATE_LEASE_HEADERS,
    if_match_values,
    issue_command_error_response,
    issue_command_http_error,
    issue_for_read,
)


class ContributorOut(BaseModel):
    user_id: int
    name: str
    is_agent: bool = False
    added_by: int
    added_at: str


class ClaimHandoffEventOut(BaseModel):
    event_id: int
    actor_id: int
    actor_name: str
    created_at: str
    run_id: str | None


class ClaimHandoffResumeEventOut(ClaimHandoffEventOut):
    lease_generation: str
    note: str | None


class ClaimHandoffOut(BaseModel):
    handoff_token: str
    issue_id: int
    lease_generation: str
    schema_version: Literal[1]
    state: Literal["awaiting_resume", "resumed"]
    reason: lease_commands.ClaimYieldReason
    note: str | None
    attempted_work: str
    evidence: list[str]
    blocking_question: str
    resume_instructions: str
    yielded: ClaimHandoffEventOut
    resumed: ClaimHandoffResumeEventOut | None
    advisory_untrusted: Literal[True]


class LeaseOut(BaseModel):
    # The exclusive claim on an issue: who holds it, when it was taken, when it expires,
    # and whether that window is still open (active=false is an expired, reclaimable lease).
    issue_id: int
    holder_id: int
    holder_name: str
    claimed_at: str
    expires_at: str
    generation: str
    active: bool
    declared_paths: list[str] = []
    open_claim_handoff: ClaimHandoffOut | None = None


class ClaimIn(BaseModel):
    # How long the lease should hold before it must be renewed. Omitted → the server
    # default. Bounded by the command to [MIN, MAX] lease seconds.
    lease_seconds: int | None = None
    # Omit to acquire a free/expired lease. Supply the current value to renew the
    # same active possession; a supplied stale value never becomes acquisition.
    generation: str | None = None
    # Optional repo-relative POSIX paths this holder intends to touch. Empty or
    # omitted = issue fence only. Overlap with another active lease is 409.
    paths: list[str] | None = None


class YieldClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Optional at the transport boundary so the command can return the stable
    # 428 lease_generation_required error instead of a generic validation error.
    generation: str | None = None
    reason: lease_commands.ClaimYieldReason
    note: str | None = Field(
        default=None,
        max_length=lease_commands.MAX_CLAIM_YIELD_NOTE_CHARS,
    )
    attempted_work: str = Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_ATTEMPTED_WORK_CHARS,
    )
    evidence: list[HandoffEvidenceItem] = Field(
        max_length=lease_commands.MAX_HANDOFF_EVIDENCE_ITEMS
    )
    blocking_question: str = Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_BLOCKING_QUESTION_CHARS,
    )
    resume_instructions: str = Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS,
    )


class ResumeClaimHandoffIn(LeaseGenerationIn):
    resume_note: str | None = Field(
        default=None,
        max_length=lease_commands.MAX_HANDOFF_RESUME_NOTE_CHARS,
    )


class DeclineDelegationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Required only when decline would release this actor's active lease.
    generation: str | None = None


class ContributorAdd(BaseModel):
    user_id: int


@router.get("/{issue_id}/contributors", response_model=list[ContributorOut])
def list_issue_contributors(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like listing comments/labels. 404 if the issue is missing or not
    # visible.
    issue_for_read(conn, issue_id, actor)
    return contributors.list_contributors(conn, issue_id)


@router.post(
    "/{issue_id}/contributors", response_model=list[ContributorOut], status_code=201
)
def add_issue_contributor(
    issue_id: RowIdPath,
    payload: ContributorAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # The command owns the add, its auto-watch, and its atomic 'added_contributor'
    # event under the same write gate. Idempotent: re-add records nothing.
    try:
        return issue_commands.add_contributor(
            conn, actor=actor, issue_id=issue_id, user_id=payload.user_id
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc


@router.post(
    "/{issue_id}/delegate", response_model=list[ContributorOut], status_code=201
)
def delegate_issue_to_agent(
    issue_id: RowIdPath,
    payload: ContributorAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Agent delegation is the explicit agent-as-teammate path: the human assignee stays
    # accountable; the target agent is added as a contributor and receives a distinct
    # 'delegated' event. The command owns the add + atomic event; require_agent gates
    # the target to an agent. Generic contributor add remains available for humans.
    try:
        return issue_commands.add_contributor(
            conn,
            actor=actor,
            issue_id=issue_id,
            user_id=payload.user_id,
            require_agent=True,
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc


@router.delete(
    "/{issue_id}/contributors/{user_id}", response_model=list[ContributorOut]
)
def remove_issue_contributor(
    issue_id: RowIdPath,
    user_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    try:
        return issue_commands.remove_contributor(
            conn, actor=actor, issue_id=issue_id, user_id=user_id
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc


@router.get("/{issue_id}/lease", response_model=LeaseOut | None)
def get_issue_lease(
    issue_id: RowIdPath,
    response: Response,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | None:
    # Who holds this issue right now (or null if unclaimed) — so an agent can see it is
    # taken before trying to claim. 404 if the issue is missing or hidden, like the other
    # sub-resource reads; the lease itself carries `active` (an expired lease reads
    # active=false rather than vanishing, so the last holder is still visible).
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise HTTPException(status_code=404, detail="no such issue")
    response.headers.update(PRIVATE_LEASE_HEADERS)
    lease = leases.get_lease(conn, issue_id)
    if lease is not None:
        lease["open_claim_handoff"] = claim_handoffs.get_open_handoff(conn, issue_id)
    return lease


@router.post(
    "/{issue_id}/claim",
    response_model=LeaseOut,
    status_code=201,
    openapi_extra=CLAIM_IF_MATCH_OPENAPI,
)
def claim_issue(
    issue_id: RowIdPath,
    request: Request,
    response: Response,
    payload: ClaimIn | None = None,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Accept only against the exact root issue revision the claimant reviewed.
    # The command checks the raw If-Match under the lease's write transaction.
    lease_seconds = payload.lease_seconds if payload else None
    response.headers.update(PRIVATE_LEASE_HEADERS)
    kwargs = {} if lease_seconds is None else {"lease_seconds": lease_seconds}
    try:
        return lease_commands.claim_issue(
            conn,
            actor=actor,
            issue_id=issue_id,
            if_match=if_match_values(request),
            generation=payload.generation if payload else None,
            paths=payload.paths if payload else None,
            **kwargs,
        )
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)


@router.post(
    "/{issue_id}/yield",
    response_model=ClaimHandoffOut,
    status_code=201,
)
def yield_issue_claim(
    issue_id: RowIdPath,
    payload: YieldClaimIn,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # Yield is a truthful holder action, not completion or reassignment. The
    # command atomically releases the lease and records the run-stamped reason.
    try:
        handoff = lease_commands.yield_claim(
            conn,
            actor=actor,
            issue_id=issue_id,
            generation=payload.generation,
            reason=payload.reason,
            note=payload.note,
            attempted_work=payload.attempted_work,
            evidence=payload.evidence,
            blocking_question=payload.blocking_question,
            resume_instructions=payload.resume_instructions,
        )
        response.headers.update(PRIVATE_LEASE_HEADERS)
        return handoff
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)


@router.post(
    "/{issue_id}/claim-handoffs/{handoff_token}/resume",
    response_model=ClaimHandoffOut,
)
def resume_issue_claim_handoff(
    issue_id: RowIdPath,
    handoff_token: str,
    payload: ResumeClaimHandoffIn,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    try:
        handoff = lease_commands.resume_claim_handoff(
            conn,
            actor=actor,
            issue_id=issue_id,
            handoff_token=handoff_token,
            generation=payload.generation,
            resume_note=payload.resume_note,
        )
        response.headers.update(PRIVATE_LEASE_HEADERS)
        return handoff
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)


@router.post("/{issue_id}/decline", response_model=list[ContributorOut])
def decline_issue_delegation(
    issue_id: RowIdPath,
    response: Response,
    payload: DeclineDelegationIn | None = None,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict] | JSONResponse:
    # Decline: remove yourself from the contributor set so the work is visibly refused, not
    # silently dropped. 404 if you weren't a delegated contributor. Returns the remaining
    # contributors; any lease you held is released with the same act.
    try:
        result = lease_commands.decline_delegation(
            conn,
            actor=actor,
            issue_id=issue_id,
            generation=payload.generation if payload else None,
        )
        response.headers.update(PRIVATE_LEASE_HEADERS)
        return result
    except issue_commands.IssueCommandError as exc:
        return issue_command_error_response(exc)
