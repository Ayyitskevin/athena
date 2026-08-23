# The Final Sprint — one workspace, whole

**Audience:** the implementing session (Opus). Follow the stages in order.
**Authority:** [`AGENTS.md`](../AGENTS.md) is the contract; [`VISION.md`](VISION.md)
is the destination; **the repository is the source of truth** — this guide was
written against commit `eafd039` and may drift, which is why every stage begins
with verification. Where this guide and the code disagree, the code wins and
your run report says so.

---

## What this sprint is

Athena already has the organs: a tracker (Aegis), a knowledge base (Mentor),
links/backlinks/graph, a query language, live embeds, runs with lineage and
replay, controls, budgets, approvals, a tamper-evident trail, an answerability
ledger. What it does not yet have is the **connective tissue that makes an
agent treat it as one place** — the way a human treats Office 365 as one place
even though Word, Outlook, and Planner are separate programs.

The mapping we are completing, and the honest boundaries of it:

| Office habit | Athena organ | State |
|---|---|---|
| Open Outlook, know your day | **The Desk** (Stage F-1) | this sprint |
| Word / OneNote | Mentor pages, versions, drafts, daily note | shipped |
| Planner / Project | Aegis issues, boards, sprints, timeline | shipped |
| SharePoint "the handbook changed" | **Space subscriptions** (Stage F-3) | this sprint |
| Search across everything | **Workspace search** (Stage F-4) | this sprint |
| A macro that turns a checklist into work | **Playbooks** (Stage F-2) | this sprint |
| Built-in help that lives where you work | **The Field Guide** (Stage F-5) | this sprint |
| Excel / custom databases | labels + query grammar + embeds | **refused** (parked product decision) |
| Teams chat / rooms | comments, mentions, notifications | chat itself **refused** |
| Co-authoring cursors | ETag/If-Match + drafts | CRDTs **refused** |

The loop this closes: **embeds** already let docs *show* live work
(work→docs, read). **Run learnings** already let work *write back* to docs
(work→docs, write). **Playbooks** let docs *start* work (docs→work, write).
After F-2 the two modules feed each other in both directions, and after F-1
an agent orients in one call instead of six. That is the "one seamless
ecosystem" claim, made concrete.

---

## Non-negotiable rules (digest — AGENTS.md governs)

1. **One command owns each write.** Transports adapt; `*_commands.py` modules
   own authorization, validation, persistence, and the activity event in one
   transaction. New commands use the transport-neutral **error-kind dialect**
   (`"not_found"`, `"invalid"`, `"conflict"`, `"forbidden"`, `"capacity"`) with
   `_STATUS_BY_KIND` in the route module. Never copy the legacy
   `status_code`-carrying shape.
2. **The web layer owns no data.** Pages render command/data reads. Empty is
   empty.
3. **Migrations** are forward-only, contiguous, checksum-bound, pure SQL.
   The next number is **0073 — verify against `src/athena/core/migrations/`**,
   never against any document (this guide included; that lesson is written in
   `PLANNING.md`).
4. **Zero new dependencies.** The constraints freeze-diff is a CI gate.
5. **Every capability ships REST + MCP together**; web only where an operator
   supervises. MCP bounds import the command module's constants so they cannot
   drift.
6. **Derived states over stored claims. No background sweepers.** Clocks are
   injectable. Expiry/staleness is computed at read time.
7. **Epistemic honesty in every surface string.** Athena records asks, claims,
   and observations — it never asserts an OS effect, never scores, and every
   bounded read that clips says so ("showing N of M", "more exist").
8. **Coverage floors never drop** (line 92.60 / branch 82.30 / combined 90.30,
   excluded-line count exactly 2). Do not add pragmas. Do not put `...` inside
   docstrings (coverage matches stub bodies by line text — that cost this repo
   a red build once already; the warning lives in `core/ids.py`).
9. **Possessive-quantifier discipline for new regexes** on request- or
   body-derived text: no quantified term whose repeated class overlaps its
   successor (see `core/links.py` for the shape and the reason). CodeQL scans
   `src/` with security-extended and does not model `fullmatch` anchoring.
10. **When scope grows, stop and flag.** Restate the new scope in the run
    report and wait. Never quietly absorb, never fake a dependency.

**Run report protocol:** create
`docs/plans/reports/final-sprint-report.md` in your first commit. Every stage
appends: claims-verified table (prompt vs repo), design decisions, deviation
notes, validation evidence (exact commands, exact counts), limitations. The
buzz-run-steering report is the model — including its post-session correction
note about branch identity: state your real branch and push state.

**Per-stage validation gate** (all green before the stage's final commit):
`ruff check .` · `ruff format --check .` · `mypy src/athena` · all three
`scripts/check_*.py` · the stage's test files · a **real-HTTP proof** of the
new surface (bootstrap an `athena-serve`, exercise the feature over the wire,
run `athena-doctor`). Before the sprint's last commit: `scripts/coverage.sh`
full suite + `scripts/smoke_app.py`.

---

## Refused — do not build, do not "improve" into existence

- **No embeddings, vectors, or semantic search.** Zero-dep is the moat. FTS +
  links + the query grammar are the retrieval story.
- **No chat, channels, rooms, or DMs.** Comments on work items and mentions
  are the messaging model. Buzz's rooms were adapted as *shared spaces +
  subscriptions*, not conversation.
- **No reputation scalar** anywhere near answerability. Facts per lane.
- **No custom fields, no block editor, no CRDTs, no JS build, no second
  queue/lifecycle, no alerting daemon.** All previously decided; citations in
  `ROADMAP.md` "Out of scope" and `ANSWERABILITY.md`/`PLANNING.md`.
- **No sync-back for playbooks** (F-2): instantiation snapshots the doc; the
  doc changing later changes nothing already started. A template is not a
  live mirror.

---

## Stage protocol (every stage)

1. **Phase 0 — verify.** Read the named modules. Build the claims table:
   every factual claim this guide makes about existing code, confirmed or
   corrected against the tree. A wrong claim goes in the report and your
   design follows the tree.
2. **Phase 1 — design in the report** before code: data model, vocabularies,
   bounds, error kinds, verbs — recorded, then implemented.
3. **Phases 2..n — implement** in the house order: migration → data module →
   command module → REST → MCP → web (if any) → docs → tests.
4. **Validate** per the gate above. Commit with a message that names what was
   proven, not just what was written.

---

## Stage F-1 — The Desk: one call, full orientation

**Why:** an agent today burns five reads discovering its own state (whoami,
delegations, controls inbox, notifications, budget). The Office habit being
reproduced is *opening Outlook in the morning*. One bounded read that says
"here is who you are, what is asked of you, and what changed since you last
looked" is the single highest-utility ergonomic in this sprint.

**Verify first (claims table):** `identity`/`whoami` shape in `mcp/server.py`;
`delegations.list_delegations` bounds; `run_control_commands.readable_controls`
agent-inbox path; `notifications` unread reader; `budgets.observed`;
`leases`/`claim_handoffs` holder views; `GET /events` cursor semantics
(`after`/`next_after`); the `watches` table shape (0023).

**Data model — migration 0073 (verify the number), `agent_cursors`:**
one row per (user, cursor name): `user_id → users`, `name` (CHECK closed
vocabulary, v1 = `'desk'`), `after_id` (last activity id acknowledged),
`updated_at`. PK `(user_id, name)`. **Advance-only**: a BEFORE UPDATE trigger
refuses a smaller `after_id` (rewinding a read receipt would forge "I never
saw that"). Self-owned: only the cursor's user may move it, enforced in the
command. No delete trigger needed — offboarding semantics follow the user row.

**Core module `core/desk.py`** (read composition, no table of its own beyond
the cursor) returning `athena.agent_desk.v1`:

- `identity`: id, name, role, is_agent, paused, scopes, budget (observed,
  nullable), gated action kinds.
- `asks`: open run controls addressed to me (bounded 20, newest first, with
  `total_open`); pending claim-handoff acknowledgments; kill requests on my
  workers (derived kill_state, unconfirmed only).
- `work`: my delegation inbox (bounded 20 + total); leases I hold with
  generation + expiry-derived state.
- `signals`: unread notification count + newest 10; `events_since_cursor`
  count (COUNT of visible events with id > cursor, capped at 500 with
  `"500+"` semantics — an exact huge number is noise) + `next_after` so the
  agent can drain `GET /events` from exactly there.
- `cursor`: current desk cursor value or null (never seeded implicitly).

Every list carries its own bound and total; every claim-like field keeps the
owning surface's epistemic wording (a control is "asked", a heartbeat is
"reported").

**Command `core/desk_commands.py`:** `read_desk(actor)` (auth: any
authenticated actor — humans get their desk too; agents are the audience) and
`advance_desk_cursor(actor, after_id)` — kind dialect: `invalid` (non-positive,
non-integer), `conflict` (smaller than current — the trigger's twin, checked
first for a clean 409), and the write records **no activity event** (a read
receipt is personal state, the saved-filters/watches precedent — verify that
precedent in COMMAND_MIGRATION.md's personal-state category and cite it).

**REST:** `GET /desk` · `POST /desk/cursor {after_id}` (idempotency root not
required — advancing to the same value is naturally idempotent; refusing
smaller values makes retries safe). **MCP:** `my_desk()`,
`advance_desk_cursor(after_id)` — docstrings teach the loop: *desk → act →
drain events from next_after → advance cursor*.

**Web:** none (agent-facing). The operator's dashboard already exists.

**Docs:** `docs/DESK.md` (house structure incl. what the desk does NOT claim:
it is a snapshot, not a lock, not a queue, and `events_since_cursor` counts
visible events only). Pointers from `OPERATIONS.md` agent section and
`QUICKSTART.md` step 4. ROADMAP "Where the loop stands" Work bullet.

**Bounds table:** lists 20/10 as above; `events_since_cursor` cap 500;
`after_id` ≤ MAX_SQLITE_INTEGER via `RowIdQuery`/body validation.

**Tests (minimum 12):** zero-state desk (fresh agent, all lanes empty and
zero-filled); each lane populated exactly once; visibility (another agent's
controls/delegations never appear); paused agent still reads its desk (pause
blocks at identity — verify and pin whichever way the tree already behaves);
cursor advance + idempotent same-value + refuse-smaller (409 and trigger
backstop via raw SQL); `events_since_cursor` honest cap; human user desk
works; REST/MCP parity; bounds.

---

## Stage F-2 — Playbooks: docs that start work

**Why:** the flagship tie-together. A Mentor page holding a checklist becomes
a parent issue with child issues, each linking back to the page — so
backlinks, embeds, and the knowledge graph light up for free. Docs→work joins
the existing work→docs directions and the two modules become one loop.

**Verify first:** page labels (0037) and how `page_templates.py` reads them;
markdown checklist shape emitted/accepted by the renderer (`- [ ]` /
`- [x]`); `issue_commands` create + parent linkage (`set_parent`) semantics
and hierarchy cycle rules; `links` table write path for issue→page edges
(who owns that write — the indexer reads bodies; confirm whether a body
mention `[[page:N]]` suffices and body-based linking is the correct owner,
which it is unless the tree says otherwise); idempotency middleware roots in
`main.py`; the icarus precedent for server-minted domain idempotency keys.

**Design:**

- A page is a playbook when it carries the label `playbook` (labels exist;
  no new table, no new concept owner).
- `start_playbook` command (new module `mentor/playbook_commands.py` — it
  reads Mentor and writes Aegis **through `issue_commands`**, never raw):
  1. Actor: write role + both `issue:write` intent and page visibility;
     re-check inside the transaction (worker_commands recheck precedent).
  2. Resolve the page; must be visible, labeled `playbook`, not archived.
  3. Parse checklist items from the **stored body at its current version**:
     lines matching `- [ ]`/`- [x]` (unchecked only become work; checked
     lines are recorded in the activity detail as skipped count). Bounds:
     1..50 items, each title trimmed ≤ 200 chars; 0 items → `invalid`; > 50 →
     `capacity` (429 — the worker_commands mapping; note the repo's
     acknowledged 409/429 divergence and pick 429, recording it).
  4. Create ONE parent issue (`title` = page title unless caller overrides,
     body cites the playbook: `Started from [[page:N]] at version V` — the
     wikilink makes the links/backlinks machinery do the tying-together) and
     one child per unchecked item (bodies carry the same citation), all in
     one transaction, all through `issue_commands`, project-scoped by the
     caller's `project_id` (nullable = backlog, matching issue create).
  5. Domain idempotency: `(requested_by, idempotency_key)` replay returns the
     same instantiation; key minted `secrets.token_hex(16)` when omitted
     (icarus precedent). Store the instantiation as... **verify**: prefer
     zero new tables — the parent issue's activity event
     (`verb='playbook_started'`, target the parent issue, detail carrying
     page id + version + item count) IS the record; idempotency then needs a
     small table `playbook_starts(requested_by, idempotency_key UNIQUE,
     parent_issue_id, page_id, page_version_id, created_at)` — one row, no
     lifecycle, no triggers beyond immutability. Migration number: next free.
  6. Snapshot semantics stated everywhere: later edits to the page change
     nothing; re-running the playbook is a NEW instantiation (new parent) —
     deliberately allowed, that is what templates are for.
- **Error kinds:** `not_found` (page invisible/missing — same answer),
  `invalid` (not a playbook label, no unchecked items, bad title override),
  `capacity` (>50), `conflict` (idempotency reuse with different binding),
  `forbidden` (role/scope).

**REST:** `POST /pages/{page_id}/start-playbook` (201, body:
`project_id?`, `title?`, `idempotency_key?`) — add the root to
`_IDEMPOTENCY_API_ROOTS` only if the route lives under a new root (it lives
under `/pages` — **verify** whether that root is already opted in).
**MCP:** `start_playbook(page_id, project_id=None, title=None,
idempotency_key=None)` docstring: "creates real issues; a template is not a
live mirror — editing the page later changes nothing already started."
**Web:** on a playbook-labeled page, an admin/write-role button (PRG + CSRF +
minted hidden idempotency key, run_lineage form precedent) posting to a
browser twin that calls the same command; success redirects to the parent
issue.

**Docs:** `docs/PLAYBOOKS.md` — the loop diagram (embeds: work→docs read;
learnings: work→docs write; playbooks: docs→work write), snapshot semantics,
bounds, the "one concept, one owner" note (labels, issue_commands, links all
reused). CHANGELOG. ROADMAP Direct bullet.

**Tests (minimum 14):** happy path (parent + N children, backlinks from page
show the new issues, embed `kind: rollup` on the page shows child counts —
prove the loop end to end in ONE test); checked items skipped and counted;
bounds (0, 51, long titles); label missing → invalid; archived page; hidden
page = 404-shape; project routing incl. backlog; idempotent replay + key
reuse conflict + UNIQUE backstop race (Barrier); visibility of created issues
follows project rules; activity verb + detail; snapshot semantics (edit page
after start → nothing changes; restart → new parent); web button CSRF +
double-submit dedupe; REST/MCP parity; error status mapping.

---

## Stage F-3 — Space subscriptions: shared memory that says when it moved

**Why:** buzz's room-membership, adapted honestly: agents that share a space
as memory need to know it changed without polling every page. SharePoint's
"the handbook changed" for a fleet.

**Verify first:** `watches` (0023) has NO target_kind CHECK (**verify** by
reading the migration + any code-level vocabulary in `notifications.py` /
watch commands — if a closed vocabulary exists in code, extending it is the
change; if a CHECK exists, that is a migration); `notify_watchers` fan-out
call sites for page events (`activity.record` calls it with target_kind/id —
a page event fans out to PAGE watchers today); where watch writes live
(personal-state category?).

**Design:** watching a space (`target_kind='space'`) subscribes to **page
lifecycle events within that space**. Implementation: in the page-event
recording path, after the existing page-watcher fan-out, also fan out to
watchers of the page's space — **inside `notifications.notify_watchers`**
(single owner of fan-out; verify it can resolve space from a page target
cheaply — one lookup per page event, acceptable) — never a second fan-out
call site. Self-notifications stay suppressed (actor excluded, existing
rule). Bounds: notification writes are already bounded by watcher count;
document that a space watch multiplies inbox volume and that `unwatch` is
the remedy (no digest, no rollup — refused: alerting daemon).

**Surfaces:** whatever the existing watch surface shape is (REST
`PUT/DELETE /watches/{kind}/{id}`? — **verify and extend, don't invent**),
plus MCP `watch_space`/`unwatch_space` or the existing watch tool extended
with the new kind. Desk (F-1) already shows unread notifications, so the
loop closes with zero extra UI.

**Docs:** section in the notifications/watches doc + `QUICKSTART.md` agent
step ("watch the space your fleet uses as shared memory"). Tests (minimum
8): watch → page create/edit/archive in space notifies; page in OTHER space
does not; actor's own edit does not; unwatch stops; visibility (a watcher
who cannot see the space gets nothing — verify how page watches handle this
today and match); duplicate watch idempotent; kind vocabulary refusal for
garbage kinds; parity.

---

## Stage F-4 — Workspace search: one ask, everything you may see

**Why:** Office's single search box. The pieces exist (issue query grammar,
page/comment FTS); an agent should not need to know which module owns what.

**Verify first:** `search.search` capabilities + visibility gating;
`issue_query` entry point + its atom grammar; what `GET /search` serves
today; result shapes.

**Design:** `core/workspace_search.py` — `search_workspace(conn, actor, q,
limit_per_kind=10)`: runs page/comment FTS AND (when `q` contains no
grammar atoms — **verify** how to detect: reuse the query parser's own
classification, never a second parser) a title/text issue search; when `q`
IS grammar-shaped, delegate issues to the grammar. Returns typed groups
`{issues: [...], pages: [...], comments: [...]}` each with `clipped` flags
and per-kind totals-if-cheap (else `has_more`). No new index, no ranking
invention — each source keeps its own order, and the response SAYS results
are grouped by kind, not globally ranked (honesty over fake relevance).
Bounds: limit_per_kind 1..25.

**REST:** `GET /search/workspace?q=&limit_per_kind=` (auth: same bar as
existing search — **verify** and match). **MCP:** `search_workspace(q)` —
docstring names the grammar passthrough ("`is:open label:infra` works here").
**Web:** none (the operator has module search; this is agent ergonomics).

**Tests (minimum 8):** plain text hits pages+issues+comments; grammar query
routes to the grammar (and pages still searched with the raw text — decide
and pin); visibility per kind (anon vs member vs admin); clipped flags; empty
q → `invalid`; bounds; parity; unknown grammar atom surfaces the grammar's
own error (never empty results — that contract already exists; extend it).

---

## Stage F-5 — The Field Guide: the workspace documents itself

**Why:** Office ships its help inside Office. An agent's manual should be
pages in the same knowledge layer it uses for everything else — readable via
the same MCP tools, linkable from issues, exportable like anything else. It
also gives every fresh install a non-empty, useful first screen.

**Verify first:** how `demo.py` seeds (it goes through commands as a real
user — the pattern to reuse); package-data inclusion rules (MANIFEST.in +
pyproject) so guide content ships in the wheel; space key uniqueness
behavior.

**Design:** `athena-demo --field-guide` (extend the existing console script —
**one owner for seeding**, never a second seeder; plain `--seed-only` keeps
current behavior) creates space key `GUIDE` ("Athena Field Guide") with ~8
pages authored as package data (markdown files under
`src/athena/field_guide/*.md`, loaded not inlined): *Your Desk* · *Claiming
and yielding work* · *Recording learnings* · *Answering run controls* ·
*Playbooks* · *Searching the workspace* · *Watching shared memory* · *What
the trail proves (chain, answerability)*. Each page teaches with the actual
MCP tool names and cites the deeper doc. Content tone: the repo's epistemic
voice, second person, agent-addressed.

Idempotent: existing `GUIDE` space → refuse with a clear message (never
overwrite operator-modified guides — their workspace, their pages now).
Seeded through commands as the invoking admin, so provenance is real and the
chain covers it. One playbook-labeled example page so F-2 is demonstrable
out of the box.

**Docs:** QUICKSTART gains "seed the field guide" as the recommended step 3;
README one line. **Tests (minimum 6):** seed → 8 pages visible, wikilinks
resolve, playbook example carries the label; second run refuses without
touching pages; content loads from package data (wheel-integrity: add the
new package-data glob and pin it in the existing wheel-manifest test —
**verify** where that lives); pages readable over MCP; guide export works
(HTML export smoke on GUIDE).

---

## Stage F-6 — The debt tail (make the base boring)

Three items, each its own commit:

1. **If-Match on browser edit forms** (pages first, issues second): hidden
   etag field stamped at render (the drafts `based_on` machinery is the
   precedent and already carries the session baseline); on mismatch,
   re-render the form with BOTH texts visible (yours + theirs, theirs wins
   the form fields; yours preserved in the draft) and a truthful notice —
   never silent last-write-wins, never a hard 412 page that eats work.
   **Verify** the API's ETag semantics and reuse its comparison. Tests: the
   losing-editor scenario end to end, draft preserved, notice wording.
2. **Command-dialect migration** for at least: `space_commands`,
   `comment_commands`, `sprint_commands`, `token_commands`,
   `webhook_commands` (the five most write-active — **verify** the current
   debt list in `COMMAND_MIGRATION.md` and take the top five by traffic).
   Mechanical per module: kinds + `_STATUS_BY_KIND` in routes + tests
   asserting identical wire statuses (the contract is the HTTP surface does
   NOT change — pin it).
3. **`capacity` kind decision recorded**: one paragraph in
   `COMMAND_MIGRATION.md` stating the 429 convention for new modules and the
   preserved 409 exceptions by name (agent_runs check-ins), ending the
   per-PR relitigating.

---

## Stage F-7 — Adversarial review, fix pass, ship

The house closing move, non-negotiable:

1. Six-lens review of the whole sprint diff (correctness per stage ×3,
   security, doctrine, tests/docs), each finding independently verified
   against the tree. Confirmed findings get fixes with regression tests in
   the same session.
2. Full gate: `scripts/coverage.sh` (floors + excluded-count 2),
   `scripts/smoke_app.py`, real-HTTP feature proof touching every new
   surface in one scripted pass (desk → watch space → seed guide → start
   playbook from the guide's example → workspace-search finds the children →
   drain events from the desk cursor — the DEMO IS THE ECOSYSTEM CLAIM).
3. Report appendix: what was found, what was fixed, what is refuted-with-
   reason, residual risks named.
4. CHANGELOG entries per stage; ROADMAP "Where the loop stands" updated;
   VISION loop preamble gains one sentence naming the desk and playbooks.
5. Push, PR per the branch instructions in force, subscribe, drive CI to
   green. CodeQL findings are readable: check-run pages are public, and the
   SARIF uploads as an artifact per analysis run.

---

## Parked for the owner (do not build; do not decide)

- Release tagging, PyPI publication, repo security settings — issue #324.
- Public/proxy deployment shapes; the forge public edge.
- Any second executor implementation for the dispatch contract.
- Custom fields / structured databases (the standing product decision).

---

## Launch prompt (paste to the implementing session verbatim)

> Read `docs/OPUS_FINAL_SPRINT_ATHENA.md` end to end, then `AGENTS.md` and
> `docs/VISION.md`. Execute the sprint stage by stage, in order, following
> the stage protocol exactly: verify the guide's claims against the tree
> before designing, record the design in
> `docs/plans/reports/final-sprint-report.md` before implementing, and run
> the per-stage validation gate before each stage's final commit. The
> repository is the source of truth over the guide; deviations go in the
> report. Work on the branch your session designates; never push elsewhere.
> If any stage's scope grows beyond its spec, stop, restate the scope in the
> report, and wait. Finish with Stage F-7 in full — the sprint is not done
> until the adversarial review's confirmed findings are fixed and the
> composed real-HTTP ecosystem proof passes.
