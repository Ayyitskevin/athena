"""Agent-control and admin tools.

Registered by mcp.server.build_server."""

from __future__ import annotations

from typing import Literal

from athena.aegis.review_models import ReviewCandidateId
from athena.core import dispatch, run_controls, security_events, tokens
from athena.mcp import results
from athena.mcp.results import ListResult, listing, window
from athena.mcp.server import (
    ADDS,
    ANY_WRITE_SCOPE,
    CONDUCTOR,
    ERASES,
    FULL_ONLY,
    KNOWLEDGE,
    READS,
    READ_ONLY,
    SEAT,
    SETS,
    AnswerSection,
    DispatchIssueId,
    DispatchLimit,
    IdempotencyKey,
    IndeterminateKeyLimit,
    ListCursor,
    ListOffset,
    OnboardProfile,
    ProjectBriefLimit,
    ProjectId,
    ProjectReviewQueueLimit,
    RunControlId,
    RunControlKind,
    RunControlListLimit,
    RunControlPayload,
    RunControlStateFilter,
    RunControlSummary,
    RunControlTtl,
    RunId,
)

# The REST caps of the routes that page only by limit.
WORKER_LIST_CAP = 500
APPROVAL_LIST_CAP = 200


def register_admin_tools(tool, mutation_tool, client) -> None:
    # --- agent control (admin) ---------------------------------------------

    # The same email (or name-derived email) is refused.
    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def onboard_agent(
        name: str,
        scopes: list[str],
        email: str | None = None,
        token_name: str | None = None,
        profile: OnboardProfile | None = None,
    ) -> dict:
        """Admin: provision a NEW agent teammate in one audited move — create its
        user account (member role, token-only) and mint its first scoped token.
        Scopes are required (least privilege: e.g. ["read", "issue:write"]).
        Email is optional; omitted becomes {slug}@agents.local. Returns the user,
        the one-time raw token, and a ready-to-paste MCP config. profile is the
        ATHENA_MCP_PROFILE that config prints (seat, seat+knowledge or
        conductor; omitted, the seat profile the scopes suggest). It grants
        nothing, and a profile the scopes cannot use is refused. Requires admin."""
        return client.onboard_agent(
            name=name,
            email=email,
            scopes=scopes,
            token_name=token_name,
            profile=profile,
        )

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def pause_agent(user_id: int, release_leases: bool = False) -> dict:
        """Admin: PAUSE user_id — every authenticated action it attempts is
        refused until resumed, but nothing is destroyed (tokens and sessions
        stay intact). The lever to reach for BEFORE the kill switch when an
        agent looks off-course. Audited. Requires an admin token. Returns its
        active_leases; release_leases=true also releases each one, recorded
        per lease as lease_released."""
        return client.set_user_paused(user_id, True, release_leases=release_leases)

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def resume_agent(user_id: int) -> dict:
        """Admin: RESUME a paused user_id — restores the account exactly as it
        was before the pause. Audited. Requires an admin token."""
        return client.set_user_paused(user_id, False)

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def revoke_agent_tokens(user_id: int, release_leases: bool = False) -> dict:
        """Admin kill switch: revoke EVERY live API token held by user_id — the
        lever to immediately stop a compromised or runaway agent. Idempotent and
        audited; returns {user_id, revoked_token_count, active_leases}.
        release_leases=true also releases each active lease, recorded per lease
        as lease_released. Requires an admin token."""
        return client.revoke_agent_tokens(user_id, release_leases=release_leases)

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def offboard_agent(user_id: int, release_leases: bool = False) -> dict:
        """Admin one-click offboard: demote user_id to viewer, revoke every session,
        and revoke every token — one audited lockout. Refuses to strip the last
        admin. Returns the counts revoked and its active_leases;
        release_leases=true also releases each one, recorded per lease as
        lease_released. Requires an admin token."""
        return client.offboard_user(user_id, release_leases=release_leases)

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=ADDS)
    def dispatch_to_icarus(
        issue_id: DispatchIssueId,
        repo: str,
        base_commit: str,
        capability: Literal["repo.edit", "ci.run"],
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Hand an issue to the external execution fleet.

        Athena is the control plane; the executor is a separate system with its own
        store. This records that Athena ASKED, under a policy digest of the
        authorization in force, and then hands the envelope over. It does not run
        anything itself and cannot see what happens next.

        Read `state` carefully — it is Athena's knowledge, not the executor's
        progress. 'accepted' means the executor said it accepted; it never means
        work is running. 'undeliverable' means Athena could not hand it over at all.
        Evidence and completion arrive later as opaque references via the
        executor's signed callback.

        Metered and gated like any other write: it spends a budget action, and
        dispatch has its own approval kind ('dispatch.request') an operator can
        gate independently of issue.close. Requires the issue:write scope and a
        configured executor."""
        return client.dispatch_to_icarus(
            issue_id,
            repo=repo,
            base_commit=base_commit,
            capability=capability,
            idempotency_key=idempotency_key,
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_dispatches(
        work_item_id: DispatchIssueId | None = None,
        state: str | None = None,
        limit: DispatchLimit = 50,
        offset: ListOffset = 0,
    ) -> ListResult:
        """What Athena has handed to the executor, newest first. Filter by
        work_item_id or by state ('pending_delivery', 'accepted', 'undeliverable',
        'completed', 'failed'). Each state is what Athena was told, not what is
        happening on the far side."""
        return window(
            lambda rows: client.list_dispatches(
                work_item_id=work_item_id, state=state, limit=rows
            ),
            limit=limit,
            offset=offset,
            cap=dispatch.MAX_LIST_LIMIT,
        )

    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=KNOWLEDGE, hints=ADDS)
    def record_run_learning(
        issue_id: int,
        summary: str,
        run_id: str | None = None,
        space_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Write down what you learned, so the NEXT run starts knowing it: what
        you tried, what the evidence showed, what resolved it, what is still
        unknown. Appends it, quoted and attributed to you, to the issue's
        runbook page, which the next agent's work context carries. run_id
        attributes it to a run you can see. space_id is needed only for the
        first entry; `get_issue_runbook` says whether one exists."""
        return client.record_run_learning(
            issue_id,
            summary=summary,
            run_id=run_id,
            space_id=space_id,
            idempotency_key=idempotency_key,
        )

    @tool(scope=READ_ONLY, profiles=KNOWLEDGE)
    def get_issue_runbook(issue_id: int) -> dict | None:
        """The issue's runbook page of earlier runs' learnings, or {found:
        false}. Read it before work; add with `record_run_learning`."""
        return client.get_issue_runbook(issue_id)

    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def list_security_events(
        verb: str | None = None,
        since: str | None = None,
        limit: int = 50,
        offset: ListOffset = 0,
    ) -> ListResult:
        """Recent boundary REFUSALS — someone probing where they may not go.

        Covers failed logins, revoked tokens still being presented, scope denials,
        and paused accounts that keep trying. Each names the account whose boundary
        was hit. Narrow with verb ('login_failed', 'revoked_token_used',
        'scope_denied', 'paused_account_refused') and since (a timestamp as
        any packet gives it, '2026-11-21T18:19:13Z'). Requires an admin token."""
        return window(
            lambda rows: client.list_security_events(
                verb=verb, since=since, limit=rows
            ),
            limit=limit,
            offset=offset,
            cap=security_events.MAX_LIST_LIMIT,
        )

    @mutation_tool(scope=tokens.ISSUE_WRITE_SCOPE, profiles=FULL_ONLY, hints=ADDS)
    def start_playbook(
        page_id: int,
        project_id: int | None = None,
        title: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Turn a playbook page's checklist into REAL WORK: one parent issue
        plus one child per unchecked `- [ ]` step. Indented steps NEST — the
        checklist's shape comes back as the issue tree, each indented step a
        child of the issue its enclosing step became.

        The page must carry the `playbook` label. Checked (`- [x]`) steps are
        counted and skipped, never created — ticking a box before starting is
        the author saying it is already done. A checked step's unchecked
        sub-steps still become work, attached to the nearest created ancestor.

        Every created issue cites the page with a `[[page:N]]` wikilink, so the
        page's backlinks show the work it started and a `rollup` embed there
        counts its progress, with no extra step from you.

        A TEMPLATE IS NOT A LIVE MIRROR: this snapshots the page. Editing it
        afterwards changes nothing already created, and starting it again makes
        a second independent instantiation (which is what templates are for).
        Pass idempotency_key if you need a retry to be safe."""
        return client.start_playbook(
            page_id,
            project_id=project_id,
            title=title,
            idempotency_key=idempotency_key,
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def my_desk(
        section: AnswerSection | None = None,
        offset: ListOffset = 0,
        explain: bool = False,
    ) -> dict:
        """START HERE. Your desk in one bounded read: `identity` (role,
        scopes, budget, approvals needed), `asks` (run controls,
        kill requests, handoffs to acknowledge), `work` (delegations, your
        leases; work.leases.lapsed are expired rows only complete_claim
        settles), `signals` (unread notifications, events past your cursor).
        RESERVES NOTHING: seeing work here does not claim it.

        The loop: `my_desk()` -> `claim_next_ready()` -> act ->
        `complete_claim(...)` -> `wait_for_events(after=<cursor>, wait=0)` ->
        `advance_desk_cursor(after_id=<last handled id>)`. Your tools
        follow ATHENA_MCP_PROFILE (athena://docs/boot-block); the operator
        widens it. section and offset read on where `_truncated` cut.
        explain=true inlines athena://answers/glossary."""
        desk = client.my_desk() if explain else client.my_desk(lean=True)
        for item in desk.get("work", {}).get("delegations", {}).get("items", []):
            item["issue"] = results.summary(item["issue"])
        if section is None:
            return desk
        return results.section(desk, section, offset)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def my_office() -> dict:
        """Your cubicle: active leases, declared paths, and a checkout branch hint.
        At most one active lease is workflow guidance, not a server quota. START HERE if you
        already know who you are and only need the job.

        `seated` is true only when you hold exactly one active lease. `chair`
        names the issue, generation, declared_paths, and `checkout_hint`
        (a branch name — Athena does not create git remotes). `next_to_sit`
        is the first delegated issue when you are standing.

        `fence.basis` says how declared paths are compared: "lexical, root
        unknown" with no root catalog, "same root" with one.

        RESERVES NOTHING. Claim through claim_issue. complete_claim stands
        you up; it does not close the issue."""
        return client.my_office()

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def get_project_floor(
        project_id: int, section: AnswerSection | None = None, offset: ListOffset = 0
    ) -> dict:
        """A project's floor: each open issue a chair, occupied by an agent
        (with any fenced paths) or empty; `blocked_by` lists open blockers.
        RESERVES NOTHING. section and offset read on where `_truncated` cut."""
        floor = client.get_project_floor(project_id)
        if section is None:
            return floor
        return results.section(floor, section, offset)

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def get_project_brief(project_id: ProjectId, limit: ProjectBriefLimit = 5) -> dict:
        """A compact project snapshot: summary, active claims, blocker edges,
        handoffs awaiting resume and latest completion reports, `limit` per
        section; counts disclose clipping. Supplies no claim ETag: read the
        work context before claiming. Reading it: docs/CONDUCTOR_RECIPE.md."""
        return client.get_project_brief(project_id, limit=limit)

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def get_project_review_queue(
        project_id: ProjectId,
        limit: ProjectReviewQueueLimit = 5,
        cursor: ReviewCandidateId | None = None,
    ) -> dict:
        """A project's current candidates with their exact-version reports;
        follow next_cursor as cursor until null for older ones. review_state
        follows any account's newest review; to gate on a review you
        requested, read reported_verdict on your row in review_requests.items[]
        (match reviewer_id), which only that reviewer's report sets. Reports
        are advisory, not acceptance. Reading it: docs/CONDUCTOR_RECIPE.md."""
        return client.get_project_review_queue(project_id, limit=limit, cursor=cursor)

    # An acknowledgement moves a marker forward; what it acknowledges stays
    # readable.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def advance_desk_cursor(after_id: int) -> dict:
        """Record that you have handled every visible event up to `after_id`:
        the last event you processed, not the newest you saw. Forward only:
        the same id again is a no-op, a lower one 409, one past the newest
        event 422 `cursor_ahead`."""
        return client.advance_desk_cursor(after_id=after_id)

    # An admin read: the route carries the admin dependency
    # (core/activity_api.py).
    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def activity_chain_status() -> dict:
        """Where the audit trail's hash chain stands (admin only).

        Returns the ANCHOR (the first chained event — rows below it predate the
        chain and are counted, never claimed), the HEAD (the newest entry's
        hash — note it somewhere outside Athena to make even a full-chain
        rebuild detectable), and coverage counts. The chain proves the recorded
        trail has not been rewritten; it does not prove what an agent's process
        actually did off the record."""
        return client.activity_chain_status()

    # An admin read: the route carries the admin dependency
    # (core/activity_api.py).
    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def verify_activity_chain(
        after_id: int | None = None,
        limit: int = 1000,
    ) -> dict:
        """Recompute a bounded window of the audit trail's hash chain (admin only).

        Each call rehashes at most `limit` entries; loop on the returned
        `next_after` until `has_more` is false to walk the whole chain. On a
        break it reports the FIRST mismatching event id and reason — a finding
        to investigate, never something Athena repairs. `ok: true` means the
        WINDOW verified, not the whole trail."""
        return client.verify_activity_chain(after_id=after_id, limit=limit)

    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def agent_answerability(agent_id: int | None = None) -> dict:
        """The per-agent ask-and-answer ledger (admin only).

        For each agent: run controls addressed to it (open / expired-unanswered
        / completed / declined), kill requests to its workers (told-to-stop vs
        confirmed), approvals its gated actions raised (pending / approved /
        rejected), and how many of its events an undo reversed. Facts per lane,
        derived at read time from the owning tables — deliberately NOT a score:
        an expired control means the clock ran out, and only the operator can
        judge why. Narrow with agent_id."""
        return client.agent_answerability(agent_id=agent_id)

    # Liveness: every call refreshes last-seen.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=FULL_ONLY, hints=ADDS)
    def worker_heartbeat(
        worker_key: str,
        node_label: str | None = None,
        capabilities: list[str] | None = None,
        state: str = "running",
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Register or refresh YOUR worker process, and find out whether the
        operator asked you to stop.

        Call this on a timer with a stable worker_key (one per process). The reply
        carries `kill_requested`: when it is true, the operator wants this worker
        to shut down. Athena CANNOT signal your process — asking is the whole
        mechanism, so honoring it is your job. Heartbeat with state='stopping' to
        confirm you heard, then state='stopped' when you have finished.

        node_label says where you run and capabilities what you can do; both are
        yours to declare and Athena never routes or authorizes on either. A
        heartbeat proves you REPORTED, never that you are alive: going quiet reads
        as stale, not stopped."""
        return client.worker_heartbeat(
            worker_key=worker_key,
            node_label=node_label,
            capabilities=capabilities,
            state=state,
            idempotency_key=idempotency_key,
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_workers(
        agent_id: int | None = None, limit: int = 100, offset: ListOffset = 0
    ) -> ListResult:
        """The worker registry — which agent processes are reporting, on what node,
        with what capabilities, and which were asked to stop. Admins see the whole
        fleet; anyone else sees only their own workers. `reporting_state` is
        cooperative presence, not process liveness."""
        return window(
            lambda rows: client.list_workers(agent_id=agent_id, limit=rows),
            limit=limit,
            offset=offset,
            cap=WORKER_LIST_CAP,
        )

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def request_worker_kill(
        worker_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Admin: ask a worker to stop.

        This RECORDS AN INSTRUCTION; it does not end a process. The worker learns
        of it on its next heartbeat and is expected to honor it. `kill_state`
        reports only what has actually been said: 'requested' until the worker
        acknowledges, 'acknowledged_but_reporting' if it heard and kept running.
        A worker that goes silent is stale, never 'terminated'. Requires an admin
        token."""
        return client.request_worker_kill(worker_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def cancel_worker_kill(
        worker_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Admin: withdraw a kill request the worker has not acknowledged yet.
        Refused once acknowledged — it may already be shutting down. Requires an
        admin token."""
        return client.cancel_worker_kill(worker_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=ADDS)
    def create_run_control(
        run_id: RunId,
        kind: RunControlKind,
        payload: RunControlPayload | None = None,
        worker_id: int | None = None,
        ttl_seconds: RunControlTtl | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Admin: record a control request against a live run.

        'steer' hands the run's agent bounded guidance (payload required);
        'request_cancel' asks it to wind this run down cooperatively;
        'request_fresh_context' asks it to close out with a structured handoff a
        fresh context can continue from. This RECORDS A REQUEST; it does not
        change what any process is doing. Only the agent bound to the run can
        read and settle it, and an unanswered request reads as expired after
        ttl_seconds (default one hour). Requires an admin token."""
        return client.create_run_control(
            run_id=run_id,
            kind=kind,
            payload=payload,
            worker_id=worker_id,
            ttl_seconds=ttl_seconds,
            idempotency_key=idempotency_key,
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def list_run_controls(
        run_id: str | None = None,
        state: RunControlStateFilter | None = None,
        limit: RunControlListLimit = 50,
        offset: ListOffset = 0,
    ) -> ListResult:
        """Operator requests on runs addressed to you, and your answers.
        state='open' is your inbox of unsettled ones: answer what you find.
        'expired' means the clock ran out, never that anything stopped."""
        return window(
            lambda rows: client.list_run_controls(
                run_id=run_id, state=state, limit=rows
            ),
            limit=limit,
            offset=offset,
            cap=run_controls.MAX_LIST_LIMIT,
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_run_control(control_id: RunControlId) -> dict:
        """One run control, for an admin or the agent it is addressed to."""
        return client.get_run_control(control_id)

    # An acknowledgement moves a marker forward; the control stays readable.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def acknowledge_run_control(
        control_id: RunControlId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Record that you read a control addressed to your run: a receipt
        only; answer it with decline_run_control or complete_run_control.
        Refused once it is settled or expired."""
        return client.acknowledge_run_control(
            control_id, idempotency_key=idempotency_key
        )

    # A settled control refuses a repeat.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def decline_run_control(
        control_id: RunControlId,
        reason: RunControlSummary,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Decline a control addressed to your run, with the reason the operator
        will read."""
        return client.decline_run_control(
            control_id, reason=reason, idempotency_key=idempotency_key
        )

    # An acknowledgement moves a marker forward; the control stays readable.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def complete_run_control(
        control_id: RunControlId,
        summary: RunControlSummary | None = None,
        handoff: dict | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Complete a control addressed to your run: for steer and
        request_cancel, a summary of what you did; for request_fresh_context,
        handoff = {summary, unresolved_questions, athena_refs, evidence_refs},
        short strings, never transcripts. Recorded as your claim."""
        return client.complete_run_control(
            control_id,
            summary=summary,
            handoff=handoff,
            idempotency_key=idempotency_key,
        )

    # Inverses are reversible; a repeat is refused.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def undo_action(
        event_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Undo one activity event by applying its registered inverse.

        History is append-only, so this NEVER edits or deletes the original event:
        it runs the inverse as a new, fully audited action whose event points back
        at the one it reversed. You act as yourself — the same role, scope, and
        visibility rules apply as if you had made the change by hand, so undo is
        not a way to reach someone else's write.

        Reversible today: issue archive/unarchive and label/unlabel, an issue's
        title and body edit (refused once a later edit replaced that text), page
        archive/unarchive and label/unlabel. Everything else is refused with a
        reason: a comment or an attachment is one-way (delete it explicitly
        instead), a destroyed row is a trapdoor. Refused too when the event was
        already undone, was imported from another system, or when its effect is no
        longer in force (someone already changed it back). Find event ids with
        `recent_events(...)`."""
        return client.undo_action(event_id, idempotency_key=idempotency_key)

    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def list_approvals(
        state: str | None = None, limit: int = 100, offset: ListOffset = 0
    ) -> ListResult:
        """The operator's approval queue — actions an agent asked to take that are
        waiting on a human decision. Pass state='pending' for just the open ones.
        Requires an admin token."""
        return window(
            lambda rows: client.list_approvals(state=state, limit=rows),
            limit=limit,
            offset=offset,
            cap=APPROVAL_LIST_CAP,
        )

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def decide_approval(
        request_id: int,
        decision: str,
        note: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Approve or reject a pending approval request ('approve' | 'reject').

        Approving does NOT perform the action: it opens the gate for exactly ONE
        retry by the original requester against the same target, and that retry
        re-validates everything. Deciding an already-settled request is refused
        rather than silently flipping an answer the agent may have acted on.
        Requires a human administrator's token: an agent account is refused
        whatever its role, and nobody decides a request they made."""
        return client.decide_approval(
            request_id,
            decision=decision,
            note=note,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def set_approval_policy(
        user_id: int,
        action_kind: str,
        idempotency_key: IdempotencyKey | None = None,
    ) -> ListResult:
        """Admin: require operator approval before this user may take an action
        kind ('issue.close' or 'dispatch.request'). Gating is opt-in — an ungated
        user is unaffected. Returns the user's gated kinds."""
        return listing(
            client.set_approval_policy(
                user_id, action_kind=action_kind, idempotency_key=idempotency_key
            )
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_agent_budget(user_id: int) -> dict | None:
        """Read a user's durable action budget — how many metered writes it may
        make per fixed window, how many it has used, and how many remain — or
        {found: false} when that user is unbudgeted (the default, meaning unlimited). Admin for
        anyone else; any actor may read its own. `whoami` carries yours already."""
        return client.get_agent_budget(user_id)

    # Usage and window survive; each limit is audited.
    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def set_agent_budget(
        user_id: int,
        window: str,
        action_limit: int,
        coordination_limit: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Admin: cap how many metered writes a user may make per fixed window
        ('hour' or 'day'). Metering is opt-in — an unbudgeted user is unlimited —
        so this is the lever that starts bounding an agent. Raising a limit
        mid-window releases the agent at once without granting a fresh window;
        action_limit=0 freezes its metered writes while leaving reads working.
        coordination_limit is a second ceiling, counted apart, on comments,
        claims and delegations in the same window; omit it (or null) to leave
        them unlimited, which also removes one set earlier. Renewals, yields
        and completions are never metered.
        Returns the budget. Requires an admin token."""
        return client.set_agent_budget(
            user_id,
            window=window,
            action_limit=action_limit,
            coordination_limit=coordination_limit,
            idempotency_key=idempotency_key,
        )

    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def list_indeterminate_keys(
        limit: IndeterminateKeyLimit = 50, cursor: ListCursor | None = None
    ) -> ListResult:
        """Admin: the idempotency keys whose outcome is unknown (the request
        was cut off by a crash or a kill), oldest first. Each row has the
        request's `method` and `path`, the `issue_id` it named if one, `reason`,
        and `sent_by` (the account and token). Read the target each names, then
        settle it with resolve_idempotency_key. Requires an admin token."""

        def fetch(rows: int) -> results.CursorPage:
            page = client.list_indeterminate_keys(limit=rows, cursor=cursor)
            return page["items"], page["next_cursor"], page["has_more"]

        return results.cursor_page(fetch, limit=limit)

    # Not undoable: a forgotten key lets the request run again.
    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def resolve_idempotency_key(
        key_id: int,
        key: str,
        outcome: Literal["applied", "not_applied"],
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Admin: settle one key from list_indeterminate_keys after reading the
        target of its request. `key_id` and `key` are the row's `id` and `key`.
        outcome="applied": the write landed; the key keeps refusing and now
        says idempotency_applied, so its sender does not send it again.
        outcome="not_applied": the key is forgotten and the same request under
        it runs. Recorded on the trail. Requires an admin token."""
        return client.resolve_idempotency_key(
            key_id, key=key, outcome=outcome, idempotency_key=idempotency_key
        )

    # Destructive: drops the window's usage count.
    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def clear_agent_budget(
        user_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Admin: remove a user's budget, returning it to unlimited. Idempotent.
        Requires an admin token."""
        return client.clear_agent_budget(user_id, idempotency_key=idempotency_key)
