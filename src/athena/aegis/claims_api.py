"""REST routes for issue contributors, delegation, and claim leases.

Split out of aegis/api.py. Attaches to the issue router defined there."""

from __future__ import annotations
import sqlite3
from typing import Literal
from fastapi import (
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from athena.aegis import handoff_replies
from athena.core import authorship
from athena.core.authorship import AuthoredBy
from athena.core.packet_time import Timestamp
from athena.core.request_models import CoordinationRootSelector, RequestBody
from athena.aegis import (
    claim_handoffs,
    contributors,
    issue_commands,
    issues,
    lease_commands,
    leases,
    ready_claim_commands,
    review_request_commands,
)
from athena.core import (
    access,
    packet_time,
)
from athena.aegis.review_models import (
    CandidateReviewRequest,
    CandidateReviewRequestReceipt,
)
from athena.core.ids import MAX_SQLITE_INTEGER, RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import current_actor, issue_write_actor, optional_actor

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
from athena.core.refusals import Refusal


@router.post(
    "/{review_issue_id}/review-requests", response_model=CandidateReviewRequestReceipt
)
def request_candidate_review(
    review_issue_id: RowIdPath,
    payload: CandidateReviewRequest,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Request an exact submission review on an existing issue."""
    try:
        receipt = review_request_commands.request_candidate_review(
            conn, actor=actor, review_issue_id=review_issue_id, **payload.model_dump()
        )
    except issue_commands.IssueCommandError as exc:
        error = issue_command_http_error(exc)
        error.headers = PRIVATE_LEASE_HEADERS
        raise error from exc
    response.headers.update(PRIVATE_LEASE_HEADERS)
    return receipt


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
    created_at: Timestamp
    run_id: str | None


class ClaimHandoffResumeEventOut(ClaimHandoffEventOut):
    lease_generation: str
    note: str | None
    # The resume note is the resuming account's words.
    authored_by: AuthoredBy | None = authorship.field()


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
    authored_by: AuthoredBy | None = authorship.field(authorship.HANDOFF_DESCRIPTION)
    replies: list[handoff_replies.HandoffReplyOut] | None = (
        handoff_replies.replies_field()
    )
    replies_total: int | None = handoff_replies.replies_total_field()


class LeaseOut(BaseModel):
    # The exclusive claim: holder, latest acquisition/renewal time, expiry,
    # and whether that window is still open (active=false is an expired, reclaimable lease).
    issue_id: int
    holder_id: int
    holder_name: str
    claimed_at: Timestamp
    expires_at: Timestamp
    # Whole seconds from this answer's clock read to expires_at; 0 once passed.
    expires_in_seconds: int
    generation: str
    active: bool
    declared_paths: list[str] = []
    coordination_root: dict[str, str] | None = None
    # rooted: placed through a current root catalog entry ("same root" basis).
    # "lexical, root unknown": no catalog is configured, so the paths are
    # compared as written under one implicit root (decision D5). unresolved:
    # nonempty but unplaceable (a catalog and no current root, or unreadable).
    path_fence: Literal[
        "rooted", "lexical, root unknown", "unresolved", "issue_only"
    ] = "issue_only"
    open_claim_handoff: ClaimHandoffOut | None = None


class ClaimOut(LeaseOut):
    """A claim's or renewal's answer: the lease, and beside it the issue's
    current ETag. A holder that renews on its generation alone sends no
    If-Match, so this is how it sees the issue changed (closed, archived,
    re-scoped) without another read. It is not part of the lease: the lease
    read does not carry it."""

    issue_etag: str


class RenewOut(ClaimOut):
    """A renewal's answer: the claim's, and how many times this possession has
    been renewed with no work of the holder's recorded on the issue since its
    newest (C7). At ``ATHENA_NO_PROGRESS_RENEWALS`` the claim is raised to the
    accounts that answer for it. Recording work on the issue (a comment, an
    edit, a status change) starts the count again."""

    renewals_without_progress: int


class ClaimIn(RequestBody):
    # How long the lease should hold before it must be renewed. Omitted → the server
    # default. Bounded by the command to [MIN, MAX] lease seconds.
    lease_seconds: int | None = None
    # Omit to acquire a free/expired lease. Supply the current value to renew the
    # same active possession; a supplied stale value never becomes acquisition.
    generation: str | None = None
    # Optional repo-relative POSIX paths this holder intends to touch. Empty or
    # omitted = issue fence only on acquisition; omitted/null retains on renewal.
    # Explicit [] is issue-only. With no root catalog they are compared under one
    # implicit root; with one, a fence without a current root cannot prove
    # separation and conflicts conservatively.
    paths: list[str] | None = None
    # A closed selector; the shared command still resolves it against the
    # operator's catalog; the operator hands selectors out (no read returns one).
    coordination_root: CoordinationRootSelector | None = None


class ReadyClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    limit: int = Field(default=1, ge=1, le=ready_claim_commands.MAX_ATTEMPTS)
    lease_seconds: int | None = Field(
        default=None, ge=leases.MIN_LEASE_SECONDS, le=leases.MAX_LEASE_SECONDS
    )
    project_id: int | None = Field(default=None, ge=1, le=MAX_SQLITE_INTEGER)
    # Relative POSIX paths the claim this pickup makes declares, exactly as
    # claim_issue's `paths`; omitted, null or [] claims issue-only, as before.
    paths: list[str] | None = None


class FenceIssueRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int
    key: str | None


class FenceHolderRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int
    name: str


class FenceRefusalOut(BaseModel):
    """What a fence refusal (fence_overlap or fence_unresolved) names. The other
    lease's issue, holder and path are null unless the caller can read that
    lease; nothing else differs between the two."""

    model_config = ConfigDict(extra="forbid", strict=True)

    basis: Literal["same root", "lexical, root unknown", "unresolved"]
    # The caller's own declared path that overlaps; null for fence_unresolved.
    path: str | None
    issue: FenceIssueRef | None
    holder: FenceHolderRef | None
    holder_path: str | None
    # The newest event on that issue you may read: wait on the issue's events
    # after it to learn when that lease changes. Null with the issue.
    events_after: int | None


class ReadyClaimAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    issue_id: int
    outcome: Literal["conflict"]


class ReadyClaimBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_: Literal["athena.ready_claim.v1"] = Field(alias="schema")
    observed_at: Timestamp
    attempts: list[ReadyClaimAttempt]


class ReadyClaimIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int
    key: str | None
    title: str
    status: str
    priority: str
    created_at: Timestamp


class ReadyClaimPreviousLease(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    holder_id: int
    holder_name: str
    expired_at: Timestamp


class ReadyClaimHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    handoff_token: str
    reason: lease_commands.ClaimYieldReason
    yielded_by: str
    yielded_at: Timestamp
    next: str


class ReadyClaimAcquired(ReadyClaimBase):
    claimed: Literal[True]
    issue: ReadyClaimIssue
    lease: LeaseOut
    issue_etag: str
    work_context_etag: str
    previous_lease: ReadyClaimPreviousLease | None
    open_handoff: ReadyClaimHandoff | None


class ReadyClaimReviewCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    issue_id: int
    if_match: str


class ReadyClaimReviewNext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool: Literal["claim_issue"]
    arguments: ReadyClaimReviewCall
    hint: str


class ReadyClaimReviewRequest(BaseModel):
    """A review the desk asks of this account, which pickup does not claim."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: int
    review_issue_id: int
    candidate_id: int
    source_issue_id: int
    pending_total: int = Field(ge=1)
    next: ReadyClaimReviewNext


class ReadyClaimNothing(ReadyClaimBase):
    claimed: Literal[False]
    # The newest review request naming you that is still waiting, with the
    # call that claims its review issue; null when the desk holds none (D16).
    review_request: ReadyClaimReviewRequest | None


class ReadyClaimHolding(ReadyClaimNothing):
    reason: Literal["already_holding"]
    held_by_you: int = Field(ge=1)


class ReadyClaimEmpty(ReadyClaimNothing):
    reason: Literal["nothing_ready"]
    ready_total: Literal[0]


class ReadyClaimConflicted(ReadyClaimNothing):
    reason: Literal["candidates_conflicted"]


class ReadyClaimFenced(ReadyClaimNothing):
    """The paths this pickup declares overlap, or cannot be placed beside,
    another active lease's fence. That verdict is the same for every candidate,
    so pickup stops at the first: ``attempts`` names it and ``fence`` is what a
    claim_issue refusal with the same code would carry."""

    reason: Literal["fence_overlap", "fence_unresolved"]
    fence: FenceRefusalOut


@router.post(
    "/claim-next",
    response_model=ReadyClaimAcquired
    | ReadyClaimHolding
    | ReadyClaimEmpty
    | ReadyClaimConflicted
    | ReadyClaimFenced,
)
def claim_next_ready(
    request: Request,
    response: Response,
    payload: ReadyClaimIn | None = None,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    """Select and acquire current ready work atomically for this account."""
    if "if-match" in request.headers:
        raise Refusal(
            status_code=422,
            detail="If-Match is not supported for ready pickup; use an explicit issue claim",
            headers=PRIVATE_LEASE_HEADERS,
            code="validation_error",
        )
    response.headers.update(PRIVATE_LEASE_HEADERS)
    payload = payload or ReadyClaimIn()
    try:
        result = ready_claim_commands.claim_next_ready(
            conn, actor=actor, **payload.model_dump()
        )
        # The lease's seconds count from the pickup's own clock read.
        return packet_time.stamp(result, now=packet_time.parse(result["observed_at"]))
    except issue_commands.IssueCommandError as exc:
        try:
            error_response = issue_command_error_response(exc)
        except HTTPException as http_error:
            http_error.headers = {**PRIVATE_LEASE_HEADERS, **(http_error.headers or {})}
            raise
        error_response.headers.update(PRIVATE_LEASE_HEADERS)
        return error_response


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
    # A string, or (AD9, D31) a completion evidence object the command checks
    # and stores as one line.
    evidence: list[HandoffEvidenceItem | dict[str, object]] = Field(
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


class ContributorAdd(RequestBody):
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


class EligibleAccountOut(BaseModel):
    """An account that can see the issue and that assignment takes (delegation
    too, when it is an agent). Only these three fields: no email, role, token or
    pause state (D19)."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    # Delegation takes an agent; assignment takes every row.
    is_agent: bool


class EligibleAccountsOut(BaseModel):
    """One page of eligible accounts, paged as every new read is (D2): send
    `next_cursor` back as `cursor` while `has_more` is true."""

    model_config = ConfigDict(extra="forbid")

    accounts: list[EligibleAccountOut]
    # Opaque: pass it back unchanged. Null on the last page.
    next_cursor: str | None
    has_more: bool


def _eligible_accounts_cursor(cursor: str | None) -> int:
    """The account id an opaque cursor stands for. A value this read did not
    hand out is refused, never read as the first page."""
    if cursor is None:
        return 0
    if not cursor.isascii() or not cursor.isdigit() or int(cursor) > MAX_SQLITE_INTEGER:
        raise Refusal(
            status_code=422,
            detail="cursor is not one this read returned; omit it for the first page",
            code="validation_error",
        )
    return int(cursor)


@router.get("/{issue_id}/eligible-accounts", response_model=EligibleAccountsOut)
def list_eligible_accounts(
    issue_id: RowIdPath,
    limit: int = Query(
        issue_commands.ELIGIBLE_ACCOUNTS_DEFAULT_LIMIT,
        ge=1,
        le=issue_commands.ELIGIBLE_ACCOUNTS_MAX_LIMIT,
    ),
    cursor: str | None = Query(
        None,
        max_length=32,
        description="the previous page's next_cursor, unchanged; omit for the first page",
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """The accounts that can see and work this issue, ordered by id: `id`,
    `name` and `is_agent`. Every row is one assignment takes, and every agent
    row one delegation takes; whether the caller may itself assign or delegate
    this issue is the write's own check. The caller must see the issue and hold
    a write role: an issue it cannot see is 404, the same as one that does not
    exist. While `has_more` is true, send `next_cursor` back as `cursor`."""
    after_id = _eligible_accounts_cursor(cursor)
    try:
        rows, has_more = issue_commands.eligible_accounts(
            conn, actor=actor, issue_id=issue_id, limit=limit, after_id=after_id
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    return {
        "accounts": rows,
        "next_cursor": str(rows[-1]["id"]) if has_more else None,
        "has_more": has_more,
    }


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
        raise Refusal(status_code=404, detail="no such issue", code="not_found")
    response.headers.update(PRIVATE_LEASE_HEADERS)
    # One clock read: `active` and `expires_in_seconds` are the same verdict.
    now = packet_time.clock()
    lease = leases.get_lease(conn, issue_id, now=now)
    if lease is None:
        return None
    lease["open_claim_handoff"] = claim_handoffs.get_open_handoff(conn, issue_id)
    return packet_time.stamp(lease, now=now)


@router.post(
    "/{issue_id}/claim",
    # A renewal's answer carries one more field; RenewOut is tried first.
    response_model=RenewOut | ClaimOut,
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
        lease = lease_commands.claim_issue(
            conn,
            actor=actor,
            issue_id=issue_id,
            if_match=if_match_values(request),
            generation=payload.generation if payload else None,
            paths=payload.paths if payload else None,
            coordination_root=(
                payload.coordination_root.model_dump()
                if payload and payload.coordination_root is not None
                else None
            ),
            **kwargs,
        )
        # The acquisition's clock read is claimed_at, the instant expires_at
        # was set from: the seconds are the lease's whole length.
        return packet_time.stamp(lease, now=packet_time.parse(lease["claimed_at"]))
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
