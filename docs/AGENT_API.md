# Agent API — MCP tools, scopes, and the REST calls behind them

<!-- GENERATED FILE — do not edit by hand. -->
<!-- Regenerate: python scripts/generate_agent_api.py -->
<!-- Drift check: tests/test_agent_api_doc.py (runs in the gate) -->

Tools that access Athena use the REST API with the session's bearer
token — never around it. `begin_run` and `current_run` only set or read
local run identity without HTTP. This table maps the tool surface;
the full REST reference is the app's own `/openapi.json`
(browse it at `/redoc`); this page adds the two columns OpenAPI
cannot: which **token scope** each MCP tool requires, and which REST
call(s) it performs.

At runtime the tool list is normally scope-filtered using the token's
scopes ([RUNTIME_RECIPE.md](RUNTIME_RECIPE.md)); `admin` implies every scope.
If the identity probe is unavailable, presentation fails open; REST
enforcement remains the boundary. A tool shown as *(client-side)*
composes other calls or local state instead of one fixed route.

Every tool answers one JSON object, as one text block and as structured
content. A list is `{items, count, truncated, next}`; when `truncated` is
true, `next` holds the arguments that continue it. A call with nothing to
return answers `{found: false}` (a read) or `{applied: true}` (a write).
The Hints column is the tool's MCP annotations: *read-only*, or for a
write whether it is *destructive* and *idempotent*.

## Reported claim completion

`complete_claim` also accepts mutually exclusive `candidate_submission` and
`review_candidate_id` for exact-version tracking under your own active lease.
Submission requires an immutable declared identity and an explicit nullable
`expected_previous_candidate_id`; review targets the current candidate from
a distinct issue and account. Both compose with release in one request.
See [exact-version closeout](CLOSEOUT_RECIPE.md#exact-version-submission-and-review)
for the grammar, visibility, stale-version and historical-retry boundaries.
These associations grant no acceptance or external-action authority.

`complete_claim` and `POST /issues/{id}/complete` accept optional
`verdict` (`PASS`, `PASS-WITH-NOTES`, `HOLD`, `BLOCKED`), `findings`
(`P1`/`P2`/`P3` to non-negative integers; a missing key is "not stated"),
`results`, `unfinished`, `uncertainty`, and typed, source-qualified
`evidence` (each item may add `digest`, `bytes`, `locator`; each result a
`subject` list; kinds include `attachment` and `forge_event`, whose `ref` is a
row id). Only shape is validated, except that an attachment on the same issue
with a `digest` is shown as `matches stored attachment` or `unverified
reference` (a mismatch is shown, never refused).
They are actor-reported data, not a verified success verdict.
Optional `usage` reports model, token and cost data since the previous
report. It is reported by the agent, not metered by Athena, and limits
nothing. See the [usage grammar and example](CLOSEOUT_RECIPE.md#optional-reported-usage)
for its accepted fields, bounds and cost/currency pairing.
`not_assessed` means the condition was not evaluated; for example, an
integration check was never run. `inconclusive` means an evaluation was
attempted but did not establish `met` or `not_met`; for example, an
integration check ran but timed out before producing a result.
Reported `not_met` results and `unrun` evidence do not block lease release
under the existing generation rules. Issue status and gates are unchanged.
Omitted fields retain the legacy response and event detail. References are
inert text: never submit credentials or secret-bearing URLs.
New report text and yield evidence refuse recognized credential shapes
without redaction; this accident guard is not a security boundary.
Existing stored history is not scanned or rewritten. See
[CLOSEOUT_RECIPE.md](CLOSEOUT_RECIPE.md#optional-reported-completion) for
the bounded grammar, evidence scopes, example, and preservation limits.

## Read tools — scope: `read`

| MCP tool | REST call(s) | Hints | Summary |
|---|---|---|---|
| `begin_run` | *(client-side)* | read-only | Switch this session's run identity: every write you make afterwards is … |
| `count_work` | `GET /issues/query/count` | read-only | How many issues a work query matches, ignoring paging — so a bounded … |
| `current_run` | *(client-side)* | read-only | Read the run identity this session is currently stamping on writes: |
| `embed_help` | `GET /embeds/help` | read-only | The embed vocabulary as data: every kind, its keys, and the limits. |
| `find_pages_by_title` | `GET /pages/by-title` | read-only | Find Mentor pages by their TITLE instead of a numeric id — the address you can … |
| `get_agent_budget` | `GET /users/{user_id}/budget` | read-only | Read a user's durable action budget — how many metered writes it may … |
| `get_attention_ranking` | `GET /attention/ranking` | read-only | Read the bounded, actor-filtered "Now" queue. Each row cites its … |
| `get_fleet_metrics` | `GET /fleet/metrics` | read-only | Read bounded issue throughput for this token's visible scope. Dates are … |
| `get_issue` | `GET /issues/{ref}` | read-only | Get one issue by issue_id (the number every write tool takes) or by … |
| `get_issue_history` | `GET /issues/{issue_id}/history` | read-only | Read one issue's bounded operator run narrative. Every item cites … |
| `get_issue_lease` | `GET /issues/{issue_id}/lease` | read-only | Who holds the exclusive claim on this issue right now — {holder_id, holder_name, … |
| `get_issue_prior_text` | `GET /issues/{issue_id}/prior-texts/{event_id}` | read-only | Read the title and body one edit of an issue replaced, exactly as they … |
| `get_issue_runbook` | `GET /issues/{issue_id}/runbook` | read-only | The issue's runbook page — accumulated learnings from earlier runs — or … |
| `get_issue_state` | `GET /issues/{issue_id}/state` | read-only | Reconstruct an issue's lifecycle state from the activity log. Pass … |
| `get_issue_work_context` | `GET /issues/{ref}/work-context` | read-only | Get a bounded, current packet containing one visible issue and its … |
| `get_page` | `GET /pages/{page_id}` | read-only | Get one Mentor page (title + Markdown body). The response includes the … |
| `get_page_version` | `GET /pages/{page_id}/versions/{version}` | read-only | Fetch one historical page revision by its version number (title + body as of … |
| `get_project_brief` | `GET /projects/{project_id}/brief` | read-only | A compact visible project snapshot: summary, active claims, blocker edges, … |
| `get_project_floor` | `GET /projects/{project_id}/floor` | read-only | A project's floor: every open issue is a chair. Occupied chairs … |
| `get_project_review_queue` | `GET /projects/{project_id}/review-queue` | read-only | Read current visible project candidates with their exact-version reports. |
| `get_run_control` | `GET /run-controls/{control_id}` | read-only | One run control, for an admin or the agent it is addressed to. |
| `get_run_fork_contract` | `GET /activity/runs/{run_id}/fork` | read-only | Validate a fork point inside a parent run and return the child-run … |
| `get_run_lineage` | `GET /activity/runs/{run_id}/lineage` | read-only | Read a tagged run's causal tree: ancestors, the focal run's replayable … |
| `get_run_replay` | `GET /activity/runs/{run_id}/replay` | read-only | Export one run as its portable replay ARTIFACT: the events in replay … |
| `get_sprint` | `GET /sprints/{sprint_id}` | read-only | Get one sprint's descriptive fields and lifecycle state. Hidden and … |
| `get_watch_preference` | `GET /watches/{target_kind}/{target_id}/preference` | read-only | Read YOUR priority, mute, and digest preference for one active watch. |
| `link_graph` | `GET /{base}/{node_id}/graph` | read-only | The link neighbourhood around an issue or page, as you can see it. |
| `list_activity_runs` | `GET /activity/runs` | read-only | Reconstruct one actor's recent activity into runs. Explicit X-Athena-Run … |
| `list_dispatches` | `GET /dispatches` | read-only | What Athena has handed to the executor, newest first. Filter by … |
| `list_eligible_accounts` | `GET /issues/{issue_id}/eligible-accounts` | read-only | List the accounts that can see and work this issue. |
| `list_issue_comments` | `GET /issues/{issue_id}/comments` | read-only | Read one issue's comment thread, oldest first — the discussion a … |
| `list_issue_links` | `GET /issues/{issue_id}/links` | read-only | Read an issue's dependency links — what it blocks, what blocks it, and … |
| `list_issues` | `GET /issues` | read-only | List Aegis issues, optionally filtered by a project-configured status, … |
| `list_labels` | `GET /labels` | read-only | List the shared label vocabulary (id, name, color). |
| `list_my_delegated_work` | `GET /delegations/me` | read-only | List issues delegated to the authenticated contributor. By default this … |
| `list_notifications` | `GET /notifications` | read-only | Read YOUR notification inbox — mentions, watched-issue changes, work … |
| `list_page_versions` | `GET /pages/{page_id}/versions` | read-only | The page's superseded revisions, newest first (the live page is NOT one of … |
| `list_pages` | `GET /spaces/{space_id}/pages` | read-only | List the pages in a space. Archived (soft-deleted) pages are hidden by … |
| `list_priority_notifications` | `GET /notifications/priority` | read-only | Read YOUR inbox as a priority/mute/digest projection. Priority is an … |
| `list_projects` | `GET /projects` | read-only | List Aegis projects (id, key, name). |
| `list_run_controls` | `GET /run-controls` | read-only | Run controls — operator requests on live runs and how their agents … |
| `list_run_events` | `GET /activity` | read-only | Replay one run: exactly the activity events tagged with this run id, … |
| `list_spaces` | `GET /spaces` | read-only | List Mentor spaces (id, key, name). |
| `list_sprints` | `GET /projects/{project_id}/sprints` | read-only | List a project's sprints, optionally filtered by state … |
| `list_subtasks` | `GET /issues/{issue_id}/children` | read-only | List the direct child issues (sub-tasks) nested under this issue. |
| `list_workers` | `GET /workers` | read-only | The worker registry — which agent processes are reporting, on what node, … |
| `my_desk` | `GET /desk` | read-only | START HERE. Your desk: who you are, what is asked of you, what you … |
| `my_office` | `GET /office` | read-only | Your cubicle: active leases, declared paths, and a checkout branch hint. |
| `notification_priority_summary` | `GET /notifications/priority/summary` | read-only | Count YOUR visible notifications by resolved priority and mute state. |
| `page_backlinks` | `GET /pages/{page_id}/backlinks` | read-only | What references this page — the INCOMING edges of the knowledge graph. Each … |
| `page_outgoing_links` | `GET /pages/{page_id}/outgoing-links` | read-only | What this page references — the OUTGOING edges of the knowledge graph (the … |
| `project_timeline` | `GET /projects/{project_id}/timeline` | read-only | A project's roadmap: sprint lanes, the issues in them, and the declared … |
| `query_help` | `GET /issues/query/help` | read-only | The work-query vocabulary as data: every field, its accepted values, … |
| `read_page_embeds` | `GET /pages/{page_id}` | read-only | Resolve a Mentor page's live embeds to DATA, as you. |
| `recent_events` | `GET /events` | read-only | Read the audit/event feed in ascending event-id order, with REST filters. |
| `related_items` | `GET /{base}/{node_id}/related` | read-only | What cites what this cites, but is NOT linked to it yet. |
| `resolve_embeds` | `POST /embeds/resolve` | read-only | Resolve embed directives in arbitrary text, as you. |
| `search` | `GET /search` | read-only | Full-text search across Aegis issues and Mentor pages. Optionally narrow … |
| `search_work` | `GET /issues` | read-only | Find issues with a work query — the precise way to ask for work. |
| `search_workspace` | `GET /search/workspace` | read-only | One ask across issues, pages, AND comments — use this when you do not … |
| `unlinked_mentions` | `GET /{base}/{node_id}/unlinked-mentions` | read-only | Documents whose text NAMES this issue/page without linking to it. |
| `wait_for_events` | `GET /events` | read-only | The event feed after `after`, in ascending id order, parked instead of … |
| `whoami` | `GET /users/me` | read-only | Who am I? Your identity (id, email, role, agent flag), the acting … |

## Aegis write tools — scope: `issue:write`

| MCP tool | REST call(s) | Hints | Summary |
|---|---|---|---|
| `archive_issue` | `POST /issues/{issue_id}/archive` | idempotent | Archive (soft-delete) an issue: it's hidden from the default lists but the … |
| `assign_issue` | `PUT /issues/{issue_id}/assignee` | idempotent | Assign an issue to a user id, or pass no assignee_id to unassign it. |
| `attach_label` | `POST /issues/{issue_id}/labels` | idempotent | Attach an existing label (by id — see list_labels) to an issue. |
| `bulk_update_issues` | `POST /issues/bulk` | idempotent | Apply the same change to MANY issues at once (one call instead of N). |
| `claim_issue` | `POST /issues/{issue_id}/claim` | additive | Claim or renew an issue. To acquire, omit generation and pass the … |
| `claim_next_ready` | `POST /issues/claim-next` | additive | Atomically select and claim ready work: delegated first, then priority, … |
| `comment_on_issue` | `POST /issues/{issue_id}/comments` | additive | Add a comment to an issue, authored by the token's user. |
| `complete_claim` | `POST /issues/{issue_id}/complete` | idempotent | Release your lease. This does NOT close the issue; update_issue does. |
| `complete_sprint` | `POST /sprints/{sprint_id}/complete` | idempotent | Move an active sprint to completed. Athena supplies today's date when … |
| `create_issue` | `POST /issues` | additive | Create an Aegis issue. Omit status to use the target project's default; … |
| `create_label` | `POST /labels` | idempotent | Create a label in the shared vocabulary. color is a #RRGGBB hex string. |
| `create_sprint` | `POST /projects/{project_id}/sprints` | additive | Create a planned sprint in a project you created. Optional dates are … |
| `decline_delegation` | `POST /issues/{issue_id}/decline` | idempotent | Decline an issue delegated to you (decline) — remove yourself from its … |
| `delegate_issue` | `POST /issues/{issue_id}/delegate` | idempotent | Delegate an issue to an agent user. The human assignee remains accountable; … |
| `delete_sprint` | `DELETE /sprints/{sprint_id}` | destructive, idempotent | Permanently delete an empty sprint. Set confirm_permanent=true after … |
| `detach_label` | `DELETE /issues/{issue_id}/labels/{label_id}` | idempotent | Remove a label from an issue. Returns the updated issue. |
| `dispatch_to_icarus` | `POST /issues/{issue_id}/dispatch` | additive | Hand an issue to the external execution fleet. |
| `link_issues` | `POST /issues/{issue_id}/links` | idempotent | Declare a dependency FROM this issue to another (by id or key, e.g. |
| `request_candidate_review` | `POST /issues/{review_issue_id}/review-requests` | idempotent | Ask a named reviewer to review an exact candidate on an existing review issue. |
| `resume_claim_handoff` | `POST /issues/{issue_id}/claim-handoffs/{handoff_token}/resume` | idempotent | Explicitly acknowledge an open claim handoff as the exact current … |
| `set_issue_parent` | `PUT /issues/{issue_id}/parent` | idempotent | Nest an issue under a parent issue (making it a sub-task), or pass no … |
| `set_issue_placement` | `PATCH /issues/{issue_id}` | idempotent | Atomically set an issue's project and sprint. Always provide BOTH … |
| `set_issue_sprint` | `PUT /issues/{issue_id}/sprint` | idempotent | Put an issue into a sprint (which must belong to the issue's own … |
| `start_playbook` | `POST /pages/{page_id}/start-playbook` | additive | Turn a playbook page's checklist into REAL WORK: one parent issue … |
| `start_sprint` | `POST /sprints/{sprint_id}/start` | idempotent | Move a planned sprint to active. A project may have only one active … |
| `unarchive_issue` | `POST /issues/{issue_id}/unarchive` | idempotent | Restore a previously archived issue to the active lists. Returns the issue. |
| `unlink_issues` | `DELETE /issues/{issue_id}/links/{relation}/{target_id}` | idempotent | Remove a dependency from this issue: the same relation used to add it … |
| `update_issue` | `PATCH /issues/{issue_id}` | idempotent | Update an issue. Send only the fields to change. status must be a name … |
| `update_sprint` | `PATCH /sprints/{sprint_id}` | destructive, idempotent | Update a sprint's name, goal, or dates without changing its state. |
| `yield_claim` | `POST /issues/{issue_id}/yield` | idempotent | Honestly release your exact active claim with a structured continuation … |

## Mentor write tools — scope: `docs:write`

| MCP tool | REST call(s) | Hints | Summary |
|---|---|---|---|
| `archive_page` | `POST /pages/{page_id}/archive` | idempotent | Archive (soft-delete) a page: it's hidden from the space tree, navigation, … |
| `create_page` | `POST /spaces/{space_id}/pages` | additive | Create a Mentor page in a space. Optionally nest it under parent_id (a … |
| `label_page` | `POST /pages/{page_id}/labels` | idempotent | Attach an existing label (by id — see list_labels) to a Mentor page. |
| `open_my_notebook` | `POST /desk/notebook` | idempotent | Open your notebook page (an agent's notes kept between sessions), … |
| `record_run_learning` | `POST /issues/{issue_id}/learnings` | additive | Write down what you learned, so the NEXT run starts knowing it. |
| `restore_page_version` | `POST /pages/{page_id}/versions/{version}/restore` | idempotent | Restore a page's content to a prior version. Non-destructive: the current … |
| `unarchive_page` | `POST /pages/{page_id}/unarchive` | idempotent | Restore a previously archived page to the active tree/nav/search. Returns … |
| `unlabel_page` | `DELETE /pages/{page_id}/labels/{label_id}` | idempotent | Remove a label from a Mentor page. Returns the page. |
| `update_page` | `PATCH /pages/{page_id}` | additive | Update a page's title and/or body. Each edit is snapshotted into the … |

## Cross-module write tools — scope: any write scope

| MCP tool | REST call(s) | Hints | Summary |
|---|---|---|---|
| `acknowledge_run_control` | `POST /run-controls/{control_id}/acknowledge` | idempotent | Record that YOU read a control addressed to your run. |
| `advance_desk_cursor` | `POST /desk/cursor` | idempotent | Record that you have handled every visible event up to `after_id`. |
| `clear_watch_preference` | `DELETE /watches/{target_kind}/{target_id}/preference` | destructive, idempotent | Delete YOUR preference for a watch, restoring target/default priority. |
| `complete_run_control` | `POST /run-controls/{control_id}/complete` | idempotent | Complete a control addressed to your run. |
| `decline_run_control` | `POST /run-controls/{control_id}/decline` | idempotent | Decline a control addressed to your run, with the reason the operator … |
| `heartbeat_agent_run` | `PUT /agent-runs/heartbeat` | additive | Report that this authenticated agent is still working on this … |
| `link_mention` | `POST /{base}/{source_id}/link-mention` | additive | Rewrite the SOURCE document so its first unlinked mention of the target … |
| `mark_notification_read` | `POST /notifications/read_all`<br>`POST /notifications/{notification_id}/read` | idempotent | Mark notifications in YOUR inbox as read, after handling them. Pass … |
| `mark_notifications_read` | `POST /notifications/read_all` | additive | Mark ALL unread notifications in YOUR inbox as read, including rows not shown … |
| `set_watch_preference` | `PUT /watches/{target_kind}/{target_id}/preference` | destructive, idempotent | Replace YOUR preference for an active watch. Omit or pass null for a … |
| `undo_action` | `POST /activity/{event_id}/undo` | idempotent | Undo one activity event by applying its registered inverse. |
| `unwatch` | `DELETE /watches/{target_kind}/{target_id}` | destructive, idempotent | Unsubscribe YOUR inbox from a target you are watching. Errors 404 if … |
| `watch` | `POST /watches` | idempotent | Subscribe YOUR inbox to a target so you learn it changed without … |
| `worker_heartbeat` | `PUT /workers/heartbeat` | additive | Register or refresh YOUR worker process, and find out whether the … |

## Operator tools — scope: `admin`

| MCP tool | REST call(s) | Hints | Summary |
|---|---|---|---|
| `activity_chain_status` | `GET /activity/chain` | read-only | Where the audit trail's hash chain stands (admin only). |
| `agent_answerability` | `GET /fleet/answerability` | read-only | The per-agent ask-and-answer ledger (admin only). |
| `cancel_worker_kill` | `DELETE /workers/{worker_id}/kill` | idempotent | Admin: withdraw a kill request the worker has not acknowledged yet. |
| `clear_agent_budget` | `DELETE /users/{user_id}/budget` | destructive, idempotent | Admin: remove a user's budget, returning it to unlimited. Idempotent. |
| `create_automation_rule` | `POST /automation/rules` | additive | Create an admin-only automation rule. Event rules use trigger_type='event' … |
| `create_run_control` | `POST /run-controls` | additive | Admin: record a control request against a live run. |
| `decide_approval` | `POST /approvals/{request_id}/decision` | idempotent | Approve or reject a pending approval request ('approve' \| 'reject'). |
| `delete_automation_rule` | `DELETE /automation/rules/{rule_id}` | destructive, idempotent | Permanently delete an admin-only automation rule. Disable it instead when … |
| `get_agent_run_health` | `GET /activity/agent-runs` | read-only | Read the admin-only fleet cockpit rollup: each agent's bounded recent … |
| `get_automation_rule` | `GET /automation/rules/{rule_id}` | read-only | Get one admin-only automation rule by id, including schedule progress, … |
| `get_fleet_active_work` | `GET /fleet/active-work` | read-only | Admin-only view of agent-held issue claims. Joins each lease to its … |
| `list_approvals` | `GET /approvals` | read-only | The operator's approval queue — actions an agent asked to take that are … |
| `list_automation_failures` | `GET /automation/rules` | read-only | Read the admin-only exception list of automation rules whose actions have … |
| `list_automation_rules` | `GET /automation/rules` | read-only | List every admin-only automation rule with its event or schedule … |
| `list_security_events` | `GET /security/events` | read-only | Recent boundary REFUSALS — someone probing where they may not go. |
| `list_users` | `GET /users` | read-only | List users (id, name, role) — useful for resolving an assignee. |
| `offboard_agent` | `POST /users/{user_id}/offboard` | destructive, idempotent | Admin one-click offboard: demote user_id to viewer, revoke every session, … |
| `onboard_agent` | `POST /users/onboard_agent` | idempotent | Admin: provision a NEW agent teammate in one audited move — create its … |
| `pause_agent` | `PUT /users/{user_id}/paused` | idempotent | Admin: PAUSE user_id — every authenticated action it attempts is … |
| `request_worker_kill` | `POST /workers/{worker_id}/kill` | idempotent | Admin: ask a worker to stop. |
| `resume_agent` | `PUT /users/{user_id}/paused` | idempotent | Admin: RESUME a paused user_id — restores the account exactly as it … |
| `revoke_agent_tokens` | `DELETE /users/{user_id}/tokens` | destructive, idempotent | Admin kill switch: revoke EVERY live API token held by user_id — the … |
| `set_agent_budget` | `PUT /users/{user_id}/budget` | idempotent | Admin: cap how many metered writes a user may make per fixed window … |
| `set_approval_policy` | `PUT /approvals/policies/{user_id}` | idempotent | Admin: require operator approval before this user may take an action … |
| `set_automation_rule_enabled` | `PATCH /automation/rules/{rule_id}` | idempotent | Arm or disarm an admin-only automation rule without deleting its … |
| `verify_activity_chain` | `GET /activity/chain/verify` | read-only | Recompute a bounded window of the audit trail's hash chain (admin only). |

## Refusal codes

Every 4xx and 5xx JSON body from a REST route carries a top-level `code`
from this registry (`core/refusal_codes.py`), beside an unchanged
`detail`. Branch on `code`, never on the `detail` text. Over MCP a
refused call returns the same object with `isError` true, plus
`status_code` and, where this table names one, `next`: the tool call
that resolves it. A 202 `approval_required` is not an error: it comes
back with `isError` false, `applied: false` and `next`.

| Code | Status | Meaning | Next |
|---|---|---|---|
| `bad_request` | 400 | The request is malformed outside its body schema; fix it and resend. |  |
| `unauthenticated` | 401 | No valid credential: the token or actor is missing, unknown or revoked. Retrying with the same credential fails the same way. |  |
| `forbidden` | 403 | Authenticated, but your role, ownership or the target's access rules do not allow this action. |  |
| `scope_denied` | 403 | Your token lacks the scope this call needs (the detail names it); only an operator can issue a wider token, so no call of yours resolves it (my_desk's identity.scopes lists what this one may do). |  |
| `account_paused` | 403 | The operator paused or removed this account; every authenticated action is refused until it is resumed. Stop and report. |  |
| `run_binding_conflict` | 403 | The run id on this write belongs to another actor's run; tag the write with a run of your own. |  |
| `run_id_mismatch` | 422 | The run_id you sent is not the run this session is tagged with (the detail names both); omit run_id to use the session's run. | `current_run`: omit run_id, or pass the run_id it returns |
| `run_header_invalid` | 422 | The X-Athena-Run header was sent but would not be recorded as sent: a server-reserved namespace, over 200 characters, or not a printable run id (the detail says which). Fix the header; it is never read as absent. |  |
| `not_found` | 404 | The target does not exist or is not visible to you; the two are deliberately indistinguishable. |  |
| `method_not_allowed` | 405 | The path exists but does not accept this HTTP method. |  |
| `conflict` | 409 | The target's current state does not allow this action; read it again before deciding what to do. |  |
| `payload_too_large` | 413 | The request body or upload exceeds the server's size limit; `limit_bytes` names it. |  |
| `validation_error` | 422 | The input failed validation; the detail says which part. Resending it unchanged fails the same way. |  |
| `rate_limited` | 429 | Too many requests from this token or address; `rate_limit` carries the limit, what remains and retry_after_seconds to wait. |  |
| `capacity_reached` | 409, 429 | A bounded server-side limit is reached (run check-ins, workers, run controls, playbook steps); the detail says which and how to stay within it, and `limit` names the bound where it is one. |  |
| `internal_error` | 500 | The server failed while handling the request; whether a write applied is unknown. Read the target's state before retrying. |  |
| `unavailable` | 503 | A dependency (the database, the idempotency store or an execution fleet) cannot answer now; retry later. |  |
| `deployment_boundary` | 421, 503 | Refused before any Athena work: the Host or socket is not one this deployment serves. Check the base URL you were given. |  |
| `precondition_required` | 428 | This write needs If-Match with the target's current ETag. | `get_issue`: pass its _etag as if_match (a page's _etag is on get_page) |
| `invalid_if_match` | 400 | The If-Match value is not a valid entity tag; send one strong ETag. |  |
| `if_match_too_large` | 431 | The If-Match header exceeds the parsing limit; send one ETag. |  |
| `precondition_failed` | 412 | The target changed since the ETag you sent: read it again, merge your change into the current state, then retry. | `get_issue`: fresh _etag (a page's is on get_page); retry with it and a new idempotency_key |
| `claim_held` | 409 | Another account holds this issue's active lease; `holder` and `expires_at` name it and when it ends, and `events_after` is the newest event on the issue you can see. Do not work the issue. | `wait_for_events`: kind=issue, target=the issue, after=events_after: answers when the holder renews, yields, completes or declines; then claim again (also after expires_at). Other work: claim_next_ready |
| `claim_required` | 409 | This action needs your own active lease on the issue, and there is none: it lapsed, was released, or was never taken. | `get_issue_lease`: issue_id; if it is free, claim_issue takes it again |
| `lease_generation_required` | 428 | The call needs the lease's current generation (a repeated claim by its holder is a renewal). When you hold the lease, `generation` names it: repeat with it. An admin releasing another account's lease is not told it; get_issue_lease shows it. |  |
| `invalid_lease_generation` | 422 | The generation is not a 32-character lowercase hex token. | `get_issue_lease`: issue_id; pass its generation |
| `lease_generation_mismatch` | 409 | The generation you sent is not the lease's current one: your possession lapsed or was replaced. | `get_issue_lease`: issue_id; if the lease is no longer yours, stop work on it |
| `fence_overlap` | 409 | A path you declared overlaps another active lease's fence; `fence` names your path and the basis, and the other issue, holder, path and newest event (`events_after`) when you can read that lease (else null). | `wait_for_events`: kind=issue, target=fence.issue.id, after=fence.events_after: answers when that holder acts there; then claim again. With fence.issue null, declare paths that do not overlap instead |
| `fence_unresolved` | 409 | Your fence cannot be placed: the instance's root catalog does not know your coordination_root, or one of two fences has no current root. Without a catalog omit coordination_root; with one, obtain a current reviewed selector from the operator. |  |
| `blocked_issue_close_policy` | 409 | The project refuses to close an issue that has open blockers; close or unlink the blockers first (get_issue_work_context's dependencies.open_blockers lists them). |  |
| `cursor_ahead` | 422 | after_id is past the newest event you can see; the body carries latest_visible_event_id, the highest value you may pass. | `advance_desk_cursor`: after_id no higher than latest_visible_event_id |
| `invalid_query` | 422 | The work query does not parse; detail.error says where. |  |
| `approval_required` | 202 | The action is gated for you: it was NOT applied, and an operator decision is pending (the body carries the approval, and `events_after`, the newest event on its target you can see). | `wait_for_events`: kind=approval.target_kind, target=approval.target_id, after=events_after: answers when the operator decides; then repeat the call with the same idempotency_key |
| `approval_rejected` | 409 | The operator rejected this gated action (the note is in the body); do not retry it. |  |
| `agent_budget_exhausted` | 429 | Your account's write budget for this window is spent; nothing was applied. `held_leases` names each lease you hold and `retry_after_seconds` when the window ends. The accounts that answer for that work are told once per window. | `yield_claim`: for each of held_leases: issue_id, generation, reason=capacity and what you attempted; the work returns to the ready lane. Holding none: stop writing until retry_after_seconds |
| `agent_coordination_budget_exhausted` | 429 | Your account's coordination budget for this window is spent: comments, claims and delegations are refused until `retry_after_seconds`; nothing was applied. Work you hold is not affected: you can still edit, renew, complete or yield it. |  |
| `idempotency_key_invalid` | 400 | Send exactly one Idempotency-Key of 1-255 visible ASCII characters. |  |
| `idempotency_key_unsupported` | 400 | This endpoint does not take an Idempotency-Key; resend without one. |  |
| `idempotency_mismatch` | 409 | This Idempotency-Key was already used for a different request; use a new key for a different call. |  |
| `idempotency_in_progress` | 409 | A request with this Idempotency-Key is still running; retry the same call after Retry-After seconds. |  |
| `idempotency_indeterminate` | 409 | The earlier request under this key ended with an unknown outcome; read the target's state before choosing a new key. |  |
| `idempotency_authorization_changed` | 409 | What you may do changed after this key was claimed (the assignee, creator or a contributor of the issue it named, `issue_id`; or a role, token, membership, visibility, pause, placement, agent flag, close policy, approval gate or budget limit anywhere), so its stored response is not replayed and this answer does not say whether the write was applied. Read the target; repeat under a new key only if it was not. The key stays refused until its receipt would have expired. | `get_issue_work_context`: issue_id: the refusal's issue_id (the issue your keyed write named); its activity and comments show whether your write was applied. Repeat it with a new idempotency_key only if it was not. For a write on no issue, read what it changed instead. |
| `undo_event_not_found` | 404 | No activity event with that id is visible to you. |  |
| `undo_not_reversible` | 401, 422 | This event's action has no inverse that undo may apply. |  |
| `undo_already_reversed` | 409 | This event was already reversed; the detail names the reversal. |  |
| `undo_imported_event` | 422 | An imported event records another system's history; it cannot be undone. |  |
| `undo_no_effect` | 409 | The action's effect is no longer in force, so there is nothing to undo. |  |
| `undo_refused_by_command` | 400, 401, 403, 404, 409, 412, 422, 428, 431 | Undo is possible, but the inverse command refused it for you or for now; the status and detail are that command's. |  |

### MCP-side codes

These failures never got an HTTP answer from Athena: bad tool arguments,
a check the MCP client makes itself, or no response at all. No REST route
answers with them. A tool call that fails this way is the same one object,
with `isError` true and `status_code` null.

| Code | Status | Meaning | Next |
|---|---|---|---|
| `invalid_arguments` | none | The tool's arguments failed its own checks, so no request was sent; the detail names each argument and the rule it broke. |  |
| `unknown_tool` | none | No tool of that name is registered in this session's profile. |  |
| `coordination_root_unconfirmed` | none | The claim's answer did not confirm the coordination root you sent; the claim may already exist. Read the lease before acting. | `get_issue_lease`: issue_id; do not retry the claim without its root |
| `athena_no_response` | none | Athena did not answer (refused, reset or timed out); whether a write applied is unknown. Read the target's state before retrying. |  |
| `tool_failed` | none | The tool failed inside the MCP server after any request it made; the detail says how. Read the target's state before retrying. |  |

---

*142 tools. Generated from `mcp/tools_*.py` (each tool's declared scope and hints, and its body) and `mcp/client.py` (verb + path literals); the registration path is fail-closed, so a tool missing here cannot exist in the server either.*
