# Roadmap

Athena's goal is to be the **command center for a fleet of AI agents**: the
good parts of a tracker (Jira), a wiki (Confluence), a linked notebook
(Obsidian), and a workspace (Notion) — self-hosted, in one tool, where humans
operate and agents work, and everything an agent does is **attributed,
observable, controllable, and auditable**.

This roadmap came out of a full-codebase review (eight independent passes:
architecture, agent surface, audit/observability, knowledge layer, work layer,
security, tests, review-readiness). Like
[COMMAND_MIGRATION.md](COMMAND_MIGRATION.md), it is an honest ledger: each item
names the gap it closes, and the phases are ordered by leverage toward the
vision — agent capability first, run integrity second, knowledge depth third,
fleet coordination last.

## Where the loop stands

The operator loop is **Assign → Work → Observe → Intervene**. Today:

- **Observe** is the strongest leg: an append-only activity trail with run ids,
  parent runs, fork lineage, run reconstruction, replay export, webhooks, and a
  Mission Control view.
- **Intervene** has the nuclear levers (token kill switch, one-command
  offboard) but nothing softer.
- **Assign** works (delegation inbox with machine-readable warnings) but the
  loop breaks at the last step: permissions don't yet let a delegated agent
  *finish* the work it was handed.
- **Work** over MCP functions, but writes arrive untagged: the MCP transport
  does not yet send run headers, so the lineage machinery cannot see the
  primary agent path.

Phase 1 exists to close exactly those breaks.

## Phase 1 — Close the loop (agents become first-class, attributed workers)

Goal: an agent connecting over MCP can be onboarded with a scoped token, pick
up delegated work, execute it under a run id, and complete it — and the
operator can replay the run.

- [x] Thread run identity through the MCP transport: a per-session run id
      (environment) plus optional per-call override, mapped to the
      `X-Athena-Run` / `X-Athena-Parent-Run` / `X-Athena-Fork-From-Event`
      headers the API already honors. *(The single highest-leverage change in
      the repo: today MCP writes are invisible to run lineage and replay.)*
- [x] Fix the delegation dead-end: allow a delegated contributor to transition
      the issue delegated to it (with an admin override), so the flagship
      Assign → Work flow completes without a human performing every status
      change.
- [x] Require explicit scopes when minting tokens (today an omitted `scopes`
      silently mints **admin** — a fail-open default in an agent-credential
      product). Legacy stored tokens keep their meaning.
- [x] Audited agent onboarding: one atomic admin command that mints a scoped
      token for an agent user, surfaced in the admin cockpit — provisioning
      should be as one-click and audited as revocation already is.
- [x] `whoami` MCP tool (identity + effective scopes) so an agent discovers its
      boundaries by asking, not by failing.
- [x] Scope-enforcement consistency: a least-privilege write token must be able
      to read its own delegation inbox.
- [x] Highest-value MCP read gaps: issue comment threads, the notifications
      inbox, and run-replay artifacts — the primitives for agent-to-agent
      handoff via mentions and comments.

## Phase 2 — Intervene, and make the trail trustworthy

Goal: runs become tamper-resistant attribution units, misbehavior (not just
success) is observable, and the operator gets levers between "watch" and
"revoke everything".

- [x] Bind a run id to the first identity that uses it; reject writes that try
      to stamp into another identity's run. *(Run ids are honor-system today —
      replay artifacts should be evidence, not convention.)*
- [x] Per-agent **pause** (checked at identity resolution, audited, toggleable
      in the cockpit) — the lever an operator reaches for before the kill
      switch.
- [x] Audit authentication/authorization *failures*: failed logins,
      revoked-token use, and scope denials become activity events. An agent
      probing its boundary currently leaves no trace.
- [x] Audit the project lifecycle (create/edit/delete) — today a workspace
      container can appear or vanish with no record of who did it.
- [x] Give automation lineage: stamp rule-driven writes with the triggering
      event and rule, and make the comment action idempotent across a crashed
      pass.
- [ ] Migrate the remaining risky Aegis writes to commands per
      [COMMAND_MIGRATION.md](COMMAND_MIGRATION.md): parent hierarchy,
      archive/restore, labels, contributors.
- [x] Include run/lineage coordinates in webhook payloads so push consumers can
      mirror Mission Control.

## Phase 3 — Docs become safe shared agent memory

Goal: the knowledge layer supports concurrent agent writers as safely and
traceably as the issue layer already does, and agents can traverse the
knowledge graph the database already stores.

- [ ] Page optimistic concurrency: ETag / `If-Match` parity with issues on page
      edits (REST + MCP), so two agents editing shared memory get a clean 412
      instead of silent last-write-wins.
- [ ] Page soft-delete (`archived_at`, matching issues) — today one call
      permanently destroys a page, its versions, and its comments.
- [ ] Complete the Mentor command migration: atomic mutation + audit for page
      create/edit/move/delete/restore and space lifecycle.
- [ ] Knowledge-graph MCP tools: backlinks, outgoing links, page versions and
      restore, space-scoped search — the graph exists; agents can't walk it
      yet.
- [ ] Title-based addressing: `[[Page Title]]` resolution and a
      fetch-by-title API/tool. Numeric ids are exactly the lookup agents are
      worst at.
- [ ] Index comments into full-text search; record run provenance on page
      versions.

## Phase 4 — Fleet operations

Goal: multiple agents coordinate without double-work, fleet throughput is
measurable, and the architecture's rules are machine-enforced against the very
agents that contribute here.

- [ ] Delegation claim/lease protocol (accept / decline / complete) so two
      agents cannot silently pull the same issue.
- [ ] Project- and sprint-scoped boards with agent-vs-human swimlanes.
- [ ] Fleet throughput metrics derived from the existing trail: cycle time,
      created-vs-resolved, per-agent completion.
- [ ] Time/schedule-based automation triggers (stale-issue nudges, sprint-end
      sweeps) alongside event triggers.
- [ ] Mechanical guardrails: import-linter contracts for module boundaries,
      mypy on `src/`, and a coverage gate in CI.
- [ ] Optional per-project hard workflow gates for agent actors (block closing
      blocked issues at the command layer, not just as web advisory).

## Out of scope (for now)

Multi-tenant hosting, real-time collaboration (CRDTs), a JS build, and
Notion-style databases/blocks. Athena stays a self-hosted, server-rendered,
single-operator tool until the fleet loop above is complete and boring.
