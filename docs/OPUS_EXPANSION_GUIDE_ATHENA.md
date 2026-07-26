# The second campaign: making Athena rival the tools it borrowed from

**Audience:** the Opus session (or any capable agent session) executing the next
stages of Athena. This is the successor to
[`OPUS_VISION_GUIDE_ATHENA.md`](OPUS_VISION_GUIDE_ATHENA.md), which drove Stages
A–H; Stages I–L continued its method. Read that guide's history before starting:
the discipline it encodes is why twelve consecutive stages shipped without
breaking the architecture.

**Owner's direction (2026-07-26):** build Athena out to rival GitHub, Notion,
Obsidian, Jira, and Confluence.

## 0. What "rival" means here — read this before any stage

Athena will not beat five venture-funded products at their own feature
matrices, and must not try: a feature-parity chase is how the differentiator
drowns. The owner's direction is read the only way it can be won:

> **For a solo operator running an AI agent fleet, Athena should do the JOBS
> those five tools do — so that operator needs none of them open.**

Notion's job is *pages that hold both prose and live structure*. Obsidian's is
*a knowledge graph you can traverse and grow*. Jira's is *plan, order, and see
work over time*. Confluence's is *durable team documentation with structure*.
GitHub's, for this user, is *the place work-evidence lives* — Athena should
**integrate** with a forge, never become one. Each stage below closes one of
those jobs. Feature ideas that don't serve the operator-plus-fleet user fail
the test no matter which rival has them.

**The differentiator is non-negotiable.** Everything that made stages A–L
valuable — agents as first-class attributed actors, one command owner per
durable write, an append-only trail, fail-closed authorization, evidence over
claims — applies to every new surface. A rival-feature that requires weakening
those is a refusal with a written reason, not a build.

## 1. The unlock, and the constitution that survives it

`ROADMAP.md` scoped this expansion out "until the fleet loop above is complete
and boring." **That condition is met.** Stages A–L completed the loop
(budgets, approvals, undo, workers, attention, learnings, dispatch), hardened
it (I–J), proved it composed over real HTTP against a real counterparty (K),
and cut a release candidate with full observed evidence (L). The moratorium's
own terms unlock the expansion; Stage M-0 below amends the roadmap so the
documents stay truthful.

What is **amended** (owner's direction):
- Richer knowledge surfaces (embeds, graph, templates) are now in scope.
- Planning surfaces (timeline, rollups) are now in scope.
- Inbound integration (forge events) is now in scope.

What is **not amended** — the architecture constitution, unchanged:

1. Never push directly to main. Branch `claude/<topic>`, draft PR, full gate.
2. No invented UI data, in-memory records, fake activity, or simulated results.
   An embed that cannot render real data renders an explicit refusal box.
3. The web layer stays a thin client over real domain/API data.
4. One command owner per durable write: authorization, validation, mutation,
   projections, audit — one `db.transaction(conn, immediate=True)`.
5. MCP reaches the same behavior through REST. No parallel mutation paths.
   **Every stage ships MCP parity for its new reads in the same PR.**
6. Preserve actor attribution, idempotency, ETags, visibility rules, lease
   generations, run identity, replay integrity, fail-closed authorization.
7. Import contract: `web → (aegis|mentor) → core`; aegis and mentor are peers;
   `main.py` composes. `scripts/check_import_contracts.py` enforces it.
8. Migrations stay forward-only, contiguous (next is **0069**), checksum-bound.
9. Server-rendered Jinja/HTMX. **No JS build chain.** Small inline or vendored
   scripts in `static/` are acceptable where HTMX is not enough (they already
   exist); a bundler, npm, or a framework is not.
10. SQLite single-writer discipline: no network calls inside transactions.
11. Still OUT, regardless of rivalry: multi-tenant hosting, real-time
    collaborative editing (CRDTs), git hosting/code review/CI (integrate,
    don't host), BPMN/workflow engines, and Notion-style free-form block
    databases. Custom fields are parked as a product decision (§9).
12. If a feature requires a product decision or a new trust boundary, document
    the blocker and park it (§9) instead of silently inventing behavior.

## 2. What is already won — do not rebuild it

Verify against the tree before building anything; this list is the map, the
code is the territory. Aegis: projects, issues, per-project configurable
statuses, priorities, labels, sprints, boards with swimlanes, typed
dependencies with visible blockers, parent hierarchy, saved filters (JSON
criteria, owner-scoped), comments, watches, mentions, notifications, bulk
update, archive, FTS, attachments, event+schedule automation, blocked-close
gates, ETags/If-Match, durable idempotency. Mentor: spaces, nested pages,
versions+restore, page comments, labels, wikilinks/backlinks/title addressing
(`[[Page]]`, `[[ATH-12]]`, `[[issue:N]]`), page archive, version provenance,
**page watches already exist** (`notifications.WATCHABLE_KINDS`). Fleet: the
whole A–L loop, `examples/icarus_executor.py`, `scripts/field_exercise.py` as
a CI gate. Platform: 68 migrations, 2,300 tests, enforced coverage floors
(92.60/82.30/90.30), release evidence in `RELEASE_READINESS.md`.

Extension points you will use, verified present:
- `web/render.py:render_body` — the one markdown pipeline; wikilink/issue-key
  substitution lives here; fenced-directive embeds slot in beside it.
- `links` table — typed source/target for issues and pages; the graph is
  already stored, it has never been drawn.
- `activity.imported_at` (0041) — foreign history lands as imported events
  that can never be mistaken for native writes; inbound forge events use this.
- `access.event_visibility_clause` / `visible_project_ids` /
  `can_see_space` — the only visibility predicates; every new read uses them.
- `webhooks.sign` / HMAC-verify-before-parse (dispatch callback) — the
  pattern for every inbound signed payload.

## 3. Stage M — one query language over all work

*The job (Jira/GitHub): find and order anything, precisely, without clicking
through filters — for humans in a search box and agents over MCP.*

**M-0 (do first, small):** amend `ROADMAP.md`'s "Out of scope" and add an
"Expansion" phase listing these stages; update `VISION.md`'s differentiator
paragraph to name the expansion. The docs must never lag the direction.

**M-1: the grammar.** A bounded, documented, GitHub-style query grammar — NOT
JQL: `is:open label:infra project:ATH assignee:@me priority:high
sort:updated-desc has:blockers text terms`. Closed vocabulary of atoms;
an unknown atom is a **422 naming the atom**, never a silent empty result
(steering rule 2 — an empty page that should have matched is invented data of
another kind). Parser in `core/` (pure, no I/O), compiler to SQL fragments in
the domain layers, visibility clauses composed in SQL, never post-filtered in
Python. Quoted phrases, negation (`-label:x`), and `@me` resolution are in;
OR-groups and parentheses are out of v1 (document that).

**M-2: surfaces.** `GET /issues?q=` beside the existing structured params
(both compose; conflicting specifications are a 422); the issues list page
gets the query box (HTMX); saved filters gain an optional `query` key in their
JSON criteria (bump the criteria validation, no migration — verify the shape
against `0026`); a unified `/search?q=` page with issues/pages/comments tabs
reusing existing FTS. MCP: `search_work(q)` via REST.

**M-3: tests.** Grammar property tests (every documented atom, every
documented error), visibility tests (a query never returns an issue the
caller cannot see — test with a private project and a non-member), parity
tests (same q → same ids on REST, web, MCP), and a fuzz pass over garbage
input (nothing 500s).

Rough size: the parser is the risk; keep it boring (regex-free tokenizer,
explicit atom table). No migration expected.

## 4. Stage N — live embeds: pages that are dashboards

*The job (Notion/Confluence): one page that mixes prose with live, structured
views of real work. This is the flagship stage — it is where Athena stops
being "a wiki next to a tracker" and becomes one product.*

**N-1: the directive.** A fenced block in any Mentor page (and issue bodies,
if cheap):

    ```athena
    kind: issues
    q: is:open project:ATH sort:priority-desc
    limit: 10
    ```

Rendered at **view time, per viewer**, by the same code that serves the real
surfaces — the Stage M query engine for `issues`, the board renderer for
`board`, `fleet_metrics` for `metrics`, a single issue card for `issue: N`.
Bounded `limit` with an explicit "N more not shown" line (no silent
truncation). The page stores only the directive text; **no data is ever
snapshotted into page content** — a stored snapshot is a staleness lie and a
visibility leak in one.

**N-2: the trust rules (these are the stage).**
- Visibility is the *viewer's*, not the author's: an embed authored by an
  admin renders for a member only what that member may see, and says
  "restricted" for the remainder rather than pretending completeness.
- A directive that cannot render (bad q, unknown kind, over budget) renders a
  visible error box with the reason. Never an empty space, never a 500.
- Render cost is bounded: cap directives per page, rows per directive, and
  total embed query time; over-budget renders the refusal box. A page must
  not become a denial-of-service amplifier.
- Directives compose with the existing sanitizer — the output HTML goes
  through the same escaping path as everything else in `render_body`.

**N-3: surfaces.** Works in page view, page preview, and the runbook pages
Stage G writes (a runbook that shows its issue's live state is the payoff).
MCP read parity: `render_page` output includes embed results as structured
data, not HTML (agents read data, not markup).

**N-4: tests.** Viewer-visibility (author-admin/viewer-member), every failure
box, the budget caps, sanitizer composition (a directive inside a quoted
block renders as text — Stage G's forged-attribution lesson applies), and a
field-exercise step: the exercise's runbook gains an embed and asserts the
live issue state appears.

## 5. Stage O — the knowledge graph earns its name

*The job (Obsidian): traverse, grow, and trust the link graph.*

- **O-1 Unlinked mentions.** FTS already indexes everything: for a page,
  list pages/issues whose text contains this page's title (or issue key)
  without a link, with a one-click "link it" that edits through the ordinary
  page command (attributed, versioned). The suggestion list is a read; the
  click is an ordinary audited write. Never auto-link.
- **O-2 Graph view.** Server-computed layout (Python, deterministic seed) of
  the `links` table, emitted as SVG; bounded node count with an explicit
  "showing N of M" scope line; visibility-filtered with the same predicates as
  every read; clicking a node navigates. Ego-graph per page/issue first
  (depth ≤ 2), global graph only if the bounded version proves cheap.
- **O-3 Page templates.** Space-level template pages (a flag on a page, no
  new table if a label/naming convention suffices — decide against the
  schema, document the choice); "new page from template" pre-fills content
  through the ordinary create command. Templates are content, not code: no
  variable substitution beyond title/date in v1.
- **O-4 The operator's daily note.** One route/command: today's page in a
  configured space, created from the daily template on first visit. Prefill
  only from real data (yesterday's note link, open attention items as an
  embed directive — which Stage N makes honest). This is Obsidian's daily
  note fused with Athena's attention rollup: the operator's morning page.

Tests: unlinked-mention precision (a linked mention is not suggested; a
quoted/code-fenced occurrence is — decide and pin the rule), graph visibility
(a private page never appears in a non-member's SVG), template create
attribution, daily-note idempotency (two visits, one page).

## 6. Stage P — the forge: evidence flows in

*The job (GitHub): the operator's commits, branches, and PRs are visible from
the work item that caused them. Integrate; never host.*

- **P-1 Inbound sources.** A registered "event source" (name, secret,
  enabled) — admin-managed, audited lifecycle like webhooks. One inbound
  endpoint per source kind, starting with GitHub's webhook shape:
  HMAC-verified against the exact body **before parsing** (the dispatch
  callback pattern), bounded payload, closed event vocabulary (push, PR
  opened/merged/closed, branch created — nothing else in v1).
- **P-2 Landing rule.** An inbound event that references an issue key
  (`ATH-12` in a commit message, branch name, or PR title) lands as an
  **imported** activity event on that issue (`imported_at` set, source
  named): visible on the trail, excluded from native-only machinery exactly
  as 0041 already guarantees (undo refuses imported events; lifecycle facts
  refuse imported envelopes — this is why the landing rule is cheap and
  safe). No key match → counted, not stored (a bounded "unmatched events"
  health number per source).
- **P-3 Rendering.** Commit/PR/branch references in the trail and in
  `completion_ref`/`evidence_ref` render as links when they are URLs on a
  registered source's host. Dispatch's evidence chain finally has a face.
- **P-4 Boundaries, stated in docs:** Athena never fetches from the forge
  (no outbound polling, no API tokens for GitHub in v1 — inbound only, so
  there is no new egress surface and no stored third-party credential); a
  forge event is a *claim by the source*, worded as such.

Tests: signature-refusal-before-parse, key-extraction corpus (branch names,
squash-merge messages, multi-key commits), imported-event exclusion (undo of
a forge event refuses; metrics ignore it), unmatched counting, and a
field-exercise extension: the exercise POSTs a signed synthetic push event
referencing its issue and asserts the trail shows it as imported.

## 7. Stage Q — planning: see work over time

*The job (Jira, the part worth keeping): order and horizon, without a
workflow engine.*

- **Q-1 Timeline.** A server-rendered timeline/roadmap view per project:
  sprints as lanes, issues placed by sprint with dependency edges drawn
  between them (SVG, same discipline as the graph view). No drag-and-drop in
  v1 — placement changes go through the existing sprint-assignment forms.
- **Q-2 Rollups.** A parent issue's view shows live child status counts and a
  progress line computed at read time from real children (categories from the
  per-project status config — Stage I made that trustworthy). The same rollup
  as an embed kind (`kind: rollup`, issue: N) for free via Stage N.
- **Q-3 Target dates, only if the timeline proves insufficient without them.**
  If built: one nullable `target_date` on issues via migration 0069, set
  through the issue command with an audited event, rendered on the timeline
  — and NOT a deadline engine: no reminders, no escalation, no SLA math in
  v1. If the timeline reads fine without it, skip and record why.

Tests: timeline visibility, dependency-edge correctness against the typed
dependencies, rollup live-ness (child status change moves the parent bar with
no denormalized column — compute, don't cache), embed parity.

## 8. Stage R — editing and leaving

*The job (Confluence/Notion table stakes): writing feels good, and nothing is
trapped.*

- **R-1 Preview.** HTMX side-by-side markdown preview on page and issue
  editors, rendered by the real `render_body` (embeds included, with their
  budgets). One renderer; preview can never drift from display.
- **R-2 Drafts.** Autosaved page drafts (per user per page, one row,
  migration if needed) that never touch the page's version history until an
  explicit save through the ordinary command. A crashed browser loses
  nothing; the audit trail gains nothing until a human commits. Design the
  ownership boundary before coding: a draft is user-private state, not
  content — decide where it lives and write it down.
- **R-3 HTML export.** A space (or page subtree) exports to a standalone
  HTML bundle through the existing renderer with embeds rendered as their
  **refusal box plus directive text** (an export is a snapshot; live embeds
  must be visibly dead in it, or they'd be stale data wearing a live face).
  JSON portability already exists; this is the human-readable exit.
- **R-4 Attachment images inline.** If `![...]` referencing an Athena
  attachment does not already render inline in pages, make it so through the
  existing visibility-gated download route. Verify first; may be done.

## 9. Parked for the owner — do not build without a written decision

- **Custom fields.** The classic tracker trap (EAV schema, query complexity,
  UI sprawl). The Athena-shaped alternative already exists: labels + the
  Stage M grammar + Stage N embeds. If the owner still wants typed fields
  after using those, design a bounded version (per-project, closed types,
  audited) as its own campaign.
- **Real-time collaborative editing.** CRDTs stay out. Drafts + optimistic
  concurrency (already shipped) is the single-operator answer.
- **Hosting git / code review / CI.** Integration (Stage P) is the line.
- **A block editor / JS framework.** The no-build-chain rule stands; revisit
  only as an explicit owner decision with the trade-offs written down.
- **Outbound forge credentials** (Athena calling GitHub's API): a new stored
  third-party credential and egress surface — needs its own trust-boundary
  design, not a footnote to Stage P.
- **Whether agents may configure a project's statuses** — still open from
  Stage I, still preserved prior behavior.

## 10. Standing method — this is how every stage ships

Learned across A–L; treat as binding.

1. **Recon before code.** Read the real modules you'll touch; the docs have
   been wrong twice (UNDO.md's status claim; COMMAND_MIGRATION's automation
   row). Trust the tree.
2. **The full gate, every stage:** ruff check + format, mypy, import
   contracts, `scripts/smoke_app.py`, `scripts/coverage.sh` (floors are
   enforced; ratchet them when real coverage rises), and the field exercise.
   Extend the field exercise whenever the operator loop grows a step.
3. **Execute, don't infer.** Stage K found two shipped defects and Stage L a
   third only by *running* the composed thing (real HTTP, real sdist). Every
   stage that adds a wire or a package boundary must exercise it for real.
4. **One CHANGELOG heading per type** in Unreleased; correct stale docs in
   the same PR that makes them stale; every deliberate limit is written down
   as a limitation, not left to be discovered.
5. **Verbs are contracts.** New activity verbs are named constants, chosen to
   read in a feed, and added to the event-visibility whitelist deliberately.
6. **Migrations:** forward-only, contiguous from 0069, checksum-bound,
   opt-in semantics (no row = previous behavior), immutability triggers where
   the table is evidence.
7. **PRs:** draft, one stage per PR, body with Outcome / Why / Boundaries /
   Verification (observed numbers only) / Risk and rollback / AI assistance.
   State plainly what was NOT built. Never claim CI ran if it hasn't; never
   tag a release — that is the owner's explicit act.
8. **Order:** M → N → O/P (independent, either order) → Q → R. N depends on
   M. Q's rollup-embed depends on N. Do not parallelize stages that share
   `render_body`.

## 11. How this campaign knows it's working

Not feature count. After each stage, the question is: **what did the operator
stop needing another tool for?** M: stops needing a tracker's query box. N:
stops needing Notion for dashboards-in-docs. O: stops needing Obsidian for
graph and daily notes. P: stops opening GitHub to know what happened to an
issue. Q: stops needing Jira's roadmap. R: stops fearing lock-in. If a stage
ships and that sentence can't be said honestly, the stage isn't done —
and the dogfood instance (the owner's own Athena, running his real backlog)
is where the sentence gets tested.
