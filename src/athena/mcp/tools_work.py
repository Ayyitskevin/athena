"""Issue write, sprint, label, and project tools.

Registered by mcp.server.build_server."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, SkipValidation, ValidationError

from athena.aegis import issue_commands, lease_commands
from athena.aegis.review_models import CandidateSubmission, ReviewCandidateId
from athena.core import tokens
from athena.core.request_models import CoordinationRootSelector
from athena.mcp import results
from athena.mcp.results import ListResult, listing
from athena.mcp.server import (
    ADDS,
    CONDUCTOR,
    ERASES,
    FULL_ONLY,
    KNOWLEDGE,
    READS,
    READ_ONLY,
    SEAT,
    SETS,
    ClaimNextLeaseSeconds,
    ClaimNextLimit,
    DispatchIssueId,
    FleetAgentId,
    ClaimYieldNote,
    HandoffAttemptedWork,
    HandoffBlockingQuestion,
    HandoffEvidence,
    HandoffResumeInstructions,
    HandoffResumeNote,
    HandoffToken,
    EligibleAccountCursor,
    EligibleAccountLimit,
    IdempotencyKey,
    LeaseGeneration,
    ListOffset,
    PermanentDeleteConfirmation,
    ProjectId,
    SprintId,
)

# The completion report's closed sets, published in complete_claim's input
# schema from the command's own constants. Descriptions only: the command
# validates, and refuses every problem at once by field path.
_REPORT_DOCS = lease_commands.report_field_docs()
ReportResults = Annotated[list[dict] | None, Field(description=_REPORT_DOCS["results"])]
ReportEvidence = Annotated[
    list[dict] | None, Field(description=_REPORT_DOCS["evidence"])
]
ReportVerdict = Annotated[str | None, Field(description=_REPORT_DOCS["verdict"])]
ReportFindings = Annotated[
    dict[str, int] | None, Field(description=_REPORT_DOCS["findings"])
]


def register_work_tools(tool, mutation_tool, client) -> None:
    # --- issue writes -------------------------------------------------------

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=ADDS)
    def create_issue(
        title: str,
        body: str = "",
        status: str | None = None,
        priority: str = "medium",
        project_id: int | None = None,
        parent_id: int | None = None,
        assignee_id: int | None = None,
        delegate_ids: list[int] | None = None,
        blocked_by: list[int] | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create an issue. status defaults to the project's own; priority is
        low/medium/high/urgent. Bodies take Markdown and [[issue:N]]/[[page:N]]
        links. The answer adds similar_open: up to three visible open issues in
        the project whose titles resemble it; link or comment rather than
        duplicate.

        parent_id, assignee_id, delegate_ids and blocked_by apply in the same
        call, all or nothing.

        To say what done means, write the body under six headings: ## Goal,
        ## Context, ## Inputs, ## Action, ## Format and ## Success criteria.
        None is required."""
        return client.create_issue(
            title=title,
            body=body,
            status=status,
            priority=priority,
            project_id=project_id,
            parent_id=parent_id,
            assignee_id=assignee_id,
            delegate_ids=delegate_ids,
            blocked_by=blocked_by,
            idempotency_key=idempotency_key,
        )

    # Not destructive (F10): an edit keeps the title and body it replaced, which
    # get_issue_prior_text reads and undo restores. Idempotent: the same call
    # again changes nothing further (with a tag, it is refused 412).
    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def update_issue(
        issue_id: int,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Update an issue; send only the fields to change. status is one of its
        project's (the backlog's: open/in_progress/done); priority is
        low/medium/high/urgent. title or body needs if_match, the issue's
        `_etag`, else 428; the replaced text is kept for undo. For status and
        priority alone if_match is optional.

        A move into a done category may return held_lease, your row as it was
        then; releasing it is a separate complete_claim. A gated close answers
        202 {code: 'approval_required', approval} and is NOT applied: once the
        operator decides, call again with the same arguments and
        idempotency_key. It applies once, or fails approval_rejected."""
        return client.update_issue(
            issue_id,
            title=title,
            body=body,
            status=status,
            priority=priority,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def set_issue_placement(
        issue_id: int,
        project_id: int | None,
        sprint_id: int | None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Atomically set an issue's project and sprint. Always provide BOTH
        project_id and sprint_id: use null project_id to move the issue out of a
        project, and null sprint_id for no sprint. A non-null sprint must belong to
        the supplied project. The pair is validated and committed as one transition,
        so use this tool instead of separate project and sprint moves."""
        return client.set_issue_placement(
            issue_id,
            project_id=project_id,
            sprint_id=sprint_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=CONDUCTOR, hints=SETS)
    def assign_issue(
        issue_id: int,
        assignee_id: int | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Assign an issue to a user id, or pass no assignee_id to unassign it."""
        return client.assign_issue(
            issue_id,
            assignee_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=CONDUCTOR, hints=SETS)
    def delegate_issue(
        issue_id: int,
        agent_user_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> ListResult:
        """Delegate an issue to an agent, added as a contributor; the assignee
        stays accountable. list_eligible_accounts lists the agents that can
        work it; any other id gets one refusal."""
        return listing(
            client.delegate_issue(
                issue_id, agent_user_id, idempotency_key=idempotency_key
            )
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=CONDUCTOR, hints=ADDS)
    def assign_issue_to_seat(
        issue_id: int,
        seat_slug: Annotated[str, Field(max_length=40)],
        note: Annotated[str, Field(max_length=200)] | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Hand an issue to a fleet seat by slug: its agent is delegated the
        issue, made assignee, then radioed the note. A failed radio never undoes
        the assign. A seat whose agent cannot see the issue is refused."""
        return client.assign_issue_to_seat(
            issue_id, seat_slug, note=note, idempotency_key=idempotency_key
        )

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def list_eligible_accounts(
        issue_id: int,
        limit: EligibleAccountLimit = issue_commands.ELIGIBLE_ACCOUNTS_DEFAULT_LIMIT,
        cursor: EligibleAccountCursor | None = None,
    ) -> dict:
        """The accounts that can see and work this issue, by id: {accounts:
        [{id, name, is_agent}], next_cursor, has_more}. assign_issue takes any;
        delegate_issue those with is_agent=true. An issue you cannot see is
        not_found. While has_more, pass next_cursor as cursor."""
        return client.list_eligible_accounts(issue_id, limit=limit, cursor=cursor)

    # An identical request returns its receipt.
    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=CONDUCTOR, hints=SETS)
    def request_candidate_review(
        review_issue_id: DispatchIssueId,
        candidate_id: ReviewCandidateId,
        reviewer_id: FleetAgentId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Ask a reviewer, already assigned or delegated on the review issue
        and able to see the submission, to review one exact candidate. It lands
        on their desk; their report on that issue sets the request's
        reported_verdict in get_project_review_queue. A replacement candidate
        needs a new request."""
        return client.request_candidate_review(
            review_issue_id, candidate_id, reviewer_id, idempotency_key=idempotency_key
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def get_issue_lease(issue_id: int) -> dict | None:
        """Who holds this issue's lease: {holder_id, holder_name, claimed_at,
        expires_at, generation, active, open_claim_handoff}, or {found: false}.
        active=false is an expired, reclaimable lease."""
        return client.get_issue_lease(issue_id)

    # Not idempotent: a repeat with the generation renews the lease.
    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=ADDS)
    def claim_issue(
        issue_id: int,
        if_match: str | None = None,
        generation: LeaseGeneration | None = None,
        lease_seconds: int | None = None,
        paths: list[str] | None = None,
        coordination_root: CoordinationRootSelector | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Claim or renew an issue's lease (30 minutes unless lease_seconds).
        To acquire, omit generation and pass as if_match the issue ETag you
        reviewed (`_etag` from get_issue or `issue_etag` from
        get_issue_work_context, the same value), else 428. To renew one you
        hold, pass your generation (the 428 names it); if_match only with
        paths or coordination_root. Renewals without progress (3 unless the
        operator set it) raise the claim to whoever answers for it; a comment
        on where the work stands resets the count.

        paths (relative POSIX files) are fenced against other active leases as
        my_desk's office.fence says; an overlap refuses 409 fence_overlap. With
        a root catalog, pass the operator's coordination_root. No selector
        leaves the fence unresolved; a stale explicit selector refuses and
        never falls back. Rules: docs/ACTIVE_WORK.md."""
        return client.claim_issue(
            issue_id,
            if_match=if_match,
            generation=generation,
            lease_seconds=lease_seconds,
            paths=paths,
            coordination_root=(
                None if coordination_root is None else coordination_root.model_dump()
            ),
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=ADDS)
    def claim_next_ready(
        limit: ClaimNextLimit = 1,
        lease_seconds: ClaimNextLeaseSeconds | None = None,
        project_id: ProjectId | None = None,
        paths: list[str] | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Atomically claim the next ready issue: delegated first, then
        priority, then oldest; omit project_id for every eligible project plus
        backlog. limit (1-5) bounds candidates, not acquisitions; paths are
        fenced as claim_issue's.

        Returns claimed=true with the lease generation, tags and any
        open_handoff (read the work context, then resume_claim_handoff), or
        claimed=false with reason already_holding (you hold a lease),
        nothing_ready, candidates_conflicted, fence_overlap or
        fence_unresolved. Either carries review_request, a review asked of you
        with its claim_issue call, or null. An issue you completed and a
        needs_input yield wait until someone else acts. A replay answers the
        historical receipt; a fresh poll needs a new key. Rules:
        docs/ATOMIC_PICKUP_SPEC.md."""
        return client.claim_next_ready(
            limit=limit,
            lease_seconds=lease_seconds,
            project_id=project_id,
            paths=paths,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def yield_claim(
        issue_id: int,
        generation: LeaseGeneration,
        reason: lease_commands.ClaimYieldReason,
        attempted_work: HandoffAttemptedWork,
        evidence: HandoffEvidence,
        blocking_question: HandoffBlockingQuestion,
        resume_instructions: HandoffResumeInstructions,
        note: ClaimYieldNote | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Release your active claim with a handoff for whoever resumes it: what
        you attempted, evidence, the blocking question and how to resume. Never
        include secrets: evidence holding a recognized credential is refused.
        Assignment, contributors, status and dependencies stay; a yield never
        claims completion."""
        return client.yield_claim(
            issue_id,
            generation=generation,
            reason=reason,
            attempted_work=attempted_work,
            evidence=evidence,
            blocking_question=blocking_question,
            resume_instructions=resume_instructions,
            note=note,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def resume_claim_handoff(
        issue_id: int,
        handoff_token: HandoffToken,
        generation: LeaseGeneration,
        resume_note: HandoffResumeNote | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Acknowledge an open claim handoff as its current leaseholder, after
        reading it in get_issue_work_context. Resumed means context received,
        not that the blocker was solved."""
        return client.resume_claim_handoff(
            issue_id,
            handoff_token,
            generation=generation,
            resume_note=resume_note,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def complete_claim(
        issue_id: int,
        generation: LeaseGeneration,
        idempotency_key: IdempotencyKey | None = None,
        results: ReportResults = None,
        unfinished: list[str] | None = None,
        uncertainty: list[str] | None = None,
        evidence: ReportEvidence = None,
        verdict: ReportVerdict = None,
        findings: ReportFindings = None,
        usage: dict | None = None,
        candidate_submission: SkipValidation[CandidateSubmission | None] = None,
        review_candidate_id: ReviewCandidateId | None = None,
    ) -> dict:
        """Release your lease. This does NOT close the issue; update_issue does.

        Pass the generation you hold, or a lapsed row's from my_desk's
        work.leases.lapsed. While you hold the issue, an open claim handoff
        answers 409 until you resume it.

        Report arguments are optional and advisory: verdict, findings and
        usage are not verified by Athena; usage is not metered. not_assessed means the condition was not evaluated; for
        example, an integration check was never run. inconclusive means an
        evaluation was attempted but did not establish met or not_met; for
        example, an integration check ran but timed out before producing a
        result. A refused report lists every problem under violations.

        candidate_submission submits a candidate, review_candidate_id reviews
        one; never both. Rules for both: docs/CLOSEOUT_RECIPE.md."""
        # FastMCP includes Pydantic's raw rejected input in tool errors. Keep
        # the public schema and validate here so a rejected source stays private.
        submission = None
        if candidate_submission is not None:
            try:
                submission = CandidateSubmission.model_validate(
                    candidate_submission
                ).model_dump()
            except ValidationError:
                raise ValueError("candidate submission validation error") from None
        return client.complete_claim(
            issue_id,
            generation=generation,
            idempotency_key=idempotency_key,
            results=results,
            unfinished=unfinished,
            uncertainty=uncertainty,
            evidence=evidence,
            verdict=verdict,
            findings=findings,
            usage=usage,
            candidate_submission=submission,
            review_candidate_id=review_candidate_id,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def decline_delegation(
        issue_id: int,
        generation: LeaseGeneration | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> ListResult:
        """Decline an issue delegated to you: leave its contributors, visibly,
        so it can be re-routed. Holding its lease, pass the generation to
        release it too. Answers the remaining contributors; an empty list is
        success."""
        return listing(
            client.decline_delegation(
                issue_id,
                generation=generation,
                idempotency_key=idempotency_key,
            )
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=SEAT, hints=ADDS)
    def comment_on_issue(
        issue_id: int, body: str, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Add a comment to an issue, authored by the token's user."""
        return client.comment_on_issue(issue_id, body, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def archive_issue(
        issue_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Archive (soft-delete) an issue: it's hidden from the default lists but the
        row and its history are kept, and it can be restored. Returns the issue."""
        return client.archive_issue(issue_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def unarchive_issue(
        issue_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Restore a previously archived issue to the active lists. Returns the issue."""
        return client.unarchive_issue(issue_id, idempotency_key=idempotency_key)

    # These fields' previous values are in the trail.
    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def bulk_update_issues(
        ids: list[int],
        status: str | None = None,
        priority: str | None = None,
        assignee_id: int | None = None,
        sprint_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Apply the same change to MANY issues at once (one call instead of N).
        Set any of status (open/in_progress/done), priority (low/medium/high/urgent),
        assignee_id, or sprint_id; only the fields you pass are touched. Best-effort:
        each issue is authorized and validated on its own, so the result reports
        {updated, failed, results:[{id, ok, error}]} — one issue's failure doesn't
        stop the rest. (To CLEAR an assignee or sprint, use the per-issue tools.)"""
        return client.bulk_update_issues(
            ids,
            status=status,
            priority=priority,
            assignee_id=assignee_id,
            sprint_id=sprint_id,
            idempotency_key=idempotency_key,
        )

    # --- hierarchy (epics & sub-tasks) --------------------------------------

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=CONDUCTOR, hints=SETS)
    def set_issue_parent(
        issue_id: int,
        parent_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Nest an issue under a parent (no cycles), or omit parent_id to move
        it to the top level. Returns the issue."""
        return client.set_issue_parent(
            issue_id, parent_id, idempotency_key=idempotency_key
        )

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def list_subtasks(issue_id: int, offset: ListOffset = 0) -> ListResult:
        """List the direct child issues (sub-tasks) nested under this issue."""
        rows = client.list_subtasks(issue_id)
        return results.sliced([results.summary(row) for row in rows], offset=offset)

    # --- dependencies (blocks / relates) ------------------------------------

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_issue_links(issue_id: int) -> dict:
        """Read an issue's dependency links — what it blocks, what blocks it, and
        what it relates to. Returns {blocks, blocked_by, relates}."""
        return client.list_issue_links(issue_id)

    @mutation_tool(
        scope=tokens.ISSUE_WRITE_SCOPE, profiles=(KNOWLEDGE, CONDUCTOR), hints=SETS
    )
    def link_issues(
        issue_id: int,
        target_ref: str,
        relation: str,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Link this issue to another (id or key, 'ATH-15'): relation is
        blocks, blocked_by or relates. Returns its link summary."""
        return client.link_issues(
            issue_id, target_ref, relation, idempotency_key=idempotency_key
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def unlink_issues(
        issue_id: int,
        relation: str,
        target_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Remove a dependency from this issue: the same relation used to add it
        ('blocks'/'blocked_by'/'relates') and the other issue's numeric id."""
        return client.unlink_issues(
            issue_id, relation, target_id, idempotency_key=idempotency_key
        )

    # --- sprints ------------------------------------------------------------

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_sprints(project_id: ProjectId, state: str | None = None) -> ListResult:
        """List a project's sprints, optionally filtered by state
        (planned/active/completed)."""
        return listing(client.list_sprints(project_id, state=state))

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_sprint(sprint_id: SprintId) -> dict:
        """Get one sprint's descriptive fields and lifecycle state. Hidden and
        missing sprints are both reported as not found."""
        return client.get_sprint(sprint_id)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=ADDS)
    def create_sprint(
        project_id: ProjectId,
        name: str,
        goal: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create a planned sprint in a project you created. Optional dates are
        descriptive until the sprint moves through start_sprint and complete_sprint.
        Returns the created sprint."""
        return client.create_sprint(
            project_id,
            name=name,
            goal=goal,
            start_date=start_date,
            end_date=end_date,
            idempotency_key=idempotency_key,
        )

    # Destructive: goal and dates are overwritten, not quoted.
    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def update_sprint(
        sprint_id: SprintId,
        name: str | None = None,
        goal: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        clear_start_date: bool = False,
        clear_end_date: bool = False,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Update a sprint's name, goal, or dates without changing its state.
        Omitted fields stay unchanged; use a clear-date flag to remove that date.
        Supplying a date and its clear flag together is rejected. Sprint edits are
        last-write-wins because the REST sprint surface has no ETag."""
        return client.update_sprint(
            sprint_id,
            name=name,
            goal=goal,
            start_date=start_date,
            end_date=end_date,
            clear_start_date=clear_start_date,
            clear_end_date=clear_end_date,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def start_sprint(
        sprint_id: SprintId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Move a planned sprint to active. A project may have only one active
        sprint; an illegal transition is a conflict. Athena supplies today's date
        when start_date is unset."""
        return client.start_sprint(sprint_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def complete_sprint(
        sprint_id: SprintId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Move an active sprint to completed. Athena supplies today's date when
        end_date is unset. Issues stay associated with the completed sprint."""
        return client.complete_sprint(sprint_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def delete_sprint(
        sprint_id: SprintId,
        confirm_permanent: PermanentDeleteConfirmation,
        idempotency_key: IdempotencyKey | None = None,
    ) -> None:
        """Permanently delete an empty sprint. Set confirm_permanent=true after
        verifying the target; this has no undo and fails with a conflict until every
        issue has been moved to the backlog or another sprint."""
        if not confirm_permanent:
            raise ValueError("confirm_permanent must be true")
        return client.delete_sprint(sprint_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def set_issue_sprint(
        issue_id: int,
        sprint_id: SprintId | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Put an issue into a sprint (which must belong to the issue's own
        project), or pass no sprint_id to move it back to the backlog. Returns the
        updated issue."""
        return client.set_issue_sprint(
            issue_id,
            sprint_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    # --- labels -------------------------------------------------------------

    @tool(scope=READ_ONLY, profiles=KNOWLEDGE)
    def list_labels(offset: ListOffset = 0) -> ListResult:
        """List the shared label vocabulary (id, name, color)."""
        return results.sliced(client.list_labels(), offset=offset)

    # A second label of that name is refused.
    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def create_label(
        name: str,
        color: str = "#6b7280",
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create a label in the shared vocabulary. color is a #RRGGBB hex string.
        Fails if a label with that name already exists (names are case-insensitive)."""
        return client.create_label(name, color=color, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=KNOWLEDGE, hints=SETS)
    def attach_label(
        issue_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Attach a label (id from list_labels) to an issue. Returns the issue."""
        return client.attach_label(issue_id, label_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def detach_label(
        issue_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Remove a label from an issue. Returns the updated issue."""
        return client.detach_label(issue_id, label_id, idempotency_key=idempotency_key)

    # --- projects & users ---------------------------------------------------

    @tool(scope=READ_ONLY, profiles=KNOWLEDGE)
    def list_projects(offset: ListOffset = 0) -> ListResult:
        """List Aegis projects (id, key, name)."""
        return results.sliced(client.list_projects(), offset=offset)

    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def list_users() -> ListResult:
        """List users (id, name, role) — useful for resolving an assignee."""
        return listing(client.list_users())
