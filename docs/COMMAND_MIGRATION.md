# Command-boundary migration

Athena's target architecture is **one command owns each write**. A command owns
domain authorization, validation, mutation, derived projections, and the audit
event in one SQLite transaction. REST and browser handlers translate transport
input/output; MCP uses REST.

This is a migration rule, not a completed-system claim. The inventory below is
the review-facing source of truth for the `0.1.0a1` line.

## Current inventory

| Area | Command-backed | Known migration debt |
|---|---|---|
| Issue core | Create; title/body/status/priority; assignee, project, and sprint placement; blocked-close policy enforcement/override; parent hierarchy; archive/restore; labels; contributors/delegation; typed dependency link/unlink; REST/browser comment create/edit/delete | None known |
| Projects | Issue placement uses the issue command; project create/edit/delete; blocked-close policy configuration; visibility flip (with the creator's roster row) and membership grant/revoke | None known |
| Sprints | Create, descriptive edit, start, complete, and delete — each atomic with its lifecycle event across REST and the browser | None known |
| Project statuses | Add/remove own the row change, its `project_status_added`/`project_status_removed` event, and the authorization — evaluated from live rows inside the write lock — in one transaction; the in-use guard and the delete no longer straddle a transaction boundary | Statuses cannot be renamed or reordered, so a typo is add-plus-remove; an agent that created a project may still configure its lifecycle, which is preserved prior behavior rather than a settled decision |
| Mentor | REST/browser page-comment create/edit/delete; full page lifecycle — create/edit/move/hard-delete/version-restore/archive-restore (atomic mutation + audit; edit adds If-Match optimistic concurrency); page label attach/detach (atomic join write + audit, REST/browser/MCP parity); full space lifecycle — create/edit/hard-delete/visibility/membership | None known |
| Attachments | REST/browser upload and direct delete share one command owner; metadata and activity/notifications commit or roll back together; publication rollback and post-commit unlink failures stay observable | Blob publication/unlink and hard-page-delete projections cannot join SQLite's transaction; attempt-all cleanup plus reconciliation detects but does not automatically repair residual divergence |
| Users and agents | User create, role/agent flag changes, token kill switch, complete offboard, and both password writes — admin reset and self-service change — each atomic with the session revocation it forces and its audit event | Login's transparent cost-upgrade rehash remains a bounded flow outside the command layer; it replaces no credential and records no lifecycle change |
| API tokens | Mint and revoke | None known in the token lifecycle |
| OIDC identities | Link and unlink | Provider exchange/discovery are transport/service flows rather than durable domain writes |
| Webhooks | Register, pause/resume, and delete | Delivery cursor/health updates are operational state and intentionally owned by the delivery subsystem |
| Automation | Event/schedule rule create, enable/disable, and delete; durable schedule claiming/progress; every one of the five rule actions now reaches a command owner — assign and set_status through `update_issue_as_automation`, comment through `comment_commands`, label and contributor through their own `*_as_automation` entry points | The system-policy bypass is a real bypass: a rule acts on the issue its trigger selected rather than as that issue's creator or assignee. Firings stay deliberately unmetered and ungated |
| Agent budgets | Set/clear own their write plus its audit event; the charge is folded into each metered command's transaction | Metering covers issue create/edit and page create/edit; other durable writes and automation firings are deliberately unmetered |
| Approvals | Policy set/clear and approve/reject each own their write plus its audit event; the consumption of an approval is folded into the gated command's own transaction; `issue.close` and `dispatch.request` are separate kinds, so an approval is only spendable by the intent the operator read | There is no expiry, bulk decide, or un-reject |
| Undo | Reversal reuses the ordinary command owner for the inverse, as the undoing actor, and the reversal link commits inside that command's transaction. `changed_status` reverses from the structured prior state migration 0055 already records; `assigned`/`unassigned` reverse from the 0068 assignee facts recorded beside each event — both gated on the change still being in force | Roughly forty verbs are still unclassified, which the registry reports honestly rather than guessing at; verbs classified one-way or trapdoor stay refused by design |
| Run learnings | Promotion owns its authorization (issue visibility, space/page visibility, run verification), the page create-or-edit through the existing page command, the 0066 binding row, and its `page_learning_recorded` event in one transaction | Nothing is promoted automatically; entries are append-only in practice and nothing summarizes or curates a growing runbook |
| Dispatch | The dispatch command owns authorization, the budget charge, the approval gate, the policy digest, the record, and its `dispatch_requested` event in one transaction; delivery is a post-commit side effect whose outcome is a follow-up event | No redelivery loop, no cancellation; every terminal state is the executor's claim, and Athena never verifies that work happened |
| Workers | Heartbeat, kill request, cancellation, acknowledgement, and stop each own their write plus its audit event; credentials are re-resolved inside the write transaction | The kill is cooperative — Athena records an instruction and cannot end a process. No worker deletion or expiry |
| Agent runs | Run/check-in operations use their dedicated command and run-context owners | Process-level kill remains roadmap work, not a shipped guarantee |

The table identifies ownership shape, not test coverage or security severity.
Before changing a listed legacy path, inspect both REST and browser adapters;
some pairs already share lower-level validation while still split the mutation
from activity emission.

## Personal state (a documented exception, not a gap)

Some writes are **personal state**: rows that belong to one user, change nothing
anyone else can observe, and carry no shared-history significance. Today that is
**saved filters**, **watches**, and **notification read-marks**. These do NOT get
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
