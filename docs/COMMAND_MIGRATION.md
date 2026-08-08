# Command-boundary migration

Athena's target architecture is **one command owns each write**. A command owns
domain authorization, validation, mutation, derived projections, and the audit
event in one SQLite transaction. REST and browser handlers translate transport
input/output; MCP uses REST.

This is a migration rule, not a completed-system claim. The inventory below is
the review-facing source of truth for the `0.1.0a1` line. The transport side of
the rule is mechanically enforced: `scripts/check_write_ownership.py` fails the
build if a transport (`web/*`, `mcp/*`, `*_api.py`) executes write SQL or calls
a data-module mutating helper that is neither a `*_commands.py` module nor one
of the designated writers it names (personal state, the login/session flow,
the documented owners below).

## Current inventory

| Area | Command-backed | Known migration debt |
|---|---|---|
| Issue core | Create; title/body/status/priority; assignee, project, and sprint placement; blocked-close policy enforcement/override; parent hierarchy; archive/restore; labels; contributors/delegation; typed dependency link/unlink; REST/browser comment create/edit/delete — **the comment commands now take a resolved actor and own their gate**: issue visibility and author-ownership (with the delete-only admin moderation override) are checked INSIDE the command's write transaction, so an ownership test and the write it guards can no longer straddle a boundary, and a caller reaching the command directly is refused rather than trusted. Automation keeps a narrow documented bypass (`create_comment_as_automation`), mirroring `update_issue_as_automation` | None known in the comment path |
| Projects | Issue placement uses the issue command; project create/edit/delete; blocked-close policy configuration; visibility flip (with the creator's roster row) and membership grant/revoke | None known |
| Sprints | Create, descriptive edit, start, complete, and delete — each atomic with its lifecycle event across REST and the browser | None known |
| Project statuses | Add/remove own the row change, its `project_status_added`/`project_status_removed` event, and the authorization — evaluated from live rows inside the write lock — in one transaction; the in-use guard and the delete no longer straddle a transaction boundary | Statuses cannot be renamed or reordered, so a typo is add-plus-remove; an agent that created a project may still configure its lifecycle, which is preserved prior behavior rather than a settled decision |
| Mentor | REST/browser page-comment create/edit/delete; full page lifecycle — create/edit/move/hard-delete/version-restore/archive-restore (atomic mutation + audit; edit adds If-Match optimistic concurrency); page label attach/detach (atomic join write + audit, REST/browser/MCP parity); full space lifecycle — create/edit/hard-delete/visibility/membership; page-from-template create and the idempotent daily note, both ordinary metered page-create commands | The page and page-comment commands keep authorization — space/page visibility, comment author-ownership — at the transport boundary (`access.can_see_space` and friends in the routes) and trust a bare actor id; the command owns the write and its audit, not the gate. Both module docstrings say so plainly. A future caller reaching the command directly bypasses the check until the gate moves inside |
| Labels (vocabulary) | Attach/detach on issues and pages is command-backed and audited (see the Issue core and Mentor rows) | Standalone `POST /labels` has no command owner and records no audit event: any write-role actor holding the `issue:write` scope grows the shared vocabulary, unaudited and unmetered — accepted for now, since the vocabulary is reference data and the audited write is the attach. A duplicate name is a 409 — the pre-check is backed by the case-insensitive UNIQUE constraint (0007), so a lost create race maps `IntegrityError` to the same 409 rather than a 500 |
| Attachments | REST/browser upload and direct delete share one command owner; metadata and activity/notifications commit or roll back together; publication rollback and post-commit unlink failures stay observable | Blob publication/unlink and hard-page-delete projections cannot join SQLite's transaction; attempt-all cleanup plus reconciliation detects but does not automatically repair residual divergence |
| Event sources (forge) | Register, pause/resume, and delete are command-backed (`event_source_commands`), each atomic with its audit event; the one-time secret never reaches the trail | Authorization — admin role **and** admin token scope — lives in the transport dependency (`admin_actor`); the command takes a bare actor id and does not re-authorize. Delivery (`POST /forge/{name}`) is deliberately not a command: it is the unauthenticated HMAC-verified path that writes *imported* history, charged against the anonymous rate limiter before the source is looked up |
| Query, embeds, and the knowledge graph | No durable writes to own: the query grammar, embed rendering, and graph traversal are read-only surfaces. The graph's "link it" action is an ordinary audited page edit through the page command; unlinked mentions propose edges and never create one | None — the honesty risk here is a surface that *looks* like it writes; it does not |
| Users and agents | User create, role/agent flag changes, token kill switch, complete offboard, and both password writes — admin reset and self-service change — each atomic with the session revocation it forces and its audit event. First-admin eligibility, forced role selection, insert, and audit also share one immediate transaction | The post-bootstrap admin gate for ordinary create/role/agent edits remains at the transport boundary. Login's transparent cost-upgrade rehash is a bounded flow outside the command layer; it replaces no credential and records no lifecycle change |
| API tokens | Mint and revoke | None known in the token lifecycle |
| OIDC identities | Link and unlink | Provider exchange/discovery are transport/service flows rather than durable domain writes |
| Webhooks | Register, pause/resume, and delete | Delivery cursor/health updates are operational state and intentionally owned by the delivery subsystem |
| Automation | Event/schedule rule create, enable/disable, and delete; durable schedule claiming/progress; every one of the five rule actions now reaches a command owner — assign and set_status through `update_issue_as_automation`, comment through `create_comment_as_automation`, label and contributor through their own `*_as_automation` entry points | The system-policy bypass is a real bypass: a rule acts on the issue its trigger selected rather than as that issue's creator or assignee. Firings stay deliberately unmetered and ungated |
| Agent budgets | Set/clear own their write plus its audit event; the charge is folded into each metered command's transaction | Metering covers issue create/edit, page create/edit (including page-from-template and the daily note), and dispatch requests; other durable writes and automation firings are deliberately unmetered |
| Approvals | Policy set/clear and approve/reject each own their write plus its audit event; the consumption of an approval is folded into the gated command's own transaction; `issue.close` and `dispatch.request` are separate kinds, so an approval is only spendable by the intent the operator read | There is no expiry, bulk decide, or un-reject |
| Undo | Reversal reuses the ordinary command owner for the inverse, as the undoing actor, and the reversal link commits inside that command's transaction. `changed_status` reverses from the structured prior state migration 0055 already records; `assigned`/`unassigned` reverse from the 0068 assignee facts recorded beside each event — both gated on the change still being in force | Roughly forty verbs are still unclassified, which the registry reports honestly rather than guessing at; verbs classified one-way or trapdoor stay refused by design. The undo *route* runs on plain authentication and enforces no scope itself — safe today only because every classified verb's inverse runs through an issue command that checks role and `issue:write` inside its own transaction. That makes command-owned authorization a **precondition for classifying a verb two-way**: a mentor verb must not be classified until the page commands own their gate (see the Mentor row), or a read-scoped token could undo a page edit through a command that trusts its caller |
| Run learnings | Promotion owns its authorization (issue visibility, space/page visibility, run verification), the page create-or-edit through the existing page command, the 0066 binding row, and its `page_learning_recorded` event in one transaction | Nothing is promoted automatically; entries are append-only in practice and nothing summarizes or curates a growing runbook |
| Dispatch | The dispatch command owns authorization, the budget charge, the approval gate, the policy digest, the record, and its `dispatch_requested` event in one transaction; the callback command owns evidence/terminal transitions and their audit events atomically; delivery is a post-commit side effect whose outcome is a follow-up event | No redelivery loop, no cancellation, and callback v1 has one immutable evidence pointer rather than a sender sequence; every terminal state is the executor's claim, and Athena never verifies that work happened |
| Workers | Heartbeat, kill request, cancellation, acknowledgement, and stop each own their write plus its audit event; credentials are re-resolved inside the write transaction | The kill is cooperative — Athena records an instruction and cannot end a process. No worker deletion or expiry |
| Agent runs | Run/check-in operations use their dedicated command and run-context owners | Process-level kill remains roadmap work, not a shipped guarantee |
| Personal state | None — by design, and bounded by the rules in the next section: saved filters, watches, notification read-marks, and page drafts are owner-scoped in their mutation SQL (single-statement writes, or one immediate transaction where a read feeds the write), record no audit events, and have exactly one data-module writer each | The category is an exception, not a lane: anything that fails those rules is not personal state and needs a command owner like every other write |

The table identifies ownership shape, not test coverage or security severity.
Before changing a listed legacy path, inspect both REST and browser adapters;
some pairs already share lower-level validation while still split the mutation
from activity emission.

## Personal state (a documented exception, not a gap)

Some writes are **personal state**: rows that belong to one user, change nothing
anyone else can observe, and carry no shared-history significance. Today that is
**saved filters**, **watches**, **notification read-marks**, and **page drafts**.
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
