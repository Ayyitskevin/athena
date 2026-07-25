# Changelog

Notable changes to Athena are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and package version
markers follow semantic versioning while the project remains pre-1.0. As of
2026-07-23, Athena has no tags or GitHub releases: version-like headings below are
untagged package/development milestones, not published releases.

## [Unreleased]

### Added

- **Run learnings close the memory loop.** VISION's fifth step promised that
  corrections "feed back into Mentor as durable context the agents read next
  time"; Mentor pages were already read by agents, but nothing ever wrote back, so
  every run started from the knowledge the last one had. A human or an agent can
  now promote what a run learned into the issue's **runbook** — one Mentor page
  bound to that issue (migration 0066). The entry references the issue, so the link
  index, the issue's backlinks, and the next agent's work-context packet pick it up
  **through machinery that already existed**; nobody had to wire the feedback path.
  Three constraints are deliberate. **Promotion is explicit** — nothing fires on
  completion or yield, because handoff text is untrusted advisory input and the
  operator decides what earns a place in the knowledge base. **Promoted text is
  quoted, not merged** — it is stored as a blockquote under an attribution header
  Athena writes, so a summary containing its own headings renders inside the quote
  instead of forging a second attribution beside the real one, and nothing in it is
  ever executed. **Provenance is verified** — a named `run_id` must be a run that
  actually exists and is visible to the recorder, or the promotion is refused
  rather than recorded with invented attribution. The runbook binding is a row
  rather than a title lookup, so renaming the page cannot silently fork the memory
  in two. Surfaced through `POST /issues/{id}/learnings`, `GET
  /issues/{id}/runbook`, MCP `record_run_learning` / `get_issue_runbook`, and a
  form on the run lineage view — where looking at what a run did is exactly the
  moment an operator knows what the next one should be told. See
  [docs/RUN_LEARNINGS.md](docs/RUN_LEARNINGS.md).

- **Security signals have a surface.** Failed logins, revoked tokens still being
  presented, scope denials, and paused-account refusals have been recorded on the
  activity trail since they were added — and were readable only by an operator who
  already knew the four verb names and thought to filter for them. Probing before
  compromise is exactly the signal that must not require knowing to grep, so it now
  has `GET /security/events`, `GET /security/counts`, MCP `list_security_events`,
  and an admin **Security signals** page. Admin-only, with a closed verb vocabulary
  so the surface cannot quietly become a general activity reader wearing a security
  name, and zero-filled counts so a quiet fleet reads as an explicit zero.
- **One fleet-attention rollup on the dashboard.** Athena's exception surfaces grew
  one at a time and each landed on its own page, so an operator expected to steer
  by exception had to know six places to look. An admin-only card now counts claims
  needing attention, approvals waiting, workers told to stop, failing automation
  rules, failing webhooks, budget ceilings hit, and boundary refusals — each linking
  to the surface that owns it. **The card computes nothing**: every number comes
  from the surface that owns it, so it can never disagree with the page it sends
  you to. Standing state is not window-bounded (an unanswered approval is still
  unanswered a month later); event-counted signals are, because a probe from months
  ago is not this morning's alarm. A quiet fleet reads "nothing is asking for you
  right now — that is what the last N hours recorded, not a promise that every agent
  is healthy". `base.html` also gained the Mission Control and Security links it
  never had. See [docs/EXCEPTION_SURFACES.md](docs/EXCEPTION_SURFACES.md).
- **A worker registry with a cooperative kill request** — Athena models *who* an
  agent is, and check-ins prove a credential reported a *run*; neither answers
  "which of my agent processes are up, on what box, and can I tell one to stop?"
  A worker now registers itself by heartbeat (`PUT /workers/heartbeat`), declaring
  a node label and self-declared capabilities, and an admin can ask it to stop.
  **The kill is cooperative, and the schema says so.** Athena cannot signal a
  foreign OS process, so it records three separate facts — the operator *asked*,
  the worker *said it heard*, the worker *said it stopped* — rather than one
  `killed` flag that would invent certainty. The worker learns of the request on
  its next heartbeat (`kill_requested: true`); honoring it is the worker's job.
  A worker that goes quiet is **stale**, never terminated; one that acknowledged
  and kept reporting is surfaced as `acknowledged_but_reporting`, because hiding
  that would undo the point. A restart does not cancel the instruction, asking
  twice does not reset how long it has been ignored, and a request can be
  withdrawn only until it is acknowledged. Registration requires an agent account
  holding a live write-scoped bearer token, re-resolved inside the write
  transaction; a browser session, a human's token, and a read-only token are all
  refused. First registration is audited, refreshes are not, and every kill
  request, cancellation, acknowledgement, and stop is. Surfaced through
  `GET`/`POST`/`DELETE` on `/workers`, MCP `worker_heartbeat` / `list_workers` /
  `request_worker_kill` / `cancel_worker_kill`, and a per-agent worker list plus a
  "Told to stop" queue in the cockpit. Worker events are admin-only on the trail.
  See [docs/WORKERS.md](docs/WORKERS.md) — including how pause interacts with a
  kill request, and why there is still no process-level kill.
- **Undo by compensation** — VISION promises the operator can undo what an agent
  did; ARCHITECTURE promises the trail is append-only. Undo never deletes or edits
  a row: reversing event *N* runs the inverse as a new, fully audited forward
  command whose event carries `reverses_event_id = N` (migration 0064), so history
  gains two rows and a replay shows the action *and* its reversal. Authorization is
  **re-evaluated, never inherited** — the compensator runs as the undoing actor
  through the ordinary command owner, so undo is not a back door into someone
  else's write, and it can itself be refused by a budget or an approval gate like
  any other write. An undo that would change nothing is a **refusal**, not a
  cheerful no-op: every compensator is idempotent at the domain layer, so "nothing
  was recorded" means the effect is no longer in force. Single use is enforced by a
  partial unique index rather than a read-then-write check, so two concurrent undos
  cannot both compensate. Reversible today: issue archive/unarchive and
  label/unlabel, page archive/unarchive and label/unlabel — four pairs whose
  inverse needs no prior state. Everything else is refused *with its class*:
  one-way (a comment people read, a published attachment), trapdoor (a destroyed
  row), or unclassified. Imported history and events the actor cannot see are never
  undoable. Surfaced through `POST /activity/{event_id}/undo`, the
  `reverses_event_id` field on every event read, MCP `undo_action`, and an Undo
  control on reversible rows of the activity feed. See [docs/UNDO.md](docs/UNDO.md)
  — including why `changed_status` and `assigned` are *not* reversible: their
  inverse needs the prior value, and `detail` is human-readable prose rather than
  structured before/after. This is undo by compensation for a bounded set of
  actions, not general undo.
- Human-in-the-loop **approval gates** — VISION's Intervene step promised the
  operator can "approve/reject risky actions"; the only gate before this was the
  per-project blocked-close policy, which can refuse but cannot *ask*. An admin can
  now require operator approval before a chosen actor takes a chosen action kind
  (`issue.close` today). The gated write is refused with `202` and a recorded ask
  naming who wanted to do what to which target, carrying the requesting run's id;
  the operator approves, and the **agent retries** the same write.
  Deliberately **gate + retry, not deferred execution**: Athena never stores a
  mutation and replays it later on the agent's behalf, which would re-execute a
  stale payload under authorization evaluated at a different moment. Because the
  retry is an ordinary write it re-validates everything — visibility, scope, the
  blocked-close policy, the budget, `If-Match` — so an approval authorizes an
  intent, never a stored side effect. Approvals are **single-use** (consumed inside
  the retry's own transaction) and bound to both requester and target. A rejection
  answers with `409` and the `approval_rejected` code so an agent stops rather than
  waits. Gating is **opt-in**: applying migration 0063 changes nothing until an
  operator gates someone, and automation rule firings are never gated — the
  operator's own automation must not wait on the operator. Surfaced through
  `GET /approvals`, `POST /approvals/{id}/decision`, the
  `/approvals/policies/{user_id}` policy routes, the `approval_required` field on
  `/users/me`, the MCP tools `list_approvals` / `decide_approval` /
  `set_approval_policy`, and a "Waiting on you" queue in the agent cockpit. Every
  step is audited (`approval_requested`, `approval_approved`, `approval_rejected`,
  `approval_consumed`, `approval_policy_set`, `approval_policy_cleared`). See
  [docs/APPROVALS.md](docs/APPROVALS.md) for the guarantees and the limitations —
  one action kind, no expiry, no bulk decide, no un-reject.
- Durable per-agent **action budgets** — the bounded half of "attributable,
  reversible, and bounded". An admin caps how many metered writes an agent may
  make per fixed window (`hour` or `day`); the charge is folded into the metered
  command's own transaction, so a refused or failed write spends nothing and two
  concurrent writes cannot both spend the last unit. Unlike the in-process token
  rate limiter, the counter survives restart. Metering is **opt-in**: a user with
  no budget is unlimited, so applying migration 0062 changes nothing until an
  operator sets a ceiling. Exhaustion returns `429` with the stable
  `agent_budget_exhausted` code, a `Retry-After`, and the budget itself, and lands
  on the activity trail. Surfaced through `GET`/`PUT`/`DELETE /users/{id}/budget`,
  the `budget` field on `/users/me` (so an agent learns its ceiling by asking
  rather than by being refused), the MCP tools `get_agent_budget` /
  `set_agent_budget` / `clear_agent_budget`, and the agent cockpit. Budgets meter
  **actions, not tokens or dollars**: Athena never observes an agent's model
  spend, so it deliberately carries no cost column it could not honestly populate.
- `PUT /users/{id}/password`, the REST parity surface for the admin password
  reset that previously existed only in the browser. Authorization (admin role
  plus the admin scope for a bearer token) is enforced inside the command, so the
  two transports cannot drift.
- MCP `label_page` / `unlabel_page` tools, so agents can manage the shared label
  vocabulary on Mentor pages through the same REST behavior the browser uses —
  closing an API/MCP parity gap for an existing durable write.
- A visibility-safe Fleet Throughput page, strict REST endpoint, and MCP tool
  backed by one bounded snapshot service. New append-only lifecycle facts preserve
  event-time terminal categories, project scope, predecessor chains, and actor type;
  monotonic issue identities prevent deleted-target rebinding, legacy/imported
  ambiguity is reported as coverage, valid typed history survives manual target
  deletion for full-visibility admins, partial-visibility cycle timing is withheld,
  hidden work affects no aggregate,
  and query-plan tests pin the targeted time-window index.
- An admin-only active claimed-work projection across Mission Control, REST, and
  MCP, joining durable leases to exact claim runs, cooperative reports, current
  holder access, blockers, and replay evidence without inferring process health.
- Generation-fenced issue leases across REST, MCP, and Active Work. Every fresh
  acquisition receives a new opaque generation, renew/release mutations require
  the exact current generation, and delayed commands from an earlier possession
  fail without exposing the replacement token.
- Typed claim handoffs across yield, claim, work context, delegation inboxes,
  Active Work, and browser views. A holder yields bounded attempted work,
  evidence, a blocking question, and resume instructions atomically with its
  lease release; the next holder must explicitly acknowledge the untrusted
  advisory context before completing the claim.
- Project- and sprint-scoped fleet boards with explicit agent, human, and
  unassigned swimlanes; filter state survives HTMX, drag, and no-JS moves while
  private project and sprint names remain visibility-gated.
- An additive `assignee_is_agent` issue projection plus optional sprint
  filtering on the MCP `list_issues` tool, so agent clients can query the same
  sprint and actor dimensions as the web board.
- A public roadmap (`docs/ROADMAP.md`) grounded in a full-codebase review:
  phased plans for the agent loop, run integrity, docs-as-agent-memory, and
  fleet operations.
- Deterministic attachment reconciliation for local/tailnet operations, reporting
  missing, checksum-tampered, size-mismatched, unreadable, non-regular, and orphan
  storage without following symlinks or hashing FIFOs/devices/sockets. Doctor can
  run the reconciliation against a selected database and attachment directory and
  reports bounded category counts rather than blob names or content.

### Changed

- Claim acquisition and same-holder renewal now require exactly one strong root
  issue ETag across REST and MCP, with explicit missing, malformed, oversized, and
  stale-precondition responses and durable exact-retry replay.
- The test gate runs in parallel (`pytest -n 4` via pytest-xdist), cutting the
  suite from ~16 minutes to ~3 — the suite was already deterministic
  (no sleeps, per-test databases), so no test changed.
- Corrected four stale docstrings/comments that no longer matched the code
  (issue status "canonical set", webhook payload/event parity, JWKS caching,
  comment cross-link rendering), and removed operator-environment references
  from the contributor docs. Research planning notes moved to `docs/research/`.
- Made Python 3.12 the only supported runtime and changed boolean, numeric,
  floating-point, log-level, and partial OIDC configuration errors to abort startup
  instead of silently accepting an unsafe value or incomplete identity setup.
- Hardened `/readyz` and `athena-doctor` to validate the exact packaged migration
  inventory and applied checksums. Doctor additionally runs SQLite integrity and,
  when `--attach-dir` is supplied, attachment reconciliation.
- Staged database restore candidates through SQLite `quick_check`, durable atomic
  replacement, and automatic recovery of an existing target after sidecar cleanup or
  swap failure. Recovery names are directory-synced before destructive work and all
  candidate stages are cleaned on failure. Operations guidance now treats SQLite plus
  its matched attachment-directory snapshot as the complete stopped-service recovery
  unit.
- Graceful application shutdown now cancels both in-process background runners,
  awaits both, and surfaces non-cancellation failures. The supported deployment
  remains one process/runner on a trusted local machine or tailnet.

### Fixed

- **Attention-bearing claims are no longer the rows most likely to be hidden.**
  The active-work window sorted active leases before expired ones, so on a busy
  fleet the expired — attention-bearing — claims were exactly the rows the limit
  dropped, while the returned-items summary could truthfully report `0 need
  attention` about a page it had filtered clean of them. An exception surface that
  hides exceptions is worse than none, because it reads as reassurance. The window
  is still bounded but now fills from the urgent end, the exact per-row attention
  state sorts the returned page, and a new `attention_state` filter (web, REST, and
  MCP) returns precisely the rows needing a human. `examined_count` was added
  because "0 need attention" is otherwise ambiguous between "none do" and "none of
  the ones we looked at do" — different statements on a clipped fleet. The SQL
  ordering is explicitly a bias, not a second definition of attention: it does not
  reproduce the check-in, blocker, or token reasons, since two implementations of
  one predicate eventually disagree.

- The sprint lifecycle is now audited. Creating, editing, starting, completing,
  and deleting a sprint were all bare data-layer writes with no activity event on
  any transport, so the trail could not answer who started an iteration or who
  deleted the one that held last week's work — and the surface was absent from the
  command-migration inventory entirely. `aegis/sprint_commands.py` now owns each
  write: the row change and its event commit or roll back together, from both REST
  and the browser. The one-active-sprint serialization is preserved, and a no-op
  edit still records nothing.
- Project visibility and membership writes are now audited-atomic commands
  (`project_commands.set_project_visibility` / `add_project_member` /
  `remove_project_member`). The visibility flip previously ran as three
  independent commits — the flip, the creator's roster row when going private,
  then the activity event — so a crash mid-sequence left a permanently unaudited
  access-control change: because the mutation is idempotent and the event is
  recorded only on a real change, a retry never backfills it. REST and the browser
  now call the same commands, which no longer emit activity from either transport.
- Run-lineage coordinates must now name something real. `parent_run_id` and
  `forked_from_event_id` arrived as client headers and were stored verbatim, so
  any writer could fabricate ancestry out of thin air — naming a run nobody ever
  wrote, or a fork point that is not an event of that run — and `run_lineage()`
  plus the replay artifact build their trees from exactly those columns. A parent
  is now kept only when that run has actually been written, and a fork point only
  when it is a real event belonging to the surviving parent; a failing coordinate
  is stored as `NULL` rather than rejected, since the headers remain correlation
  hints, not authorization. Cross-actor lineage stays legitimate on purpose (the
  fork contract and automation both depend on it), and a rule firing on an
  untagged trigger keeps its real fork point while having no parent run.
- Password writes are now audited-atomic commands
  (`user_commands.change_own_password` / `reset_user_password`). Both paths ran
  the hash write and the session revocation as separate commits with no audit
  event on any transport: a crash between them left a rotated password with live
  sessions, and an admin could reset any account — taking it over, since later
  writes are attributed to the target — with nothing on the append-only trail.
  The hash write, the revocation that rotation forces, and the event now commit or
  roll back together, and the password and its hash never reach the trail.
- The automation engine's per-firing idempotency guard can no longer be forged
  from a client run-id header. A rule's firing run id is predictable
  (`automation:rule-N:event-M`) and the guard treats any activity row carrying it
  as proof the firing already happened; client `X-Athena-Run` values were stored
  verbatim, so a write-scoped client could pre-stamp that id and silently suppress
  the rule. The request edge now drops a client run id in the reserved
  `automation:` namespace (the engine still mints those ids in-process); forking a
  child run from an automation run is unaffected.
- Pausing an account now also freezes its stored idempotency receipts. The
  idempotency middleware previously authenticated by credential liveness alone
  and served stored replays without reaching the identity-layer pause gate, so a
  paused agent credential could still read completed mutation responses for up
  to the receipt TTL; `paused_at` was also missing from the authorization-
  revision fence triggers. A paused account's keyed request now skips
  claim/replay entirely and receives the same audited 403 refusal as its other
  actions, and any pause-state flip bumps the global authorization revision
  (migration 0061), permanently fencing pre-pause receipts even after resume.
- Page label attach/detach is now an audited-atomic command
  (`page_commands.attach_page_label` / `detach_page_label`): the join write and
  its `page_labeled`/`page_unlabeled` event commit or roll back together, where
  they previously committed separately and a crash between them could label a
  page with no trail entry. REST and the browser call the same command; MCP
  reaches it through REST. This was the last Mentor write listed as
  command-migration debt.
- Fleet-board moves now submit the card's canonical issue ETag and fail closed
  when the board is stale, preserving the newer status and showing an explicit
  refreshed-board conflict notice in both HTMX and no-JS flows.
- Saved-filter assignee ids now reject invalid types and values outside SQLite's
  integer range before persistence; malformed JSON, non-object JSON, unknown keys,
  and invalid stored criteria fail closed instead of widening the query or raising
  an overflow while running it.
- Searching within a saved filter now preserves the filter's own title/body text
  constraint instead of replacing it with the ad-hoc query.
- Attachment publication now uses a private same-directory stage, file and
  directory fsync, and atomic replacement before one metadata-plus-audit commit.
  Audit, notification, run-binding, write, and commit failures roll back and
  attempt blob cleanup; a cleanup failure is surfaced alongside the primary error
  for reconciliation. Deletion commits metadata plus audit before observable
  post-commit unlink. Downloads open regular blobs through descriptor-anchored,
  no-follow paths, and hard page delete
  attempts each blob/link/search cleanup independently.

## 0.1.0a1 development milestone (untagged) — 2026-07-18

### Added

- AGPL-3.0-only licensing and package metadata.
- Contributor and security policies.
- A bounded peer-review guide and an explicit command-migration inventory.
- Transparent documentation of Athena's AI-assisted development process.
- A safe, loopback-only seeded demo command for a five-minute product tour.
- A pull-request template that records scope, verification, risks, and AI help.
- Atomic, audited application commands for the credential/privilege/content
  writes that previously split mutation from audit (or recorded nothing):
  dependency links, user role/agent-flag changes, user creation, API-token
  mint/revoke, webhook lifecycle, SSO identity link/unlink, automation-rule
  lifecycle, and issue/page comment create/edit/delete — each write and its
  activity event now commit or roll back together.

### Changed

- Reframed the README around the operator loop, a fast demo, architecture
  evidence, and honest alpha boundaries.
- Moved agent credential authorization into the command boundary as defense in
  depth; REST and web adapters remain early gates.
- Advanced package metadata to the `0.1.0a1` review-candidate line.

## 0.0.1 development milestone (untagged)

Initial local-alpha development line: Aegis issues, Mentor documentation,
cross-links, scoped agent access, MCP, activity/run lineage, portability,
webhooks, automation, OIDC, and operational tooling.
