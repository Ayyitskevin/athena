# Answer glossary

<!-- Generated from src/athena/aegis/answer_glossary.py by scripts/generate_agent_api.py. Do not edit by hand. -->

The desk and the work-context packet each have a lean shape: the full answer without the fixed explanations listed here and without any key whose value is null. This document holds those explanations once, by the lean schema that leaves them out and the path where the full answer carries them. It is served as the MCP resource `athena://answers/glossary` and at `GET /agent/glossary`.

- On MCP, `my_desk` and `get_issue_work_context` answer lean by default; `explain=true` returns the full answer.
- On REST, `GET /desk` and `GET /issues/{ref}/work-context` answer in full by default; `?lean=true` returns the lean shape.
- A lean answer's `schema` names its shape. A key absent from a lean answer is null. Nothing else differs from the full answer.
- Paths are dotted from the answer's root; `[]` stands for each item of a list.

## athena.agent_desk.v3

The lean `athena.agent_desk.v2`: `my_desk` (MCP, by default) and `GET /desk?lean=true`. Nested blocks keep their own schema names (`work.ready` is athena.ready_queue.v1, `office` is athena.office.v1); their texts are listed here by their path in the desk.

- `identity.notebook.meaning`: your notebook page: the first lines are shown; read the page for the rest. Append at session end with an If-Match edit of the page, so a parallel session cannot clobber it. Nothing summarises it, and it is not a runbook.
- `asks.run_controls.meaning`: asked of you and not yet answered
- `asks.worker_kill_requests.meaning`: the operator asked these workers to stop; Athena cannot signal a process, so honoring it is yours to do
- `asks.claim_handoffs.meaning`: context handed to you that you have not acknowledged
- `work.protocol.claim_one_issue`: `true`
- `work.protocol.do_not_touch_other_work`: `true`
- `work.protocol.complete_does_not_close_issue`: `true`
- `work.protocol.meaning`: The desk is orientation, not a lock. Claim at most one issue. Do not edit files another active lease has fenced. complete_claim releases the lease only.
- `work.ready.meaning`: issues you could claim now, in claim order (delegated to you, then priority, then oldest): visible, to-do or doing, no live lease, no blocker that is not done (a blocker you cannot see still blocks), you are the assignee, a delegated contributor or an admin, and not awaiting an answer (the desk's not_offered says why). `total` counts the same rule before `limit` and is what claim_next_ready can serve. A read, not a reservation: the claim command decides between two claimants.
- `work.waiting_on_operator.meaning`: what you asked a human and is still open: your blocking questions nobody has resumed and your pending approvals, oldest first. Nothing enforces a response time; do not re-ask what is already here. `not_offered` lists work the ready lane holds back from you until another account answers, with the reason.
- `work.waiting_on_operator.not_offered.meaning`: issues claim_next_ready will not offer you yet, though every other ready rule holds. awaiting_answer: the newest settlement is a needs_input yield, offered to no account; completed_by_you: you completed it, not offered back to you. Either lifts when another account (not Automation) comments, changes its status or assignment, or claims it; completed_by_you also when another account requests your review on it. claim_issue still works directly.
- `work.waiting_on.meaning`: issues you delegated that are waiting on their delegate: delegations older than 24h that the delegate has neither claimed nor recorded work on since, oldest first; work by anyone else does not count as pickup, and done or archived issues are not waiting
- `work.leases.meaning`: `items` are the issues you hold at this read; `active` is the clock's verdict, not a stored state, and `total` counts the same instant. `lapsed` are leases the clock already released, including done issues (`issue_done`). These are your own residual rows, not active possession. `lapsed_total` counts them before the shared `limit`; `lapsed_clipped` says whether rows are omitted. `lapsed_oldest_expires_at` is null when empty or clipped (unknown when clipped); total/clipped distinguish these cases. Claim order does not establish oldest expiry. Holders settle their own rows deliberately with complete_claim and the observed generation; an admin alone settles another actor's expired row, by generation. Reading changes nothing. complete_claim releases the lease and does not mark the issue done. declared_paths is the optional file fence. path_fence: rooted (a catalog root, not a verified location), lexical, root unknown (no catalog), unresolved (conflicts conservatively) or issue_only (no files).
- `work.leases.items[].progress.meaning`: last_work_at is the holder's latest recorded work on this issue (edit, status change, comment, attachment, link, completion). Check-ins and lease renewals are shown separately and never reset it; unknown means no work is recorded, not that the work is fresh.
- `office.protocol.claim_one_issue`: `true`
- `office.protocol.do_not_touch_other_work`: `true`
- `office.protocol.complete_does_not_close_issue`: `true`
- `office.protocol.meaning`: Sit in at most one chair. Claim that issue. Fence files with paths. complete_claim stands you up; it does not close the issue.
- `office.semantics.snapshot`: this_actor_right_now
- `office.semantics.does_not_assert`: `["alive", "running", "repo_exists", "branch_exists"]`

## athena.issue_work_context.v2

The lean `athena.issue_work_context.v1`: `get_issue_work_context` (MCP, by default, full and brief) and `GET /issues/{ref}/work-context?lean=true`. `how_to_claim.claimant_prerequisite` keeps `satisfied_at_read` and `next_operation`: they are this reader's, not fixed. The brief packet carries none of these texts; lean, it only drops its null keys.

- `semantics.snapshot`: current_visible_state
- `semantics.does_not_assert`: `["claimed", "ready", "unblocked", "running", "live", "replay_safe"]`
- `semantics.readiness`: this packet does not assert ready or unblocked; the desk's work.ready lane (GET /desk/ready) does, for you: visible, to-do or doing, no live lease, no blocker that is not done, and claimable by you, bounded with its own total
- `protocol.claim_one_issue`: `true`
- `protocol.do_not_touch_other_work`: `true`
- `protocol.complete_does_not_close_issue`: `true`
- `how_to_claim.claimant_prerequisite.policy`: assignee, delegated contributor, or admin; creator alone is insufficient
- `how_to_claim.claimant_prerequisite.meaning`: claimant policy snapshot, not authority or full claim eligibility; write-time scope, visibility, ETag, generation and conflict checks still apply
- `how_to_claim.header`: If-Match
- `how_to_claim.from`: issue_etag
- `how_to_claim.not`: the work-context packet's top-level _etag
- `how_to_claim.paths`: optional relative POSIX files; omitted/null retains on renewal, [] means issue-only
- `how_to_claim.coordination_root`: with a catalog: a reviewed selector from the operator; not authority or location proof
- `how_to_claim.path_fence`: rooted: a catalog root; lexical, root unknown: no catalog, compared as written; unresolved: separation unproven; issue_only: none
