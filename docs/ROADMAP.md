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

The operator loop is **Assign → Work → Observe → Intervene**. Current state:

- **Assign** spans the delegation inbox, durable claim/lease ownership,
  possession-generation fencing, and typed blocker handoffs. The current holder
  can complete, yield, or decline work without a human performing every final
  transition.
- **Work** over MCP carries run and parent/fork identity on writes. Agents can
  inspect effective identity and scopes, delegation context, replay artifacts,
  active-work supervision, and visibility-safe fleet metrics through MCP.
- **Observe** combines the append-only activity trail with bound run identity,
  parent/fork lineage, run reconstruction, replay export, webhooks, Mission
  Control, active-work state, and throughput metrics.
- **Intervene** includes token revocation, one-command offboard, audited
  per-agent pause, holder/admin claim controls, and optional project blocked-close
  gates, durable per-agent action budgets that refuse a metered write once the
  window's ceiling is spent, and opt-in human-in-the-loop approval gates that
  refuse a gated action with a recorded ask the operator approves or rejects
  (`issue.close` and `dispatch.request` — two action kinds, each naming one
  intent, not a general approval workflow).
- **Intervene** also includes a cooperative worker registry: an agent process
  registers by heartbeat, and an admin can ask it to stop. Athena records the
  request and the worker's reply — it cannot signal or observe a process, so a
  silent worker is stale, never terminated. That is not process-level kill.
- **Intervene** now also reaches a single live run: an admin can record a bounded
  control request against it — steer with guidance, request cooperative
  cancellation, or request a structured fresh-context handoff — which only the
  run's bound agent can read and settle (acknowledge, decline, or complete;
  unanswered requests expire by the server clock). The same request-and-reply
  honesty as the worker kill, at run granularity
  (see [`RUN_CONTROLS.md`](RUN_CONTROLS.md)).
- **Intervene** finally has one place to look: an admin-only fleet-attention
  rollup on the dashboard counts claims needing attention, waiting approvals,
  unanswered kill requests, run controls awaiting an agent (with a fleet-wide
  `/admin/run-controls` page owning that number), failing automation rules and
  webhooks, budget ceilings
  hit, and boundary refusals — each linking to the surface that owns it. Refused
  logins, revoked tokens, scope denials, and paused refusals were always recorded;
  they now have a page and an API instead of requiring an operator to know the verb
  names.
- **Delegate** can now reach outside Athena: an issue may be dispatched to an
  external execution fleet under a policy digest of the authorization in force, and
  the executor reports evidence and an outcome through a signed, idempotent
  callback. Every state is what Athena was told, never what is happening on the far
  side, and dispatch is off unless an executor is configured.
- **Trust / Learn** also closes the memory loop: a human or an agent can promote
  what a run learned into the issue's runbook page, which the next agent reads
  through ordinary backlinks and its work-context packet. Promotion is always
  explicit, promoted text is quoted and attributed rather than merged, and a named
  run must be one that actually exists.
- **Trust / Learn** adds undo by compensation: reversing an action records its
  inverse as a new audited command linked to the event it reversed, so history is
  never rewritten. The reversible set now covers issue and page archive and
  labels, an issue's status (from the structured prior state the lifecycle facts
  already recorded), and its assignee (from the 0068 assignee facts) — each
  scalar refusing when a newer change has superseded it. Everything else is
  refused with its reversibility class. That is not general undo.
- **Trust / Learn** gained a trail that can prove itself: every activity row
  recorded after migration 0072 is hash-chained in the same transaction, chained
  rows are undeletable, and bounded resumable verification (REST, MCP,
  `athena-doctor`, a tail check on `/admin/security`) points at the first broken
  link. The chain makes tampering evident, not impossible — the boundary is
  documented in [`TRAIL_INTEGRITY.md`](TRAIL_INTEGRITY.md).
- **Trust / Learn** also gained the answerability ledger: per agent, every
  recorded ask (controls, kill requests, approvals) beside its recorded answer,
  plus reversals — derived at read time, zero-filled, deliberately never a
  score (see [`ANSWERABILITY.md`](ANSWERABILITY.md)).
- **Intervene** finally has no unaudited durable writes left in the Aegis project
  surface: configuring a project's statuses — which is configuring what "closed"
  means for its issues — is a command with an actor and an audit event, and every
  automation rule action reaches a command owner rather than composing its own
  write beside the trail.

- **The loop is exercised, not just implemented.** A field exercise
  (`scripts/field_exercise.py`, gated in CI) drives every step above against a
  real Athena process and a real reference executor
  (`examples/icarus_executor.py`) over real HTTP with real signatures — the
  composed system, not its parts. Its first run found two defects the stubbed
  tests could not: the SSRF guard refused every local executor (now an explicit
  opt-in allowlist), and the delivery poster discarded the acceptance body that
  carries the executor's run id (so no real callback could ever correlate).

Phase 1 closed the original attribution and delegated-completion breaks. Items
that remain intentionally open are still unchecked below.

## Phase 1 — Close the loop (agents become first-class, attributed workers)

Goal: an agent connecting over MCP can be onboarded with a scoped token, pick
up delegated work, execute it under a run id, and complete it — and the
operator can replay the run.

- [x] Thread run identity through the MCP transport: a per-session run id
      (environment) plus optional per-call override, mapped to the
      `X-Athena-Run` / `X-Athena-Parent-Run` / `X-Athena-Fork-From-Event`
      headers the API already honors, so MCP writes participate in the same run
      lineage and replay path as direct REST writes.
- [x] Fix the delegation dead-end: allow a delegated contributor to transition
      the issue delegated to it (with an admin override), so the flagship
      Assign → Work flow completes without a human performing every status
      change.
- [x] Require explicit scopes when minting tokens, eliminating the former
      fail-open **admin** default in an agent-credential product. Legacy stored
      tokens keep their meaning.
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
      to stamp into another identity's run. Replay artifacts are therefore
      evidence tied to an identity, not an honor-system convention.
- [x] Per-agent **pause** (checked at identity resolution, audited, toggleable
      in the cockpit) — the lever an operator reaches for before the kill
      switch.
- [x] Audit authentication/authorization *failures*: failed logins,
      revoked-token use, and scope denials are recorded as activity events,
      including rejected boundary probes.
- [x] Audit the project lifecycle (create/edit/delete), recording who created,
      changed, or removed each workspace container.
- [x] Give automation lineage: stamp rule-driven writes with the triggering
      event and rule, and make the comment action idempotent across a crashed
      pass.
- [x] Migrate the remaining risky Aegis writes to commands per
      [COMMAND_MIGRATION.md](COMMAND_MIGRATION.md): parent hierarchy,
      archive/restore, labels, contributors.
- [x] Include run/lineage coordinates in webhook payloads so push consumers can
      mirror Mission Control.

## Phase 3 — Docs become safe shared agent memory

Goal: the knowledge layer supports concurrent agent writers as safely and
traceably as the issue layer already does, and agents can traverse the
knowledge graph the database already stores.

- [x] Page optimistic concurrency: ETag / `If-Match` parity with issues on page
      edits (REST + MCP), so two agents editing shared memory get a clean 412
      instead of silent last-write-wins.
- [x] Page soft-delete (`archived_at`, matching issues), preserving page
      versions and comments for restore instead of destroying them.
- [x] Complete the Mentor command migration: atomic mutation + audit for page
      create/edit/move/delete/restore and space lifecycle.
- [x] Knowledge-graph MCP tools: backlinks, outgoing links, page-version
      history/restore, and space-scoped search, so agents can traverse the graph.
- [x] Title-based addressing: `[[Page Title]]` resolution and a
      fetch-by-title API/tool. Numeric ids are exactly the lookup agents are
      worst at.
- [x] Index comments into full-text search; record run provenance on page
      versions.

## Phase 4 — Fleet operations

Goal: multiple agents coordinate without double-work, fleet throughput is
measurable, and the architecture's rules are machine-enforced against the very
agents that contribute here.

- [x] Delegation claim/lease protocol (accept / decline / complete) so two
      agents cannot silently pull the same issue.
- [x] Possession generations fence delayed renew, yield, complete, decline, and
      handoff-resume commands from a later lease epoch.
- [x] Typed blocker handoffs preserve bounded continuation context across yield
      and reassignment, with explicit holder acknowledgment before completion.
- [x] Admin active-work supervision joining each lease to its exact claim run,
      cooperative check-in, current holder controls and eligibility, visible
      blockers, and replay evidence across web, REST, and MCP.
- [x] Project- and sprint-scoped boards with agent-vs-human swimlanes.
- [x] Visibility-safe fleet throughput metrics derived from typed append-only
      lifecycle facts: created-vs-completed flow, trustworthy completion-cycle
      median for full-visibility admins, and event-performer human/agent attribution.
      Legacy and imported ambiguity remains explicit rather than a guessed backfill.
- [x] Bounded UTC time/schedule-based automation triggers (stale-issue nudges, sprint-end
      sweeps) alongside event triggers.
- [x] Mechanical guardrails: Ruff lint and formatting checks, static import
      contracts, mypy across every runtime module in `src/athena`, and a
      full-source coverage gate with configured line and branch floors.
- [x] Optional per-project hard workflow gates for agent actors (block closing
      blocked issues at the command layer, not just as web advisory).

Checked boxes record repository implementation, not release status. They do not
assert a successful hosted-CI run for the current production-readiness changes
or that Athena is production-ready; deployment and release sign-off are separate.

## Phase 5 — Expansion: do the other tools' jobs

Goal: a solo operator running an agent fleet needs no other tab open. Not feature
parity with five products — the *jobs* those products do, done inside Athena's
own architecture. The full campaign, with boundaries and parked decisions, is
[OPUS_EXPANSION_GUIDE_ATHENA.md](OPUS_EXPANSION_GUIDE_ATHENA.md).

The previous scoping deferred this "until the fleet loop above is complete and
boring". Phases 1–4 and the hardening/field-proving/release stages that followed
satisfied that condition, so the expansion begins on the roadmap's own terms —
under exactly the same architecture rules, none of which are amended.

- [x] **One query language over all work** ([QUERY.md](QUERY.md)): a bounded,
      GitHub-shaped grammar — `is:open label:infra assignee:@me sort:priority-desc`
      — over REST, the browser, MCP, and saved filters. Unknown atoms are errors
      naming the atom, never empty results; visibility is composed into the SQL.
- [x] **Live embeds** ([EMBEDS.md](EMBEDS.md)): fenced ```athena directives in
      Mentor pages that render real issue lists, counts, and single issues at view
      time, with the *viewer's* visibility rather than the author's. Nothing is
      snapshotted into page content; a directive that cannot render says so in
      place. The stage where docs and tracker become one product.
- [x] **Knowledge graph** ([GRAPH.md](GRAPH.md)): unlinked mentions that propose
      edges without ever creating one (the "link it" click is an ordinary audited
      edit of the *source*), a bounded server-rendered ego graph over the links
      already stored — visibility applied during traversal, and a "showing N of M"
      line whenever the ceiling bites — plus label-based page templates (no new
      table) and an idempotent operator daily note that writes nothing on revisit.
- [x] **Forge integration** ([FORGE.md](FORGE.md)): inbound signed events landing
      as imported activity on the issues they name — verified before the payload is
      parsed, matched against real project keys so `UTF-8` cannot land on anyone's
      work, counted rather than stored when they match nothing, and excluded from
      undo, the lifecycle facts, and the native metrics because they are foreign
      history (the attention-rollup and security-counter exclusion was added
      after review — see CHANGELOG).
      Athena never calls the forge: inbound only, no stored third-party token.
- [x] **Planning**: a per-project timeline draws sprints as lanes with issues
      placed in them and declared dependencies between them, and a parent issue
      shows live child status counts — computed on every read, never stored, and
      available as a `kind: rollup` embed so a page can carry the same number.
      Lane ORDER is chronological; lane WIDTH is deliberately not a duration,
      because sprint dates are optional and unvalidated. Read-only: placement
      still changes through the sprint form that owns that write. Per-issue
      target dates were considered and deliberately not built (see
      [PLANNING.md](PLANNING.md)).
- [x] **Editing and leaving**: page and issue editors show a live preview
      rendered by the SAME function the view uses, so it cannot drift; page
      editors autosave crash-safe drafts that are user-private state rather than
      content (own table, no activity event, owner-only, never exported, and
      only an explicit save turns one into a page); attachment images render
      inline from a byte-sniffed type with SVG deliberately excluded; and a
      space exports to one self-contained HTML file where embeds are visibly
      dead and carry their own directive text
      (see [EDITING.md](EDITING.md)).

## Out of scope (for now)

Multi-tenant hosting, real-time collaboration (CRDTs), a JS build chain,
git hosting / code review / CI (Athena integrates with a forge rather than
becoming one), and Notion-style free-form block databases. Custom fields are
parked as an explicit product decision — labels plus the query grammar plus
embeds are the Athena-shaped alternative. Athena stays a self-hosted,
server-rendered, single-operator tool.
