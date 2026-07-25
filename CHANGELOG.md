# Changelog

Notable changes to Athena are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and package version
markers follow semantic versioning while the project remains pre-1.0. As of
2026-07-23, Athena has no tags or GitHub releases: version-like headings below are
untagged package/development milestones, not published releases.

## [Unreleased]

### Added

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
