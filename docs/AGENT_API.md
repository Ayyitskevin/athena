# Agent API — MCP tools, scopes, and the REST calls behind them

<!-- GENERATED FILE — do not edit by hand. -->
<!-- Regenerate: python scripts/generate_agent_api.py -->
<!-- Drift check: tests/test_agent_api_doc.py (runs in the gate) -->

Every MCP tool goes through the REST API with the session's bearer
token — never around it — so this table is a *mapping*, not a second
surface. The full REST reference is the app's own `/openapi.json`
(browse it at `/redoc`); this page adds the two columns OpenAPI
cannot: which **token scope** each MCP tool requires, and which REST
call(s) it performs.

At runtime the tool list itself is scope-filtered: a session's token
only sees the tools it can use ([RUNTIME_RECIPE.md](RUNTIME_RECIPE.md)),
and `admin` implies every scope. A tool shown as *(client-side)*
composes other calls or local state instead of one fixed route.

## Read tools — scope: `read`

| MCP tool | REST call(s) | Summary |
|---|---|---|
| `activity_chain_status` | `GET /activity/chain` | Where the audit trail's hash chain stands (admin only). |
| `begin_run` | *(client-side)* | Switch this session's run identity: every write you make afterwards is … |
| `count_work` | `GET /issues/query/count` | How many issues a work query matches, ignoring paging — so a bounded … |
| `current_run` | *(client-side)* | Read the run identity this session is currently stamping on writes: |
| `embed_help` | `GET /embeds/help` | The embed vocabulary as data: every kind, its keys, and the limits. |
| `find_pages_by_title` | `GET /pages/by-title` | Find Mentor pages by their TITLE instead of a numeric id — the address you can … |
| `get_agent_budget` | `GET /users/{user_id}/budget` | Read a user's durable action budget — how many metered writes it may … |
| `get_agent_run_health` | `GET /activity/agent-runs` | Read the admin-only fleet cockpit rollup: each agent's bounded recent … |
| `get_attention_ranking` | `GET /attention/ranking` | Read the bounded, actor-filtered "Now" queue. Each row cites its … |
| `get_automation_rule` | `GET /automation/rules/{rule_id}` | Get one admin-only automation rule by id, including schedule progress, … |
| `get_fleet_active_work` | `GET /fleet/active-work` | Admin-only view of agent-held issue claims. Joins each lease to its … |
| `get_fleet_metrics` | `GET /fleet/metrics` | Read bounded issue throughput for this token's visible scope. Dates are … |
| `get_issue` | `GET /issues/{ref}` | Get one issue by numeric id ('12') or project key ('ATH-12'). The … |
| `get_issue_history` | `GET /issues/{issue_id}/history` | Read one issue's bounded operator run narrative. Every item cites … |
| `get_issue_lease` | `GET /issues/{issue_id}/lease` | Who holds the exclusive claim on this issue right now — {holder_id, holder_name, … |
| `get_issue_runbook` | `GET /issues/{issue_id}/runbook` | The issue's runbook page — accumulated learnings from earlier runs — or … |
| `get_issue_state` | `GET /issues/{issue_id}/state` | Reconstruct an issue's lifecycle state from the activity log. Pass … |
| `get_issue_work_context` | `GET /issues/{ref}/work-context` | Get a bounded, current packet containing one visible issue and its … |
| `get_page` | `GET /pages/{page_id}` | Get one Mentor page (title + Markdown body). The response includes the … |
| `get_page_version` | `GET /pages/{page_id}/versions/{version}` | Fetch one historical page revision by its version number (title + body as of … |
| `get_project_floor` | `GET /projects/{project_id}/floor` | A project's floor: every open issue is a chair. Occupied chairs … |
| `get_run_control` | `GET /run-controls/{control_id}` | One run control, for an admin or the agent it is addressed to. |
| `get_run_fork_contract` | `GET /activity/runs/{run_id}/fork` | Validate a fork point inside a parent run and return the child-run … |
| `get_run_lineage` | `GET /activity/runs/{run_id}/lineage` | Read a tagged run's causal tree: ancestors, the focal run's replayable … |
| `get_run_replay` | `GET /activity/runs/{run_id}/replay` | Export one run as its portable replay ARTIFACT: the events in replay … |
| `get_sprint` | `GET /sprints/{sprint_id}` | Get one sprint's descriptive fields and lifecycle state. Hidden and … |
| `link_graph` | `GET /{base}/{node_id}/graph` | The link neighbourhood around an issue or page, as you can see it. |
| `list_activity_runs` | `GET /activity/runs` | Reconstruct one actor's recent activity into runs. Explicit X-Athena-Run … |
| `list_automation_failures` | `GET /automation/rules` | Read the admin-only exception list of automation rules whose actions have … |
| `list_automation_rules` | `GET /automation/rules` | List every admin-only automation rule with its event or schedule … |
| `list_dispatches` | `GET /dispatches` | What Athena has handed to the executor, newest first. Filter by … |
| `list_issue_comments` | `GET /issues/{issue_id}/comments` | Read one issue's comment thread, oldest first — the discussion a … |
| `list_issue_links` | `GET /issues/{issue_id}/links` | Read an issue's dependency links — what it blocks, what blocks it, and … |
| `list_issues` | `GET /issues` | List Aegis issues, optionally filtered by status (open/in_progress/done), … |
| `list_labels` | `GET /labels` | List the shared label vocabulary (id, name, color). |
| `list_my_delegated_work` | `GET /delegations/me` | List issues delegated to the authenticated contributor. By default this … |
| `list_notifications` | `GET /notifications` | Read YOUR notification inbox — mentions, watched-issue changes, and … |
| `list_page_versions` | `GET /pages/{page_id}/versions` | The page's superseded revisions, newest first (the live page is NOT one of … |
| `list_pages` | `GET /spaces/{space_id}/pages` | List the pages in a space. Archived (soft-deleted) pages are hidden by … |
| `list_projects` | `GET /projects` | List Aegis projects (id, key, name). |
| `list_run_controls` | `GET /run-controls` | Run controls — operator requests on live runs and how their agents … |
| `list_run_events` | `GET /activity` | Replay one run: exactly the activity events tagged with this run id, … |
| `list_spaces` | `GET /spaces` | List Mentor spaces (id, key, name). |
| `list_sprints` | `GET /projects/{project_id}/sprints` | List a project's sprints, optionally filtered by state … |
| `list_subtasks` | `GET /issues/{issue_id}/children` | List the direct child issues (sub-tasks) nested under this issue. |
| `list_workers` | `GET /workers` | The worker registry — which agent processes are reporting, on what node, … |
| `my_desk` | `GET /desk` | START HERE. Your desk: who you are, what is asked of you, what you … |
| `my_office` | `GET /office` | Your cubicle. Athena's unique seat: at most one chair (one active … |
| `page_backlinks` | `GET /pages/{page_id}/backlinks` | What references this page — the INCOMING edges of the knowledge graph. Each … |
| `page_outgoing_links` | `GET /pages/{page_id}/outgoing-links` | What this page references — the OUTGOING edges of the knowledge graph (the … |
| `project_timeline` | `GET /projects/{project_id}/timeline` | A project's roadmap: sprint lanes, the issues in them, and the declared … |
| `query_help` | `GET /issues/query/help` | The work-query vocabulary as data: every field, its accepted values, … |
| `read_page_embeds` | `GET /pages/{page_id}` | Resolve a Mentor page's live embeds to DATA, as you. |
| `recent_events` | `GET /events` | Read the audit/event feed in order. Pass the last event id you saw as … |
| `related_items` | `GET /{base}/{node_id}/related` | What cites what this cites, but is NOT linked to it yet. |
| `resolve_embeds` | `POST /embeds/resolve` | Resolve embed directives in arbitrary text, as you. |
| `search` | `GET /search` | Full-text search across Aegis issues and Mentor pages. Optionally narrow … |
| `search_work` | `GET /issues` | Find issues with a work query — the precise way to ask for work. |
| `search_workspace` | `GET /search/workspace` | One ask across issues, pages, AND comments — use this when you do not … |
| `unlinked_mentions` | `GET /{base}/{node_id}/unlinked-mentions` | Documents whose text NAMES this issue/page without linking to it. |
| `verify_activity_chain` | `GET /activity/chain/verify` | Recompute a bounded window of the audit trail's hash chain (admin only). |
| `whoami` | `GET /users/me` | Who am I? Your identity (id, email, role, agent flag), the acting … |

## Aegis write tools — scope: `issue:write`

| MCP tool | REST call(s) | Summary |
|---|---|---|
| `archive_issue` | `POST /issues/{issue_id}/archive` | Archive (soft-delete) an issue: it's hidden from the default lists but the … |
| `assign_issue` | `PUT /issues/{issue_id}/assignee` | Assign an issue to a user id, or pass no assignee_id to unassign it. |
| `attach_label` | `POST /issues/{issue_id}/labels` | Attach an existing label (by id — see list_labels) to an issue. |
| `bulk_update_issues` | `POST /issues/bulk` | Apply the same change to MANY issues at once (one call instead of N). |
| `claim_issue` | `POST /issues/{issue_id}/claim` | Claim or renew an issue only against the exact root issue revision reviewed. |
| `comment_on_issue` | `POST /issues/{issue_id}/comments` | Add a comment to an issue, authored by the token's user. |
| `complete_claim` | `POST /issues/{issue_id}/complete` | Release the lease you hold. This does NOT mark the issue done. |
| `complete_sprint` | `POST /sprints/{sprint_id}/complete` | Move an active sprint to completed. Athena supplies today's date when … |
| `create_issue` | `POST /issues` | Create an Aegis issue. Omit status to use the target project's default; … |
| `create_label` | `POST /labels` | Create a label in the shared vocabulary. color is a #RRGGBB hex string. |
| `create_sprint` | `POST /projects/{project_id}/sprints` | Create a planned sprint in a project you created. Optional dates are … |
| `decline_delegation` | `POST /issues/{issue_id}/decline` | Decline an issue delegated to you (decline) — remove yourself from its … |
| `delegate_issue` | `POST /issues/{issue_id}/delegate` | Delegate an issue to an agent user. The human assignee remains accountable; … |
| `delete_sprint` | `DELETE /sprints/{sprint_id}` | Permanently delete an empty sprint. Set confirm_permanent=true after … |
| `detach_label` | `DELETE /issues/{issue_id}/labels/{label_id}` | Remove a label from an issue. Returns the updated issue. |
| `dispatch_to_icarus` | `POST /issues/{issue_id}/dispatch` | Hand an issue to the external execution fleet. |
| `link_issues` | `POST /issues/{issue_id}/links` | Declare a dependency FROM this issue to another (by id or key, e.g. |
| `resume_claim_handoff` | `POST /issues/{issue_id}/claim-handoffs/{handoff_token}/resume` | Explicitly acknowledge an open claim handoff as the exact current … |
| `set_issue_parent` | `PUT /issues/{issue_id}/parent` | Nest an issue under a parent issue (making it a sub-task), or pass no … |
| `set_issue_placement` | `PATCH /issues/{issue_id}` | Atomically set an issue's project and sprint. Always provide BOTH … |
| `set_issue_sprint` | `PUT /issues/{issue_id}/sprint` | Put an issue into a sprint (which must belong to the issue's own … |
| `start_playbook` | `POST /pages/{page_id}/start-playbook` | Turn a playbook page's checklist into REAL WORK: one parent issue … |
| `start_sprint` | `POST /sprints/{sprint_id}/start` | Move a planned sprint to active. A project may have only one active … |
| `unarchive_issue` | `POST /issues/{issue_id}/unarchive` | Restore a previously archived issue to the active lists. Returns the issue. |
| `unlink_issues` | `DELETE /issues/{issue_id}/links/{relation}/{target_id}` | Remove a dependency from this issue: the same relation used to add it … |
| `update_issue` | `PATCH /issues/{issue_id}` | Update an issue. Send only the fields to change. status is one of … |
| `update_sprint` | `PATCH /sprints/{sprint_id}` | Update a sprint's name, goal, or dates without changing its state. |
| `yield_claim` | `POST /issues/{issue_id}/yield` | Honestly release your exact active claim with a structured continuation … |

## Mentor write tools — scope: `docs:write`

| MCP tool | REST call(s) | Summary |
|---|---|---|
| `archive_page` | `POST /pages/{page_id}/archive` | Archive (soft-delete) a page: it's hidden from the space tree, navigation, … |
| `create_page` | `POST /spaces/{space_id}/pages` | Create a Mentor page in a space. Optionally nest it under parent_id (a … |
| `label_page` | `POST /pages/{page_id}/labels` | Attach an existing label (by id — see list_labels) to a Mentor page. |
| `record_run_learning` | `POST /issues/{issue_id}/learnings` | Write down what you learned, so the NEXT run starts knowing it. |
| `restore_page_version` | `POST /pages/{page_id}/versions/{version}/restore` | Restore a page's content to a prior version. Non-destructive: the current … |
| `unarchive_page` | `POST /pages/{page_id}/unarchive` | Restore a previously archived page to the active tree/nav/search. Returns … |
| `unlabel_page` | `DELETE /pages/{page_id}/labels/{label_id}` | Remove a label from a Mentor page. Returns the page. |
| `update_page` | `PATCH /pages/{page_id}` | Update a page's title and/or body. Each edit is snapshotted into the … |

## Cross-module write tools — scope: any write scope

| MCP tool | REST call(s) | Summary |
|---|---|---|
| `acknowledge_run_control` | `POST /run-controls/{control_id}/acknowledge` | Record that YOU read a control addressed to your run. |
| `advance_desk_cursor` | `POST /desk/cursor` | Record that you have handled every visible event up to `after_id`. |
| `complete_run_control` | `POST /run-controls/{control_id}/complete` | Complete a control addressed to your run. |
| `decline_run_control` | `POST /run-controls/{control_id}/decline` | Decline a control addressed to your run, with the reason the operator … |
| `heartbeat_agent_run` | `PUT /agent-runs/heartbeat` | Report that this authenticated agent is still working on `run_id`. |
| `link_mention` | `POST /{base}/{source_id}/link-mention` | Rewrite the SOURCE document so its first unlinked mention of the target … |
| `mark_notifications_read` | `POST /notifications/read_all` | Mark every unread notification in YOUR inbox as read (returns the … |
| `undo_action` | `POST /activity/{event_id}/undo` | Undo one activity event by applying its registered inverse. |
| `unwatch` | `DELETE /watches/{target_kind}/{target_id}` | Unsubscribe YOUR inbox from a target you are watching. Errors 404 if … |
| `watch` | `POST /watches` | Subscribe YOUR inbox to a target so you learn it changed without … |
| `worker_heartbeat` | `PUT /workers/heartbeat` | Register or refresh YOUR worker process, and find out whether the … |

## Operator tools — scope: `admin`

| MCP tool | REST call(s) | Summary |
|---|---|---|
| `agent_answerability` | `GET /fleet/answerability` | The per-agent ask-and-answer ledger (admin only). |
| `cancel_worker_kill` | `DELETE /workers/{worker_id}/kill` | Admin: withdraw a kill request the worker has not acknowledged yet. |
| `clear_agent_budget` | `DELETE /users/{user_id}/budget` | Admin: remove a user's budget, returning it to unlimited. Idempotent. |
| `create_automation_rule` | `POST /automation/rules` | Create an admin-only automation rule. Event rules use trigger_type='event' … |
| `create_run_control` | `POST /run-controls` | Admin: record a control request against a live run. |
| `decide_approval` | `POST /approvals/{request_id}/decision` | Approve or reject a pending approval request ('approve' \| 'reject'). |
| `delete_automation_rule` | `DELETE /automation/rules/{rule_id}` | Permanently delete an admin-only automation rule. Disable it instead when … |
| `list_approvals` | `GET /approvals` | The operator's approval queue — actions an agent asked to take that are … |
| `list_security_events` | `GET /security/events` | Recent boundary REFUSALS — someone probing where they may not go. |
| `list_users` | `GET /users` | List users (id, name, role) — useful for resolving an assignee. |
| `offboard_agent` | `POST /users/{user_id}/offboard` | Admin one-click offboard: demote user_id to viewer, revoke every session, … |
| `onboard_agent` | `POST /users/onboard_agent` | Admin: provision a NEW agent teammate in one audited move — create its … |
| `pause_agent` | `PUT /users/{user_id}/paused` | Admin: PAUSE user_id — every authenticated action it attempts is … |
| `request_worker_kill` | `POST /workers/{worker_id}/kill` | Admin: ask a worker to stop. |
| `resume_agent` | `PUT /users/{user_id}/paused` | Admin: RESUME a paused user_id — restores the account exactly as it … |
| `revoke_agent_tokens` | `DELETE /users/{user_id}/tokens` | Admin kill switch: revoke EVERY live API token held by user_id — the … |
| `set_agent_budget` | `PUT /users/{user_id}/budget` | Admin: cap how many metered writes a user may make per fixed window … |
| `set_approval_policy` | `PUT /approvals/policies/{user_id}` | Admin: require operator approval before this user may take an action … |
| `set_automation_rule_enabled` | `PATCH /automation/rules/{rule_id}` | Arm or disarm an admin-only automation rule without deleting its … |

---

*128 tools. Generated from `mcp/server.py` (`TOOL_SCOPES` + tool bodies) and `mcp/client.py` (verb + path literals); the registration path is fail-closed, so a tool missing here cannot exist in the server either.*
