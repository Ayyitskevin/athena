# Changelog

Notable changes to Athena are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release versions
follow semantic versioning while the project remains pre-1.0.

## [Unreleased]

### Added

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
- A holder-only REST and MCP claim-yield command with structured reasons and an
  optional bounded note. It atomically releases the active lease and records a
  run-stamped `claim_yielded` event without changing or reassigning the issue.
- Project- and sprint-scoped fleet boards with explicit agent, human, and
  unassigned swimlanes; filter state survives HTMX, drag, and no-JS moves while
  private project and sprint names remain visibility-gated.
- An additive `assignee_is_agent` issue projection plus optional sprint
  filtering on the MCP `list_issues` tool, so agent clients can query the same
  sprint and actor dimensions as the web board.
- A public roadmap (`docs/ROADMAP.md`) grounded in a full-codebase review:
  phased plans for the agent loop, run integrity, docs-as-agent-memory, and
  fleet operations.

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

### Fixed

- Fleet-board moves now submit the card's canonical issue ETag and fail closed
  when the board is stale, preserving the newer status and showing an explicit
  refreshed-board conflict notice in both HTMX and no-JS flows.
- Saved-filter assignee ids now reject invalid types and values outside SQLite's
  integer range before persistence; malformed JSON, non-object JSON, unknown keys,
  and invalid stored criteria fail closed instead of widening the query or raising
  an overflow while running it.
- Searching within a saved filter now preserves the filter's own title/body text
  constraint instead of replacing it with the ad-hoc query.

## [0.1.0a1] - 2026-07-18

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

## [0.0.1]

Initial local-alpha development line: Aegis issues, Mentor documentation,
cross-links, scoped agent access, MCP, activity/run lineage, portability,
webhooks, automation, OIDC, and operational tooling.
