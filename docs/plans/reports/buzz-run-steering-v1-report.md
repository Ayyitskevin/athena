# Run Report — Buzz Run Steering v1

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

### Phase status log

- Phase 0 (architecture verification): **COMPLETE** (this section)
- Phase 1 (design): pending
- Phase 2 (domain/command layer + migration): pending
- Phase 3 (REST): pending
- Phase 4 (MCP): pending
- Phase 5 (web panel): pending
- Phase 6 (docs): pending
- Phase 7 (validation + final review): pending
