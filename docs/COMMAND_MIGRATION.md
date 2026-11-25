# Command-boundary migration

Athena's target architecture is **one command owns each write**. A command owns
domain authorization, validation, mutation, derived projections, and the audit
event in one SQLite transaction. REST and browser handlers translate transport
input/output; MCP uses REST.

This is a migration rule, not a completed-system claim. The inventory below is
the review-facing source of truth for the `0.1.0a1` line. The transport side of
the rule is mechanically enforced: `scripts/check_write_ownership.py` fails the
build if a transport executes write SQL or calls a data-module mutating helper
that is neither a `*_commands.py` module nor one of the designated writers it
names (personal state, the login/session flow, the inbound forge delivery path,
and the development startup's schema migration in `main.py`). A transport is
any module that defines an `APIRouter` or registers routes, middleware or
exception handlers on the app, which includes `aegis/api.py`, `mentor/api.py`
and `main.py`, plus everything under `web/` and `mcp/` and every `*_api.py`.
The app's refusal handlers in `main.py` record through commands: the pending
approval ask (`approval_commands.open_request`), budget exhaustion
(`budget_commands.record_exhaustion`) and scope denial
(`security_event_commands.record_failure`). The atomic side is enforced too:
`scripts/check_transaction_commits.py` fails on a call inside a
`db.transaction` block that commits, and `db.transaction` raises at exit if
anything inside it committed. Outside `core/db.py` the same check fails on a
writer that commits whatever it is asked and on a transaction opened by hand,
so every write either takes `commit` or goes through `db.transaction` and
composes inside a command; the exceptions are named, with their reasons, in
the script's `ALLOWED_OWN_TRANSACTIONS` (G2). Parity between the transports is enforced as
well: `scripts/check_surface_parity.py` fails when a command function is
reached from only the cockpit or only the REST API and
`scripts/surface_parity.toml` does not say why.

## Current inventory

The "Known migration debt" column records what only a reader can see. Two kinds
of debt the code states mechanically are generated below it, under
[Generated debtors](#generated-debtors), and win where this column disagrees.

| Area | Command-backed | Known migration debt |
|---|---|---|
| Issue core | Create; title/body/status/priority; assignee, project, and sprint placement; blocked-close policy enforcement/override; parent hierarchy; archive/restore; labels; contributors/delegation; typed dependency link/unlink; REST/browser comment create/edit/delete — **the comment commands now take a resolved actor and own their gate**: issue visibility and author-ownership (with the delete-only admin moderation override) are checked INSIDE the command's write transaction, so an ownership test and the write it guards can no longer straddle a boundary, and a caller reaching the command directly is refused rather than trusted. Automation keeps a narrow documented bypass (`create_comment_as_automation`), mirroring `update_issue_as_automation` | None known in the comment path beyond the automation entry point, which takes a bare actor id by design (see [Generated debtors](#generated-debtors)) |
| Projects | Issue placement uses the issue command; project create/edit/delete, each recording its lifecycle event with `commit=False` so the event commits with its write or not at all; delete also owns the no-issues/no-sprints refusal, checked inside its immediate transaction and raised as a typed `conflict` that REST and the browser both map to 409; blocked-close policy configuration; visibility flip (with the creator's roster row) and membership grant/revoke | Every project command but the blocked-close policy still takes a bare actor id rather than the resolved actor (see [Generated debtors](#generated-debtors)). Creator-only authorization and the name/key uniqueness pre-checks still run at the transport, outside the command's transaction, so a create or rename racing another onto the same name or key surfaces the constraint failure as a 500 (it writes nothing). Before 2026-11-22 the lifecycle events committed ahead of their write (review ARCH-1), so a delete that then failed at the foreign key left a permanent `deleted_project` event for a live project; a trail written before then can carry one |
| Sprints | Create, descriptive edit, start, complete, and delete — each atomic with its lifecycle event across REST and the browser | Every sprint command still takes a bare actor id rather than the resolved actor (slice rule 1; see [Generated debtors](#generated-debtors)) |
| Project statuses | Add/remove own the row change, its `project_status_added`/`project_status_removed` event, and the authorization — evaluated from live rows inside the write lock — in one transaction; the in-use guard and the delete no longer straddle a transaction boundary | Statuses cannot be renamed or reordered, so a typo is add-plus-remove; an agent that created a project may still configure its lifecycle, which is preserved prior behavior rather than a settled decision |
| Mentor | REST/browser page-comment create/edit/delete; full page lifecycle — create/edit/move/hard-delete/version-restore/archive-restore (atomic mutation + audit; edit adds If-Match optimistic concurrency); page label attach/detach (atomic join write + audit, REST/browser/MCP parity); full space lifecycle — create/edit/hard-delete/visibility/membership; page-from-template create and the idempotent daily note, both ordinary metered page-create commands | **The page and page-comment commands now own their gate**: each takes a resolved actor and checks space/page visibility (hidden reads as missing, never 403) — and, for comments, author-ownership with the delete-only admin moderation override — INSIDE its own write transaction, mirroring the aegis comment commands. Page delete also owns the no-cascade children rule and reads the audit title from the row under its own lock. Role and token scope stay at each transport (`docs_write_actor`), as they do for the aegis comment commands |
| Labels (vocabulary) | Attach/detach on issues and pages is command-backed and audited (see the Issue core and Mentor rows). Standalone `POST /labels` is `label_commands.create_label`: the insert and its `label_created` event commit together, and a duplicate name — including the loser of a create race — is a conflict (409) | Vocabulary growth stays unmetered, the same deliberate choice as the other non-issue writes listed under Agent budgets |
| Attachments | REST/browser upload and direct delete share one command owner; metadata and activity/notifications commit or roll back together; publication rollback and post-commit unlink failures stay observable | Blob publication/unlink and hard-page-delete projections cannot join SQLite's transaction; attempt-all cleanup plus reconciliation detects but does not automatically repair residual divergence |
| Event sources (forge) | Register, pause/resume, and delete are command-backed (`event_source_commands`), each atomic with its audit event; the one-time secret never reaches the trail | **The commands now own the admin gate** — role and admin token scope, checked against the resolved actor exactly as `run_control_commands._admin` does; the transport's `admin_actor` still refuses first for its wire statuses. Delivery (`POST /forge/{name}`) is deliberately not a command: it is the unauthenticated HMAC-verified path that writes *imported* history, charged against the anonymous rate limiter before the source is looked up |
| Query, embeds, and the knowledge graph | No durable writes to own: the query grammar, embed rendering, and graph traversal are read-only surfaces. The graph's "link it" action is an ordinary audited page edit through the page command; unlinked mentions propose edges and never create one | None — the honesty risk here is a surface that *looks* like it writes; it does not |
| Users and agents | User create, role/agent flag changes, token kill switch, complete offboard, and both password writes — admin reset and self-service change — each atomic with the session revocation it forces and its audit event. First-admin eligibility, forced role selection, insert, and audit also share one immediate transaction | The post-bootstrap admin gate for ordinary create/role/agent edits remains at the transport boundary. Login's transparent cost-upgrade rehash is a bounded flow outside the command layer; it replaces no credential and records no lifecycle change |
| API tokens | Mint and revoke | Both commands still take a bare actor id rather than the resolved actor (slice rule 1; see [Generated debtors](#generated-debtors)) |
| OIDC identities | Link and unlink | Provider exchange/discovery are transport/service flows rather than durable domain writes |
| Webhooks | Register, pause/resume, and delete | Delivery cursor/health updates are operational state and intentionally owned by the delivery subsystem |
| Automation | Event/schedule rule create, enable/disable, and delete; durable schedule claiming/progress; every one of the five rule actions now reaches a command owner — assign and set_status through `update_issue_as_automation`, comment through `create_comment_as_automation`, label and contributor through their own `*_as_automation` entry points | The system-policy bypass is a real bypass: a rule acts on the issue its trigger selected rather than as that issue's creator or assignee. Firings stay deliberately unmetered and ungated |
| Agent budgets | Set/clear (`budget_commands`) own their write plus its audit event; the charge is folded into each metered command's transaction | Metering covers issue create/edit, page create/edit (including page-from-template and the daily note), and dispatch requests; other durable writes and automation firings are deliberately unmetered |
| Approvals | Policy set/clear and approve/reject (`approval_commands`) each own their write plus its audit event and refuse with a transport-neutral kind; the consumption of an approval is folded into the gated command's own transaction; `issue.close` and `dispatch.request` are separate kinds, so an approval is only spendable by the intent the operator read | There is no expiry, bulk decide, or un-reject |
| Undo | Reversal reuses the ordinary command owner for the inverse, as the undoing actor, and the reversal link commits inside that command's transaction. `changed_status` reverses from the structured prior state migration 0055 already records; `assigned`/`unassigned` reverse from the 0068 assignee facts recorded beside each event — both gated on the change still being in force | Roughly forty verbs are still unclassified, which the registry reports honestly rather than guessing at; verbs classified one-way or trapdoor stay refused by design. The undo *route* runs on plain authentication and enforces no scope itself, so every classified verb's inverse must bring its own gate: issue verbs run through commands that check role and `issue:write` inside their transaction, and mentor verbs pair `page_undo`'s role/scope check with the page commands' own visibility check. The standing rule for classifying any future verb two-way is that its inverse path must supply role, scope, AND visibility — from the command owner where it has them, from the compensator where it does not |
| Run learnings | Promotion (`run_learning_commands.record_learning`) owns its authorization (issue visibility, space/page visibility, run verification), the page create-or-edit through the existing page command, the 0066 binding row, and its `page_learning_recorded` event in one transaction | Nothing is promoted automatically; entries are append-only in practice and nothing summarizes or curates a growing runbook |
| Dispatch | The dispatch command owns authorization, the budget charge, the approval gate, the policy digest, the record, and its `dispatch_requested` event in one transaction; the callback command owns evidence/terminal transitions and their audit events atomically; delivery is a post-commit side effect whose outcome is a follow-up event | No redelivery loop, no cancellation, and callback v1 has one immutable evidence pointer rather than a sender sequence; every terminal state is the executor's claim, and Athena never verifies that work happened |
| Workers | Heartbeat, kill request, cancellation, acknowledgement, and stop each own their write plus its audit event; credentials are re-resolved inside the write transaction | The kill is cooperative — Athena records an instruction and cannot end a process. No worker deletion or expiry |
| Agent runs | Run/check-in operations use their dedicated command and run-context owners | Process-level kill remains roadmap work, not a shipped guarantee |
| Personal state | None — by design, and bounded by the rules in the next section: saved filters, watches and their notification preferences, notification read-marks, and page and issue drafts are owner-scoped in their mutation SQL (single-statement writes, or one immediate transaction where a read feeds the write), record no audit events, and have exactly one data-module writer each | The category is an exception, not a lane: anything that fails those rules is not personal state and needs a command owner like every other write |

The table identifies ownership shape, not test coverage or security severity.
Before changing a listed legacy path, inspect both REST and browser adapters;
some pairs already share lower-level validation while still split the mutation
from activity emission.

## Generated debtors

These two tables are generated from the code by
`scripts/generate_command_debtors.py`; `tests/test_command_debtors_doc.py` fails
when they drift. Regenerate, never edit them.

**Commands that take a bare actor id** (slice rule 1 below). The rule: every
public top-level function of a `*_commands.py` module under `src/athena/` that
has a parameter named `actor_id` and none named `actor`. A name ending in
`_as_automation` is the automation engine's documented entry point, listed in
its own column rather than dropped. The rule cannot tell privileged work from a
recorder that only attributes an event, so the app-level refusal recorders
(`record_exhaustion`, `record_failure`, `open_request`) are listed too.

<!-- BEGIN GENERATED debtors:bare-actor-id (scripts/generate_command_debtors.py; do not edit by hand) -->
| Command module | Takes `actor_id`, not the actor | Automation entry points |
|---|---|---|
| `athena.aegis.automation_commands` | `create_rule`, `set_rule_enabled`, `delete_rule` | — |
| `athena.aegis.comment_commands` | — | `create_comment_as_automation` |
| `athena.aegis.issue_commands` | — | `update_issue_as_automation`, `attach_label_as_automation`, `add_contributor_as_automation` |
| `athena.aegis.project_commands` | `create_project`, `update_project`, `delete_project`, `set_project_visibility`, `add_project_member`, `remove_project_member` | — |
| `athena.aegis.sprint_commands` | `create_sprint`, `update_sprint`, `start_sprint`, `complete_sprint`, `delete_sprint` | — |
| `athena.core.approval_commands` | `decide`, `set_policy`, `clear_policy`, `open_request` | — |
| `athena.core.budget_commands` | `set_budget`, `clear_budget`, `record_exhaustion` | — |
| `athena.core.oidc_commands` | `link_identity`, `unlink_identity` | — |
| `athena.core.security_event_commands` | `record_failure` | — |
| `athena.core.token_commands` | `mint_token`, `revoke_token` | — |
| `athena.core.user_commands` | `create_user`, `set_user_role`, `set_user_agent` | — |
| `athena.core.webhook_commands` | `register_webhook`, `set_webhook_active`, `delete_webhook` | — |
| `athena.mentor.space_commands` | `create_space`, `edit_space`, `delete_space`, `set_space_visibility`, `add_space_member`, `remove_space_member` | — |

*42 commands in 13 modules.*
<!-- END GENERATED debtors:bare-actor-id -->

**Writes outside a command.** Every exception in `ALLOWED_TRANSPORT_WRITES` of
`scripts/check_write_ownership.py`: a transport that calls a data-module writer
which is not a command. The check fails the build on any other such call, and on
a transport-scoped entry that stops matching one, so this list is complete for
transports. Each entry carries the reason the doctrine permits it: personal state
(next section), the login and OIDC flow, the authentication dependency's refusal
audit, forge delivery and the startup migration are documented as deliberate.

<!-- BEGIN GENERATED debtors:outside-command (scripts/generate_command_debtors.py; do not edit by hand) -->
| Transport | Writer it calls | Why the ownership check permits it |
|---|---|---|
| any transport | `athena.aegis.saved_filters.create_filter` | personal state |
| any transport | `athena.aegis.saved_filters.update_filter` | personal state |
| any transport | `athena.aegis.saved_filters.delete_filter` | personal state |
| any transport | `athena.core.notifications.mark_read` | personal state |
| any transport | `athena.core.notifications.mark_all_read` | personal state |
| any transport | `athena.core.notifications.watch` | personal state |
| any transport | `athena.core.notifications.unwatch` | personal state |
| any transport | `athena.core.notifications.set_preference` | personal state |
| any transport | `athena.core.notifications.delete_preference` | personal state |
| any transport | `athena.mentor.page_drafts.save_draft` | personal state |
| any transport | `athena.mentor.page_drafts.discard_draft` | personal state |
| any transport | `athena.aegis.issue_drafts.save_draft` | personal state |
| any transport | `athena.aegis.issue_drafts.discard_draft` | personal state |
| `athena.web.auth` | `athena.core.sessions.create_session` | session mint on login; login flow documented outside the command layer |
| `athena.web.auth` | `athena.core.sessions.destroy_session` | session revocation on logout/login; same documented flow |
| `athena.web.auth` | `athena.core.oidc.start_login` | OIDC login state; a provider-exchange flow, not a durable domain write |
| `athena.web.auth` | `athena.core.oidc.take_login_state` | OIDC login state; same provider-exchange flow |
| `athena.web.auth` | `athena.core.oidc_flow.provision_or_link` | first-login SSO provisioning; documented OIDC service flow |
| `athena.web.auth` | `athena.core.users.verify_credentials` | transparent password rehash on login; the documented bounded exception |
| any transport | `athena.core.identity.current_actor` | refusal audit inside the authentication dependency |
| any transport | `athena.core.identity.optional_actor` | refusal audit inside the authentication dependency |
| `athena.aegis.forge_api` | `athena.aegis.forge.land_delivery` | the HMAC-verified delivery path writes imported history by design |
| `athena.main` | `athena.core.db.migrate` | startup schema migration in the app lifespan; DDL, not a domain write |

*23 writers.*
<!-- END GENERATED debtors:outside-command -->

What neither table can see: authorization or uniqueness checks that run at the
transport outside the command's transaction (rule 2), a command that takes the
actor and still trusts it, a write and its audit event that do not commit
together (rule 3), REST and browser adapters that call different code (rule 4),
writes from modules that are not transports, and dynamic dispatch. Those stay in
the hand-written column above.

## Personal state (a documented exception, not a gap)

Some writes are **personal state**: rows that belong to one user, change nothing
anyone else can observe, and carry no shared-history significance. Today that is
**saved filters**, **watches and their notification preferences**, **notification
read-marks**, and **page and issue drafts**. A notification preference is owned by
the same `(user, kind, target)` watch and is deleted with it; it cannot exist first
and silently reactivate after a later re-watch.
These do NOT get
a command owner or audit events — recording "you marked your own inbox read" on
the append-only trail would be noise, and an audit log is load-bearing precisely
because it is not a dumping ground.

The category is not an anything-goes lane. Its rules are as mandatory as the
command rule above:

1. **Owner-scoped SQL is mandatory.** Every mutation carries the owner in its
   own `WHERE` clause (`… WHERE id = ? AND owner_id = ?`, 0 rows → 404), inside
   one immediate transaction. Transports never fetch-then-check ownership
   outside a transaction — SQLite reuses rowids, and a stale check can land on
   another user's row.
2. **No audit events.** Personal state records nothing on the activity trail.
3. **One data-module writer.** The owning data module (e.g.
   `aegis/saved_filters.py`, `core/notifications.py`) is the single writer;
   REST and browser adapters call it and translate the result.

Anything that fails these rules is not personal state — it needs a command
owner like every other write.

## The `capacity` kind maps to 429 — and the two exceptions, by name

New command modules use the transport-neutral **error-kind dialect**
(`not_found`, `invalid`, `conflict`, `forbidden`, `unauthorized`, `capacity`)
with a `STATUS_BY_KIND` map in the route module. Every kind but one has an
obvious status. `capacity` did not, and it was re-argued in more than one review,
so the convention is recorded here rather than rediscovered:

> **`capacity` → 429 in new modules.** It means *you asked for more than this
> surface will do right now* — a bound was hit, the request was well-formed, and
> the same request may succeed later or smaller. That is what 429 says. A 409
> would claim a conflict with existing state, which a bound is not.

Two shipped surfaces answer differently, and they stay that way:

| Surface | Status | Why it is not being changed |
|---|---|---|
| `POST /agent-runs/check-ins` (`agent_run_commands`) | **409** | Its capacity is "too many distinct run ids", which really is a conflict with existing state — the caller's fix is to reuse a run it already has, not to wait. The message says so (`refresh an existing run_id`), and the status is a shipped wire contract |
| `POST /pages/{id}/start-playbook` (`playbook_commands`) | **429** | The convention above: more than 50 steps is a bound on the surface |

So the rule for a new module is: **429 unless the bound is genuinely a conflict
with state the caller already owns** — and if you believe yours is the second
case, say why in the module docstring, because the next reader will otherwise
read it as a mistake.

This paragraph exists to end the per-PR relitigating. Changing either shipped
status is a wire-contract change and needs its own decision, not a drive-by.

## Rules for migration slices

A migration is complete only when:

1. the command accepts a resolved actor rather than trusting a bare actor id for
   privileged work;
2. authorization and visibility checks execute inside the command boundary;
3. the domain write, projections, notification facts, and activity event commit
   or roll back together;
4. REST and browser adapters call the same command;
5. MCP reaches that REST behavior without a parallel mutation;
6. no-op and idempotent behavior is explicitly defined; and
7. tests cover success, rejection, rollback, attribution, and transport parity.

## Why migration is incremental

Athena is a working local alpha with broad route coverage. A flag-day rewrite
would create a large, hard-to-review authorization diff. Vertical slices keep
the system runnable and make each trust-boundary change independently testable.
New writes must use commands immediately; legacy paths move when touched or when
their risk ranks them ahead of product work.
