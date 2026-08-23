# Run Report — Buzz Run Steering v1

> **Post-session correction (2026-08-01):** this report's "Branch" and
> "nothing pushed" statements describe the authoring session's local state and
> did not survive it. The work actually landed on
> `claude/athena-buzz-integration-4hkyx8`, was pushed there, and continued
> (Stage Q, R-1–R-4, and the round-2 features). The technical content below is
> unchanged; read the delivery-summary claims as historical.

Branch: `codex/buzz-run-steering-v1`
Starting SHA: `37d6e410bd2c708dff5d43b6347d279da05ea512` (origin/main at session start —
identical to the research anchor commit, so main had not moved past the research SHA).
Plan file used: **none** — neither `docs/plans/buzz-run-steering-v1.md` nor branch
`plan/buzz-run-steering-v1` exists locally or on the remote (verified via
`git ls-remote --heads origin`). Proceeding under the implementation prompt alone,
with a preliminary architecture-verification phase recorded below.

This report is the handoff artifact for a separate review session. Every phase
appends a status entry before the next phase begins.

---

## Phase 0 — Architecture verification

Status: **COMPLETE**. Method: 12 parallel read-only subsystem audits (runs, workers,
activity, auth, command conventions, db/migrations, MCP, REST, web, delegation/
dispatch/approvals, docs/CI, pause/revocation), each returning file:line-cited
facts, plus direct reads of `AGENTS.md`, `worker_commands.py`,
`agent_run_commands.py`, `activity.py` (record/_bind_run), `run_context.py`,
`idempotency.py`, migrations 0046/0048/0063/0065, `events_api.py`, `dispatch.py`,
`web/activity.py`, `run_lineage.html`, `scripts/check_write_ownership.py`.

### Prompt claims vs repository

| Prompt claim | Verdict | Evidence |
|---|---|---|
| Athena has existing runs | CONFIRMED with a twist | **No runs table.** A run is a projection of the append-only `activity` log (`docs/RUNS.md`); run identity = opaque client string ≤200 chars (`run_context.strict_run_id`); ownership = `run_bindings(run_id PK, actor_id, bound_at)` (0048), claimed by first tagged writer inside the write transaction (`activity._bind_run`, RunBindingError→403). Check-ins (`agent_run_checkins`, 0046, PK (agent_id, run_id)) are a deliberate control-flag-free sidecar. Reserved namespaces `automation:`/`icarus:` are server-minted. |
| Existing worker records | CONFIRMED | `agent_workers` (0065) with the cooperative kill contract: `kill_requested_at/by`, `kill_acknowledged_at`, `stopped_at` as three separate timestamp facts; "a worker that goes silent after a kill request is STALE, never terminated". Workers hold no per-worker credential — they authenticate with their agent's bearer token. |
| Existing delegation | DRIFTED (still true in spirit) | No delegation table; delegation = `issue_contributors` rows + a read-only pickup projection (`aegis/delegations.py` — "does not create a second queue or lifecycle"). Not a reusable request/settle primitive. |
| Existing activity + cursor feeds | CONFIRMED | `activity.record(conn, *, actor_id, verb, target_kind, target_id:int, detail, commit=False, ...)` stamps ambient run context; `GET /events` forward cursor (`after`/`next_after`/`has_more`); `GET /activity` backward; run lineage/replay/fork projections. No pub/sub — poll + webhook loop only. Non-issue/non-project target kinds get an EMPTY visibility scope → events are visible to authenticated feed readers (worker_* and dispatch_* precedents). |
| Existing identities/authority | CONFIRMED | Agent = `users.is_agent=1`; scoped bearer tokens with revocation; `identity.is_admin` + `token_has_scope(ADMIN_SCOPE)` for operators (browser sessions pass scope checks); commands re-resolve `is_agent` + token liveness inside the write transaction (`worker_commands._recheck_credentials`). Pause (`users.paused_at`, 0049) is enforced at identity resolution for EVERY authenticated call — a paused agent cannot read, heartbeat, or settle anything (403 "account is paused") — plus in-transaction rechecks in migrated commands. |
| Command layer owns writes | CONFIRMED and machine-enforced | `scripts/check_write_ownership.py` (CI): transports (`web/*`, `mcp/*`, `*_api.py`) may not contain write SQL or call data-module writers; writes live behind `*_commands.py` modules. Target-shape modules: `worker_commands`, `agent_run_commands`, `issue_commands`. |
| Idempotency exists | CONFIRMED, two layers | (a) ASGI `IdempotencyMiddleware`: durable replay keyed (Idempotency-Key, identity, fingerprint), opt-in per API root via `_IDEMPOTENCY_API_ROOTS` (main.py:368) — new roots must be added there; mismatched-payload reuse → 409 `idempotency_mismatch`. (b) Domain-level: `icarus_dispatches.idempotency_key TEXT NOT NULL UNIQUE`, minted in the command via `secrets.token_hex(16)` when the caller omits it (aegis/icarus_commands.py) — the precedent Run Controls follows. |
| Cancellation via "existing worker semantics" | CONFIRMED as pattern, absent at run level | There is NO run-level cancel anywhere. The only stop lever is the worker-scoped cooperative kill (`POST /workers/{id}/kill`). Run Controls' `request_cancel` is new machinery mirroring that contract at run granularity, not a wrapper over it. |
| Structured handoff precedent | CONFIRMED | `lease_commands` yield-handoff: bounded fields (attempted_work ≤4000, evidence ≤10×1000 items / ≤8000 JSON, blocking_question ≤1000, resume_instructions ≤4000) with DB `json_valid`/`json_each` trigger enforcement (0058). `request_fresh_context` responses mirror this shape. |
| Buzz-style per-context queues / cursor replay | Adapted, not copied | Athena's own activity id cursor IS the replay feed; per-run serialization comes from SQLite BEGIN IMMEDIATE single-writer + CAS predicates (dispatch.py mark_*/record_terminal precedent), not a queue structure. No Buzz code is used. |
| Roadmap names this feature | WRONG | grep over docs/: no "run steering", "fleet room", "control request", "fresh context" anywhere. This is net-new roadmap territory; nearest precedents are the worker kill and approval gates. ROADMAP/VISION updates must be written as additions, not checkbox flips. |

### Constraints that shaped the design

- **Expiry has no precedent** (approvals have no TTL; kill requests never expire).
  House doctrine: states are DERIVED at read time from the server clock vs stored
  timestamps (worker staleness), never stored claims, never background sweepers.
- **Two settlement idioms** exist: approvals' read-then-409 and dispatch's
  state-predicated UPDATE (CAS, rowcount-checked). Dispatch's is documented as the
  canonical raced-settlement answer; Run Controls uses it.
- **`capacity` kind maps to 429** in `worker_commands.STATUS_BY_KIND` but 409 in
  `agent_runs_api` — an acknowledged repo inconsistency; new module picks 429.
- **CI gates beyond AGENTS.md**: ruff format --check, mypy (3.12), constraints
  freeze-diff (zero new deps), coverage floors 92.60 line / 82.30 branch (no new
  pragmas), wheel-from-sdist boot (new runtime files must be in package-data —
  migrations `*.sql` and templates already are).
- **Local env**: repo `.venv` is Python 3.14 (works for pytest/ruff, lacks mypy);
  a CI-mirroring Python 3.12 venv (`.venv312`, via uv, `-c constraints/ci-py312.txt`)
  is used for the full gate. Both are gitignored (verified).

---

## Phase 1 — Design (recorded before implementation)

Status: **COMPLETE**. Everything below maps a prompt requirement onto a repo-native
mechanism; deviations from the prompt's literal wording are flagged inline.

### Naming

Feature name: **Run Controls** (the "Fleet Room" is the derived view of these plus
existing run/worker/check-in projections — no new source of truth). Table
`run_controls`, data module `core/run_controls.py`, command module
`core/run_control_commands.py`, REST root `/run-controls`, verbs
`run_control_requested|acknowledged|declined|completed`. Control kinds keep the
prompt's closed vocabulary: `steer`, `request_cancel`, `request_fresh_context`
(no stronger repo convention exists — Phase 0 confirmed the roadmap does not name
this feature).

### Data model (migration `0070_run_controls.sql`)

One row per control; lifecycle facts as separate nullable timestamp columns
(0065 doctrine), settlement as a CHECK-constrained pair, expiry **derived at read
time** from `expires_at` vs the server clock — never stored, so a silent agent
reads as "requested (expired)" and Athena never claims an outcome nobody reported:

- Identity/binding: `id` INTEGER PK; `schema_version` (=1 CHECK); `run_id` (house
  1–200 trimmed CHECK); `agent_id` → users (owner resolved at admission);
  `worker_id` → agent_workers (optional targeting metadata — workers hold no
  credential of their own, so it cannot be credential-enforced; documented);
  `kind` CHECK-closed; `payload` ≤4000; `requested_by` → users;
  `idempotency_key` (1–255, UNIQUE per requester via index, minted
  `secrets.token_hex(16)` when omitted — icarus precedent); `created_at`;
  `expires_at` (CHECK > created_at).
- Lifecycle facts: `acknowledged_at`; `settled_at` + `settled_by` + `settlement`
  CHECK IN ('completed','declined') (all-or-none CHECKs); `result_summary` ≤2000;
  `result_payload` JSON object ≤8000 (fresh-context handoff only, `json_valid` +
  shape CHECKs); activity correlation `requested_event_id` /
  `acknowledged_event_id` / `settled_event_id` → activity.
- DB-enforced invariants (0058 idiom): transition-only BEFORE UPDATE triggers
  (immutable identity columns; ack set once; settled row frozen; event ids bind
  only to native unrestricted activity rows); BEFORE DELETE abort; UNIQUE
  `(requested_by, idempotency_key)`; partial index for the agent-inbox hot query
  `(agent_id, id DESC) WHERE settled_at IS NULL`; run-history index
  `(run_id, id DESC)`.
- At-most-one **live** control per (run_id, kind) is enforced in the create
  command under `BEGIN IMMEDIATE` (the repo's documented answer to
  check-then-write races) because "live" includes `expires_at > now`, which a
  partial index cannot express. The settlement CAS is a state-predicated UPDATE
  (dispatch `record_terminal` pattern), rowcount-checked, with the triggers as a
  DB-level backstop against non-CAS writes.

### Admission (create) — fail-closed rules

Operator = `identity.is_admin` + `token_has_scope(ADMIN_SCOPE)` (worker-kill
precedent; browser admin sessions pass). Inside one immediate transaction:
1. `strict_run_id`; reserved namespaces (`automation:`, `icarus:`) refused —
   server-minted runs have no polling principal (design decision).
2. Owner resolution: `run_bindings.actor_id` if bound; else the run's check-in
   agent when exactly ONE agent has checked in; no binding and no check-in →
   `not_found`; multiple check-in agents, unbound → `conflict` (ambiguous);
   resolved owner not `is_agent` → `invalid`.
3. Owner paused → `conflict` (a paused agent cannot even read the control —
   Phase 0: pause 403s everything — so admitting one would be a request nobody
   can receive). "Terminal runs" have no stored equivalent (runs are
   projections); the nearest real facts are enforced instead: paused owner,
   stopped worker (below), and expiry.
4. `worker_id` given → must exist, belong to the owner (`invalid`), and not have
   `stopped_at` set (`conflict`).
5. Idempotency: existing (requester, key) row with identical binding
   (run_id, kind, payload, worker_id) → returned as a replay; different binding →
   `conflict`. DB UNIQUE index backstops the race.
6. Duplicate: a live (unsettled, unexpired) control of the same (run, kind) →
   `conflict`.
7. INSERT + `activity.record(verb=run_control_requested, target_kind='run_control',
   target_id=id, commit=False)` + backfill `requested_event_id`, one transaction.
   The operator's event carries the OPERATOR's ambient run context (never the
   target run's — writing under the agent's run id would violate the run binding);
   the linkage to the target run is the control row itself.

### Settlement (acknowledge / decline / complete) — bound-agent only

Agent credentials exactly as `worker_commands`: bearer `_token_id`, `is_agent`,
write scope, then in-transaction recheck of users.is_agent + token liveness +
**paused_at** (issue_commands defense-in-depth precedent). Then:
- Control must exist AND `agent_id == actor.id` — anything else is the same
  `not_found` a missing id gives (cross-tenant probe collapse).
- Ownership drift recheck: the run's CURRENT resolved owner must still equal the
  control's admitted `agent_id`; a binding that appeared under a different actor
  since admission → `conflict` ("run ownership changed").
- Expired (server clock) → `conflict`; settled → `conflict`; late ack → same.
- `acknowledge`: CAS `SET acknowledged_at WHERE id=? AND settled_at IS NULL AND
  acknowledged_at IS NULL AND expires_at > :now`; already-acknowledged is an
  idempotent no-op returning current state (request_kill precedent, no event).
- `decline`: requires bounded reason; CAS settle `settlement='declined'`.
- `complete`: steer/request_cancel require bounded `summary`; request_fresh_context
  requires the structured handoff {summary ≤2000; unresolved_questions ≤10×500;
  athena_refs ≤20×200; evidence_refs ≤10×500} stored as canonical JSON ≤8000 —
  never transcripts, never chain-of-thought (validated field-by-field).
- Completion/decline/ack are recorded as the AGENT'S CLAIMS (epistemics doctrine);
  acknowledgement proves receipt only; completion proves nothing about OS effects.
  Settlement events inherit the agent's ambient run context, so an agent that
  stamps `X-Athena-Run: <run>` joins its own run's replay legitimately.
- Expiry emits NO event and no state write — it is a derivation, exactly like
  worker staleness ("stale, never terminated").

### Reads / derived state

`state` is derived per read (injectable `now` for tests, workers.py pattern):
`completed`/`declined` when settled; else `expired` when `expires_at <= now`;
else `acknowledged`/`requested`. Visibility: admins and the bound agent; everyone
else gets `not_found`/empty (worker registry precedent). Activity events for
controls are authenticated-visible like worker/dispatch events; the bounded
`detail` string never contains payloads.

### Surfaces

- REST (`core/run_controls_api.py`, prefix `/run-controls`, opted into
  `_IDEMPOTENCY_API_ROOTS`): POST `` (create, 201; domain replay 200);
  GET `` (?run_id=&state=&limit=, bare bounded list); GET `/{id}`;
  POST `/{id}/acknowledge`; POST `/{id}/decline` {reason};
  POST `/{id}/complete` {summary | handoff}. `_refuse` + module STATUS_BY_KIND
  {401,403,404,422, conflict:409, capacity:429}.
- MCP (`client.py` + `server.py`): `create_run_control`, `list_run_controls`,
  `get_run_control`, `acknowledge_run_control`, `decline_run_control`,
  `complete_run_control` — one-line delegations to client methods hitting REST.
- Web: Run Controls panel on the existing run lineage page
  (`/aegis/activity/runs/{run_id}/lineage`) — visible to admins and the bound
  agent; create form (admin only, plain PRG form + CSRF, minted idempotency key
  hidden field); truthful wording ("recorded request", "the agent reported...").
  No new page, no JS build, no second feed.
- Docs: new `docs/RUN_CONTROLS.md` (house structure incl. explicit
  what-this-does-NOT-claim section); pointers from RUNS.md and WORKERS.md;
  ROADMAP "Where the loop stands" Intervene bullet; VISION loop preamble link.

### Config

`ATHENA_RUN_CONTROL_TTL_SECONDS` (default 3600) — default TTL when the operator
does not pass one; bounds 60..86400 as module constants.

### Explicitly out of scope (v1)

Operator withdrawal of a control (not in the prompt's closed lifecycle; short
TTLs are the mitigation — noted as a follow-up), fleet-attention rollup card,
automation triggers on control verbs, ETag/If-Match concurrency (CAS makes it
unnecessary), steering server-reserved runs.

### Phase status log

- Phase 0 (architecture verification): **COMPLETE**
- Phase 1 (design): **COMPLETE**
- Phase 2 (domain/command layer + migration): **COMPLETE**
- Phase 3 (REST): **COMPLETE**
- Phase 4 (MCP): **COMPLETE**
- Phase 5 (web panel): **COMPLETE**
- Phase 6 (docs): **COMPLETE**
- Phase 7 (validation + adversarial review + report): **COMPLETE**

### Phase 2 — Domain/command layer + migration: COMPLETE

Delivered: migration `0070_run_controls.sql` (table, three indexes incl. domain
idempotency UNIQUE, nine CHECK invariants, eight triggers: birth-shape, request
immutability, claims write-once, settled-frozen, three native-event bindings,
no-delete); `core/run_controls.py` (all table SQL, CAS transitions, derived-state
projection with injectable clock); `core/run_control_commands.py` (create/
acknowledge/decline/complete/visible/readable, worker_commands-shaped auth with
in-transaction recheck incl. paused, fail-closed admission and settlement);
two read helpers added to the table-owning modules (`activity.run_binding_actor`,
`agent_run_checkins.checkin_agent_ids`); `config.RUN_CONTROL_TTL_SECONDS`.

Validation at this phase boundary (all green, `.venv312` = CI-mirror Python 3.12.13):
- `ruff check` + `ruff format --check` on changed files
- `mypy src/athena` — no issues in 152 modules
- `python scripts/check_import_contracts.py` / `check_write_ownership.py` /
  `check_imported_at_guards.py`
- `pytest tests/test_migration_integrity.py tests/test_migrations_atomic.py -q` — 20 passed
- `pytest tests/test_activity.py tests/test_activity_runs.py
  tests/test_agent_run_checkins.py tests/test_workers.py -q` — 72 passed (no regressions)
- End-to-end scratch exercise of every command path (create, replay, conflicting
  key, live duplicate, ack, complete, double-settle, handoff, derived expiry,
  late ack, SQL tamper/delete blocked by triggers, list filters, unknown/
  reserved/non-admin refusals) — all behaved as designed.

Deviation from design: none. One addition — `idempotency_key` is included in the
read projection (operator metadata needed for retry ergonomics).

### Phase 3 — REST endpoints: COMPLETE

Delivered: `core/run_controls_api.py` (`/run-controls` router — create 201,
list with derived-state filters incl. `open`, get, acknowledge, decline,
complete; opaque RunId/Handoff schemas with command-side validation; `_refuse`
mapping via the command's STATUS_BY_KIND) and `main.py` wiring (router include +
`/run-controls` added to `_IDEMPOTENCY_API_ROOTS`).

Validation: live-HTTP smoke over TestClient (fresh DB, all 70 migrations applied
incl. 0070): bootstrap → onboard agent → heartbeat-only run steered via the
check-in fallback → create 201 → agent inbox → third-user list `[]` and get 404
(existence oracle closed) → admin settlement refused 403 "bearer token required"
→ ack 200 acknowledged → complete 200 completed → middleware Idempotency-Key
replay (`Idempotent-Replay: true`) and mismatch 409 `idempotency_mismatch` →
fresh-context handoff completion → decline with reason. All as designed.

### Phase 4 — MCP tools: COMPLETE

Delivered: six `AthenaClient` methods (`create_run_control`, `list_run_controls`,
`get_run_control`, `acknowledge_run_control`, `decline_run_control`,
`complete_run_control`) hitting the REST surface, and six `build_server` tools
(one-line delegations; `@mutation_tool` for writes with optional
idempotency_key; bounds via Annotated aliases importing the command module's
MAX_* constants so MCP limits cannot drift from domain limits; docstrings carry
the epistemics: "records a request", "your claim", "expired means the clock ran
out").

Validation: scripted MCP smoke over an injected TestClient-backed AthenaClient —
admin create via admin-scoped bearer, agent inbox (`state=open`), acknowledge,
complete, double-settle surfaced as AthenaError 409 "control is already
settled", admin read-back, and `build_server` tool enumeration confirming all
six tools registered (113 total).

### Phase 5 — Run Controls web panel: COMPLETE

Delivered: a "Run controls" dashboard-card on the existing run lineage page
(`aegis/run_lineage.html`) — controls table with truthful state wording
("receipt only — no outcome yet", "expired means the clock ran out"), settled
answers incl. the structured handoff rendered field-by-field, and an admin-only
create form (plain PRG form + CSRF + a per-render minted idempotency key so a
double submit records one control). `web/activity.py` gained the panel context
in `activity_run_lineage` (defensive around legacy lenient run ids) and the
browser-twin POST `/aegis/activity/runs/{run_id}/controls` through the same
command. No new page, no JS, no build system.

Validation: scripted browser-session smoke — admin login, panel + form render,
CSRF-gated create 303 with recorded-request notice, control row visible,
double-submit dedupe (one control after form resubmit), agent settlement via API
visible on reload, non-admin signed-in user sees no panel and no payload text,
CSRF-less POST refused 403.

Limitation (recorded): heartbeat-only runs are steerable over REST/MCP but have
no lineage page (the page requires visible activity events), so their controls
are managed via API/MCP only.

### Phase 6 — Documentation: COMPLETE

Delivered: `docs/RUN_CONTROLS.md` (house structure: VISION citation, the three
words the feature refuses to say, closed kind table, whose-claim lifecycle
table, authority rules, fenced REST examples, MCP tool names, handoff bounds
table, trail semantics, negative-claims section); pointer paragraphs in
`RUNS.md` and `WORKERS.md`; a new Intervene bullet in `ROADMAP.md` "Where the
loop stands"; and a `VISION.md` loop-preamble mention — all describing only
implemented behavior.

### Phase 7 — Full validation, adversarial review, final report: COMPLETE

Tests delivered: `tests/test_run_controls.py` — 28 deterministic tests (26 in
the initial suite, 2 added closing the adversarial review's gaps) (real
HTTP via TestClient, direct commands below the transport, raw SQL against the
triggers, and the MCP client against the real app): happy paths for all three
kinds; heartbeat-only steering; creator authority (agent/member/anonymous/
narrow-scoped-admin-token all refused); settlement identity (wrong agent
collapses to the missing-control 404; sessions and trusted headers refused);
wrong run / ambiguous check-ins / human-owned runs / reserved namespaces /
wrong-agent worker / stopped worker; revoked tokens (stale-actor recheck below
the transport + 401 at identity), pause (settle 403, create 409, no side
effects); ownership drift blocking both agents; domain idempotent replay,
conflicting reuse, UNIQUE backstop; transport Idempotency-Key replay and
mismatch; at-most-one-live per (run, kind) with freed slots after settlement;
Barrier-coordinated settlement and creation races; derived expiry with injected
clocks (late ack/complete/decline refused, zero writes, zero events, expired
slot does not block a fresh ask); CAS expiry-boundary predicate; payload/
result/handoff/TTL bounds incl. the closed handoff envelope; visibility
isolation + SQL-level state filters + limit clamps; feed consistency (verbs on
the /events cursor, operator event untagged, tagged agent settlement joins its
run's replay); trigger tamper-proofing (immutable request, frozen settled row,
no delete, write-once pointers, forged event pointers, born-settled rows);
command guards below the transport; MCP/REST parity incl. error status+detail
equality; web panel truthful wording, CSRF, double-submit dedupe, and
non-admin/non-agent concealment. Plus 4 entries each in `test_mcp_client.py`'s
two mutation-contract registries.

Exact validation commands and results (`.venv312` = Python 3.12.13, deps pinned
by `constraints/ci-py312.txt`, mirroring `.github/workflows/ci.yml` test job):

| Command | Result |
|---|---|
| `ruff check .` | passed |
| `ruff format --check .` | passed (384 files) |
| `mypy src/athena` | no issues in 153 source files |
| `python scripts/check_import_contracts.py` | passed, 153 modules |
| `python scripts/check_write_ownership.py` | passed |
| `python scripts/check_imported_at_guards.py` | passed |
| `scripts/coverage.sh <fresh dir>` (= `pytest -q -n 4 --strict-markers --cov` + floors) | **3104 passed, 0 failed** (final run, after the review fixes; an earlier full run at the pre-review tip: 3102 passed); line 92.87% ≥ 92.60 floor; branch 83.24% ≥ 82.30; combined 90.67% ≥ 90.30; excluded lines 2 (existing policy) |
| `python scripts/smoke_app.py` | passed (installed `athena-serve` bootstrap → restart → packaged assets → session auth → clean stop) |
| Real-HTTP feature proof | booted `athena-serve --bootstrap` on :8777 (trust-header OFF — the deployment gate correctly refused an earlier attempt with it on); bootstrap 201 over HTTP; agent heartbeat; create 201 → inbox → acknowledge → complete; all three verbs observed on `GET /events`; server stopped cleanly |

Adversarial diff review: five parallel lenses (correctness, authority/security,
repo doctrine, test adequacy, protocol-contract completeness), each finding then
handed to an independent verifier instructed to refute it against the actual
code (several reproduced findings live against the running app). 15 findings
survived verification (1 major code defect, 2 major test gaps, 12 minor incl.
cross-lens duplicates — 10 unique). **All were fixed** in commit
`fix(steering): close the adversarial review's confirmed findings`:

1. **[major] Binding poison via the admission's own audit event.** An admin
   create tagged `X-Athena-Run: <target run>` against a check-in-only run would
   record the request event under the operator's ambient run context, whose
   run-binding claim binds the target run TO THE OPERATOR in the same
   transaction — control unsettleable forever (ownership-drift check), agent
   locked out of its own run id (RunBindingError on its next tagged write),
   admin unable to re-issue (`run is not owned by an agent account`). Fixed:
   admission re-reads the binding after recording its event; a binding that is
   not the admitted agent aborts the whole transaction (control + event +
   binding all unwind). Regression test proves no rows survive and the agent
   keeps its run id.
2. **[minor] TTL absent from the idempotency identity** — a same-key retry with
   a different `ttl_seconds` silently replayed the original control. Fixed: the
   stored created→expires span joins `_control_matches_request`; mismatch → 409.
3. **[minor] Web form swallowed unreadable TTLs** (filter-style lenient parse on
   a write path) — `2h` became the silent default lifetime. Fixed: non-blank
   unparsable ttl → error redirect; surfaces now agree with the API's 422.
4. **[minor] `control_id ≥ 2^63` → 500 OverflowError** in the driver. Fixed:
   `Path(ge=1, le=2^63−1)` on all four id routes → 422. (The same latent issue
   exists in older resources, e.g. `/workers/{id}` — out of scope, noted as
   residual for the repo.)
5. **[minor] Configured default TTL could exceed the documented ceiling**
   (`_int_env` enforces only the floor). Fixed: `default_ttl_seconds()` clamps
   into 60–86400.
6. **[minor] RUN_CONTROLS.md overclaimed read authority** ("read and settle …
   write-scoped bearer") — reads actually require only the bound agent's
   identity. Fixed: docs now separate read authority from settlement authority.
7. **[major test gap] re-acknowledge idempotency untested** — now asserted
   (same timestamp, single receipt event).
8. **[major test gap] worker-targeted happy path untested** — new test covers
   targeting metadata round-trip and settlement.
9. **[minor test gaps] deterministic CAS boundary instants (at-expiry refuses,
   one second earlier lands, both from one injected creation clock); the 60s-TTL
   wall-clock race removed (1h TTL + injected expiry clock); the non-admin web
   refusal assertion can no longer vacate (`assert csrf is not None`); the
   both-summary-and-handoff refusal branch now covered.

---

## Delivery summary

**Branch**: `codex/buzz-run-steering-v1` — local commits only; nothing pushed,
no PR opened, per the session's authorization boundary.

**Starting SHA**: `37d6e410bd2c708dff5d43b6347d279da05ea512` (origin/main =
the research anchor; no drift). **Ending SHA**: the branch tip — the final
commit adds this report section on top of the review-fixes commit; the exact
hash is reported in the session handoff and visible via `git log`.

**Commits** (in order): phase 0 verification · phase 1 design · phase 2 domain
layer + migration 0070 · phase 3 REST · phase 4 MCP tools · phase 5 web panel ·
phase 6 docs · the 28-test contract suite · MCP mutation-contract registration ·
adversarial-review fixes · this report.

**Changed files**: `src/athena/core/{migrations/0070_run_controls.sql,
run_controls.py, run_control_commands.py, run_controls_api.py, activity.py (+1
read helper), agent_run_checkins.py (+1 read helper)}`, `src/athena/config.py`,
`src/athena/main.py` (router + idempotency-root wiring),
`src/athena/mcp/{client.py, server.py}`, `src/athena/web/activity.py`,
`src/athena/templates/aegis/run_lineage.html`, `docs/{RUN_CONTROLS.md,
RUNS.md, WORKERS.md, ROADMAP.md, VISION.md}`, `tests/{test_run_controls.py,
test_mcp_client.py}`, and this report. Zero new dependencies; zero coverage
pragmas; no edits outside the feature.

### Design decisions the prompt left open (resolved here)

1. **Run identity anchor**: `run_bindings.actor_id` when bound, else the run's
   SOLE check-in agent; ambiguity and unknown runs refuse. "Terminal runs" have
   no stored equivalent in Athena — mapped to the real nearest facts (paused
   owner, stopped targeted worker, expiry).
2. **Expiry is derived, never stored, never evented** — the worker-staleness
   doctrine applied to controls; no background sweeper, no clock in triggers
   (commands own expiry so tests can inject time).
3. **Worker binding is targeting metadata, not authority** — workers hold no
   credential of their own, so enforcement binds to the agent identity;
   documented explicitly.
4. **Settlement is bearer-only**; reads allow any live credential of the bound
   agent (it must be able to see its instructions from anywhere; only the
   process can answer).
5. **Reserved run namespaces (`automation:`, `icarus:`) refuse controls** — no
   principal polls them.
6. **No operator withdrawal in v1** — not in the prompt's closed lifecycle;
   short TTLs are the mitigation (worker-kill's cancel exists because kills
   never expire; controls do).
7. **`conflict`→409 and `capacity`→429** chosen where the repo disagrees with
   itself; domain idempotency keys minted server-side when omitted (icarus
   precedent) and carried in the body, with the transport header contract also
   available.
8. **Activity linkage**: control events target `run_control` ids; the
   operator's request never stamps the target run (that would forge/steal run
   authorship); the agent's settlement joins the run's replay when the agent
   tags its request — documented, tested.

### Limitations

- Heartbeat-only runs are steerable via REST/MCP but have no lineage page, so
  the web panel cannot show their controls.
- The web panel is create-and-observe; settlement is deliberately API/MCP-only.
- No fleet-attention rollup card for pending controls (EXCEPTION_SURFACES.md
  signal #8 candidate) — deferred as a follow-up to keep v1 minimal.
- Control activity events are visible to all authenticated feed readers (the
  repo-wide behavior for non-project-scoped events, same as worker/dispatch
  events); payloads and results are NOT in events — only kind + run id — and
  full rows are admin/bound-agent only.
- `capacity` ErrorKind is declared for parity with the house vocabulary but no
  cap currently triggers it (the per-(run,kind) liveness gate bounds live
  controls at 3 per run; total history is append-only like activity).

### Residual risks

- Two agents (or an agent and a human) can still both check into an unbound run
  id; admission refuses ambiguity, but a HUMAN-bound run refuses with `invalid`
  which reveals the run is human-owned to an admin (acceptable: creators are
  admins).
- The `/workers/{id}` and other pre-existing integer-id routes still 500 on
  ids ≥ 2^63 (the reviewer confirmed the same latent defect exists outside this
  feature's lane); fixed here only for `/run-controls`.
- Webhook subscribers configured for `event_kind=run_control` receive control
  lifecycle events (detail carries kind + run id only) — consistent with every
  other verb, but a deployment with third-party webhook endpoints should know.
- If a future feature adds run-binding deletion or transfer, the
  ownership-drift check's assumptions (bindings immutable once set) must be
  revisited.

