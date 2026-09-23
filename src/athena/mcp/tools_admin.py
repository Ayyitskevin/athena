"""Agent-control and admin tools.

Registered by mcp.server.build_server."""

from __future__ import annotations

from typing import Literal

from athena.mcp.server import (
    DispatchIssueId,
    DispatchLimit,
    IdempotencyKey,
    RunControlId,
    RunControlKind,
    RunControlListLimit,
    RunControlPayload,
    RunControlStateFilter,
    RunControlSummary,
    RunControlTtl,
    RunId,
)


def register_admin_tools(tool, mutation_tool, client) -> None:
    # --- agent control (admin) ---------------------------------------------

    @mutation_tool
    def onboard_agent(
        name: str,
        scopes: list[str],
        email: str | None = None,
        token_name: str | None = None,
    ) -> dict:
        """Admin: provision a NEW agent teammate in one audited move — create its
        user account (member role, token-only) and mint its first scoped token.
        Scopes are required (least privilege: e.g. ["read", "issue:write"]).
        Email is optional; omitted becomes {slug}@agents.local. Returns the user,
        the one-time raw token, and a ready-to-paste MCP config. Requires admin."""
        return client.onboard_agent(
            name=name, email=email, scopes=scopes, token_name=token_name
        )

    @mutation_tool
    def pause_agent(user_id: int) -> dict:
        """Admin: PAUSE user_id — every authenticated action it attempts is
        refused until resumed, but nothing is destroyed (tokens and sessions
        stay intact). The lever to reach for BEFORE the kill switch when an
        agent looks off-course. Audited. Requires an admin token."""
        return client.set_user_paused(user_id, True)

    @mutation_tool
    def resume_agent(user_id: int) -> dict:
        """Admin: RESUME a paused user_id — restores the account exactly as it
        was before the pause. Audited. Requires an admin token."""
        return client.set_user_paused(user_id, False)

    @mutation_tool
    def revoke_agent_tokens(user_id: int) -> dict:
        """Admin kill switch: revoke EVERY live API token held by user_id — the
        lever to immediately stop a compromised or runaway agent. Idempotent and
        audited; returns {user_id, revoked_token_count}. Requires an admin token."""
        return client.revoke_agent_tokens(user_id)

    @mutation_tool
    def offboard_agent(user_id: int) -> dict:
        """Admin one-click offboard: demote user_id to viewer, revoke every session,
        and revoke every token — one audited lockout. Refuses to strip the last
        admin. Returns the counts revoked. Requires an admin token."""
        return client.offboard_user(user_id)

    @mutation_tool
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

    @tool
    def list_dispatches(
        work_item_id: DispatchIssueId | None = None,
        state: str | None = None,
        limit: DispatchLimit = 50,
    ) -> list:
        """What Athena has handed to the executor, newest first. Filter by
        work_item_id or by state ('pending_delivery', 'accepted', 'undeliverable',
        'completed', 'failed'). Each state is what Athena was told, not what is
        happening on the far side."""
        return client.list_dispatches(
            work_item_id=work_item_id, state=state, limit=limit
        )

    @mutation_tool
    def record_run_learning(
        issue_id: int,
        summary: str,
        run_id: str | None = None,
        space_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Write down what you learned, so the NEXT run starts knowing it.

        Appends your summary to the issue's runbook page — one Mentor page per
        issue holding what people and agents found out while working on it. The
        entry references the issue, so it shows up in the issue's backlinks and in
        the work-context packet the next agent reads. This is how a correction
        becomes durable memory instead of dying with your session.

        Write what would have saved you time: what you tried, what the evidence
        showed, what actually resolved it, what is still unknown. Your text is
        recorded as a QUOTE attributed to you — it is your report, not Athena's
        finding, and nothing in it is ever executed.

        Pass run_id to attribute the learning to a run (it must exist and be one
        you can see). space_id is needed only the first time, to say where the
        runbook should live; `get_issue_runbook` tells you whether one exists.
        Requires the docs:write scope."""
        return client.record_run_learning(
            issue_id,
            summary=summary,
            run_id=run_id,
            space_id=space_id,
            idempotency_key=idempotency_key,
        )

    @tool
    def get_issue_runbook(issue_id: int) -> dict | None:
        """The issue's runbook page — accumulated learnings from earlier runs — or
        null when nobody has recorded one yet. Read it before starting work, and
        add to it with `record_run_learning` when you finish."""
        return client.get_issue_runbook(issue_id)

    @tool
    def list_security_events(
        verb: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list:
        """Recent boundary REFUSALS — someone probing where they may not go.

        Covers failed logins, revoked tokens still being presented, scope denials,
        and paused accounts that keep trying. Each names the account whose boundary
        was hit. Narrow with verb ('login_failed', 'revoked_token_used',
        'scope_denied', 'paused_account_refused') and since ('YYYY-MM-DD
        HH:MM:SS'). Requires an admin token."""
        return client.list_security_events(verb=verb, since=since, limit=limit)

    @mutation_tool
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

    @tool
    def my_desk() -> dict:
        """START HERE. Your desk: who you are, what is asked of you, what you
        are holding, and what changed since you last looked.

        Replaces the whoami + delegations + controls + notifications + budget
        round trip with one bounded read. Lanes: `identity` (role, scopes,
        budget, action kinds needing approval), `asks` (open run controls
        addressed to you, kill requests on your workers, claim handoffs you
        have not acknowledged), `work` (your delegation inbox, the leases you
        hold — `active` is the clock's verdict at THIS read, not a stored
        state — and `work.leases.lapsed`, rows the clock already released on
        issues still open: renew them or clear them with complete_claim),
        `signals` (unread notifications, and how many visible events sit past
        your cursor).

        The loop: `my_desk()` -> act -> drain `recent_events(after=...)` from
        your cursor -> `advance_desk_cursor(after_id=<last id you handled>)`.

        The desk RESERVES NOTHING. It is a snapshot, not a lock, a lease, or a
        queue: seeing work here does not claim it, and two agents can read the
        same contents at once. Claim work through the delegation/lease tools."""
        return client.my_desk()

    @tool
    def my_office() -> dict:
        """Your cubicle. Athena's unique seat: at most one chair (one active
        lease), fenced paths, and a checkout branch hint. START HERE if you
        already know who you are and only need the job.

        `seated` is true only when you hold exactly one active lease. `chair`
        names the issue, generation, declared_paths, and `checkout_hint`
        (a branch name — Athena does not create git remotes). `next_to_sit`
        is the first delegated issue when you are standing.

        RESERVES NOTHING. Claim through claim_issue. complete_claim stands
        you up; it does not close the issue."""
        return client.my_office()

    @tool
    def get_project_floor(project_id: int) -> dict:
        """A project's floor: every open issue is a chair. Occupied chairs
        have a sitting agent and optional fenced paths. Empty chairs still
        need a body. `blocked_by` lists open blockers; there is no 'ready'
        flag. RESERVES NOTHING."""
        return client.get_project_floor(project_id)

    @mutation_tool
    def advance_desk_cursor(after_id: int) -> dict:
        """Record that you have handled every visible event up to `after_id`.

        Your cursor is personal bookkeeping — it emits no activity event and
        tells nobody else anything. It moves FORWARD only: acknowledging the
        same id twice is a harmless no-op, and a lower id is refused (409),
        because unsaying an acknowledgement would be a claim about history.

        Pass the id of the last event you actually processed, not the newest
        one you saw — the desk's `signals.latest_visible_event_id` is what a
        fully drained reader would use."""
        return client.advance_desk_cursor(after_id=after_id)

    @tool
    def activity_chain_status() -> dict:
        """Where the audit trail's hash chain stands (admin only).

        Returns the ANCHOR (the first chained event — rows below it predate the
        chain and are counted, never claimed), the HEAD (the newest entry's
        hash — note it somewhere outside Athena to make even a full-chain
        rebuild detectable), and coverage counts. The chain proves the recorded
        trail has not been rewritten; it does not prove what an agent's process
        actually did off the record."""
        return client.activity_chain_status()

    @tool
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

    @tool
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

    @mutation_tool
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

    @tool
    def list_workers(agent_id: int | None = None, limit: int = 100) -> list:
        """The worker registry — which agent processes are reporting, on what node,
        with what capabilities, and which were asked to stop. Admins see the whole
        fleet; anyone else sees only their own workers. `reporting_state` is
        cooperative presence, not process liveness."""
        return client.list_workers(agent_id=agent_id, limit=limit)

    @mutation_tool
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

    @mutation_tool
    def cancel_worker_kill(
        worker_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Admin: withdraw a kill request the worker has not acknowledged yet.
        Refused once acknowledged — it may already be shutting down. Requires an
        admin token."""
        return client.cancel_worker_kill(worker_id, idempotency_key=idempotency_key)

    @mutation_tool
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

    @tool
    def list_run_controls(
        run_id: str | None = None,
        state: RunControlStateFilter | None = None,
        limit: RunControlListLimit = 50,
    ) -> list:
        """Run controls — operator requests on live runs and how their agents
        answered. Admins see everything; anyone else sees only controls addressed
        to them. state='open' is YOUR inbox: unsettled, not yet expired — poll it
        while you work and answer what you find. `state` reports what was
        actually said; 'expired' means the clock ran out, never that anything
        stopped."""
        return client.list_run_controls(run_id=run_id, state=state, limit=limit)

    @tool
    def get_run_control(control_id: RunControlId) -> dict:
        """One run control, for an admin or the agent it is addressed to."""
        return client.get_run_control(control_id)

    @mutation_tool
    def acknowledge_run_control(
        control_id: RunControlId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Record that YOU read a control addressed to your run.

        Receipt, nothing more — follow up with decline_run_control or
        complete_run_control when you have actually acted. Re-acknowledging is a
        no-op. Refused once the control is settled or expired."""
        return client.acknowledge_run_control(
            control_id, idempotency_key=idempotency_key
        )

    @mutation_tool
    def decline_run_control(
        control_id: RunControlId,
        reason: RunControlSummary,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Decline a control addressed to your run, with the reason the operator
        will read. An answer, not an error — say why you will not comply."""
        return client.decline_run_control(
            control_id, reason=reason, idempotency_key=idempotency_key
        )

    @mutation_tool
    def complete_run_control(
        control_id: RunControlId,
        summary: RunControlSummary | None = None,
        handoff: dict | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Complete a control addressed to your run.

        For steer and request_cancel, pass a bounded summary of what you actually
        did. For request_fresh_context, pass handoff instead: an object with
        summary (required), unresolved_questions, athena_refs, evidence_refs —
        bounded lists of short strings; never transcripts or hidden reasoning.
        Completion is YOUR CLAIM, recorded as such — it does not prove any
        process-level effect."""
        return client.complete_run_control(
            control_id,
            summary=summary,
            handoff=handoff,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
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

        Reversible today: issue archive/unarchive and label/unlabel, page
        archive/unarchive and label/unlabel. Everything else is refused with a
        reason: a comment or an attachment is one-way (delete it explicitly
        instead), a destroyed row is a trapdoor. Refused too when the event was
        already undone, was imported from another system, or when its effect is no
        longer in force (someone already changed it back). Find event ids with
        `recent_events(...)`."""
        return client.undo_action(event_id, idempotency_key=idempotency_key)

    @tool
    def list_approvals(state: str | None = None, limit: int = 100) -> list:
        """The operator's approval queue — actions an agent asked to take that are
        waiting on a human decision. Pass state='pending' for just the open ones.
        Requires an admin token."""
        return client.list_approvals(state=state, limit=limit)

    @mutation_tool
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
        Requires an admin token."""
        return client.decide_approval(
            request_id,
            decision=decision,
            note=note,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def set_approval_policy(
        user_id: int,
        action_kind: str,
        idempotency_key: IdempotencyKey | None = None,
    ) -> list:
        """Admin: require operator approval before this user may take an action
        kind ('issue.close' or 'dispatch.request'). Gating is opt-in — an ungated
        user is unaffected. Returns the user's gated kinds."""
        return client.set_approval_policy(
            user_id, action_kind=action_kind, idempotency_key=idempotency_key
        )

    @tool
    def get_agent_budget(user_id: int) -> dict | None:
        """Read a user's durable action budget — how many metered writes it may
        make per fixed window, how many it has used, and how many remain — or null
        when that user is unbudgeted (the default, meaning unlimited). Admin for
        anyone else; any actor may read its own. `whoami` carries yours already."""
        return client.get_agent_budget(user_id)

    @mutation_tool
    def set_agent_budget(
        user_id: int,
        window: str,
        action_limit: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Admin: cap how many metered writes a user may make per fixed window
        ('hour' or 'day'). Metering is opt-in — an unbudgeted user is unlimited —
        so this is the lever that starts bounding an agent. Raising a limit
        mid-window releases the agent at once without granting a fresh window;
        action_limit=0 freezes its metered writes while leaving reads working.
        Returns the budget. Requires an admin token."""
        return client.set_agent_budget(
            user_id,
            window=window,
            action_limit=action_limit,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def clear_agent_budget(
        user_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Admin: remove a user's budget, returning it to unlimited. Idempotent.
        Requires an admin token."""
        return client.clear_agent_budget(user_id, idempotency_key=idempotency_key)
