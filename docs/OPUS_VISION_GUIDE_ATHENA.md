# Opus Implementation Guide — Athena's Long-Term Vision

> **Audience.** This is a build sheet for the next principal engineer (Opus)
> continuing Athena toward *mission control for a one-person AI agent fleet*. It
> is implementation-ready, not a brainstorm. Every claim about current state is
> grounded in the code at the commit this guide lands on; every proposal names the
> exact repository paths, the command/API/MCP/web boundaries it must cross, and the
> tests and gates that prove it.
>
> **Read first, in this order:** [`docs/VISION.md`](VISION.md) (product north
> star + five steering rules), [`AGENTS.md`](../AGENTS.md) (the build contract —
> the cardinal rule and the command-ownership rule are non-negotiable),
> [`docs/ARCHITECTURE.md`](ARCHITECTURE.md), [`docs/COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md),
> [`docs/RUNS.md`](RUNS.md), [`docs/ACTIVE_WORK.md`](ACTIVE_WORK.md),
> [`docs/WORK_CONTEXT.md`](WORK_CONTEXT.md), [`docs/ROADMAP.md`](ROADMAP.md).
>
> **The five steering rules gate every task below.** (1) API/MCP-first — no
> capability ships without an MCP tool **and** a REST endpoint; the web page only
> supervises it. (2) Every agent action is attributable, reversible, and bounded.
> (3) The human steers by exception. (4) One operator, zero ops — one process, one
> SQLite file, one-command deploy. (5) Stay lean — leanness is the moat. If a
> proposed slice fails one of these, cut or reshape it.

---

## 1. Current-state assessment (shipped / partial / missing)

This is the honest ledger. "Shipped" means implemented **and** command-owned
where it is a durable write, with REST + web parity and (for agent-facing
capability) an MCP tool, plus tests. Citations are to files at this commit.

### Shipped

| Capability | Where | Notes |
|---|---|---|
| Issue core writes as commands | `aegis/issue_commands.py` | create; title/body/status/priority; assignee/project/sprint placement; parent hierarchy; archive/restore; labels; contributors/delegation; typed dependency link/unlink; blocked-close policy. Atomic mutation + audit under `db.transaction(immediate=True)`. |
| Comment writes as commands | `aegis/comment_commands.py`, `mentor/page_comment_commands.py` | create/edit/delete, FTS index + activity in one tx. |
| Page lifecycle as commands | `mentor/page_commands.py` | create/edit(If-Match)/move/hard-delete/version-restore/archive-restore, **and page label attach/detach** (this commit). |
| Space lifecycle as commands | `mentor/space_commands.py` | create/edit/hard-delete/visibility/membership, atomic. |
| Project lifecycle (partial) as commands | `aegis/project_commands.py` | create/edit/delete + blocked-close-policy config are commands. **Visibility/membership are not** (see §18, deferred F-3). |
| Agent identity + scoped tokens | `core/tokens.py`, `core/token_commands.py`, `core/identity.py` | scopes `read`/`issue:write`/`docs:write`/`admin`; explicit scopes required at mint; scope narrows role, never widens. |
| Fail-closed authn/authz + audited failures | `core/identity.py`, `core/security_events.py` | login-failed, revoked-token-used, scope-denied, paused-refused are recorded (but **surfaced nowhere** — deferred F-5). |
| Agent pause / kill switch / offboard | `core/agent_commands.py` | pause checked at identity resolution; token revocation; one-command offboard; all audited-atomic. Pause now also fences idempotency receipts (this commit). |
| Durable idempotency | `core/idempotency.py`, `main.py` `IdempotencyMiddleware` | single-flight claims, revocation + authorization-revision fencing, explicit indeterminate on split-commit. Receipt vs domain mutation are separate transactions **by design** (documented). |
| Issue + page ETags / If-Match | `aegis/issue_etags.py`, `mentor/page_etags.py` | strong validators; guarded edits and placement; claim acquisition/renewal. |
| Run identity + binding + lineage + replay | `core/run_context.py`, `core/activity.py`, `core/run_replay.py` | run id binds to first identity; parent/fork coordinates; `/activity/runs/{id}/{lineage,replay,fork}`; `athena-export-run`. Reserved `automation:` namespace now unforgeable from client headers (this commit). |
| Delegation claim/lease + generations + handoffs | `aegis/leases.py`, `aegis/lease_commands.py`, `aegis/claim_handoffs.py` | claim/renew/yield/decline/complete; possession-generation fencing; typed blocker handoffs with explicit resume. |
| Cooperative check-ins | `core/agent_run_checkins.py`, `core/agent_run_commands.py` | heartbeat with staleness; explicitly **not** process supervision. |
| Active-work projection | `aegis/fleet_work.py`, `fleet_work_api.py` | admin-only join of lease→run→check-in→blockers→replay with typed `attention_state`; web + REST + MCP. |
| Fleet throughput metrics | `aegis/fleet_metrics.py` | visibility-safe created-vs-completed, cycle median for full-visibility admins. |
| Automation (event + bounded UTC schedule) | `aegis/automation.py`, `automation_commands.py` | triggers → actions (assign/set_status/comment/add_label/add_contributor); per-firing run lineage; durable schedule receipts; rule-failure state. |
| Webhooks (SSRF-hardened) | `core/webhooks.py` | resolve-then-pin-IP connect, no-redirect, private-range block, HMAC signing; payload carries run/lineage. |
| OIDC SSO (optional) | `core/oidc.py`, `oidc_flow.py`, `oidc_commands.py` | all-or-none config; domain allow-list; link/unlink as commands. |
| Portability | `core/portability.py`, `ops.py` | selective export bundles, dry-run validation, manifest-gated import; excludes operational handoff rows. |
| Backup / restore / doctor | `core/backup.py`, `ops.py` | atomic restore with rollback; attachment reconciliation (detect, not auto-repair). |
| Packaging discipline | CI, `scripts/verify_wheel.py`, `scripts/smoke_app.py` | sdist→wheel→installed-boot-outside-checkout; runtime-data manifest pinned. |

### Partial

- **Project visibility/membership** — data-layer writes exist and are correct, but
  they are **not** command-owned: three separate commits, and a crash mid-flight
  leaves a permanent unaudited access-control change (deferred F-3).
- **Sprint + project-status lifecycle** — durable writes with **no audit event on
  any transport** and absent from the migration inventory (deferred F-4).
- **Human-in-the-loop gating** — ~~the only gate is the per-project blocked-close
  policy~~ — **closed** by the opt-in approval gates in `core/approvals.py`
  (migration 0063; see [`APPROVALS.md`](APPROVALS.md)). `issue.close` is the only
  gateable action kind, so this is a bounded slice rather than a general
  approval-request primitive (§5, §9).
- **Rate limiting** — per-token and per-anon-IP limits exist, but they are
  **in-process** (`core/rate_limits.py`), reset on restart, and not durable
  budgets (§5).
- **Reversibility** — ~~no general undo~~ — **partly closed** by undo by
  compensation (`core/undo.py`, migration 0064; see [`UNDO.md`](UNDO.md)). Four
  verb pairs are reversible. It is still not *general*: reversing `changed_status`
  or `assigned` needs the value in force beforehand, and `activity.detail` is
  human-readable prose, not structured before/after — see §6 for what that would
  actually take.
- **MCP parity** — write parity is good for issues/pages, but several shipped REST
  surfaces have **no MCP tool**: webhooks, saved filters, sprint lifecycle,
  attachments, watches, project/space create, event-feed verb/actor filters
  (deferred F-2). This directly violates steering rule 1.

### Missing (roadmap)

- ~~Durable **agent budgets and quotas**~~ — **shipped** as opt-in per-agent
  *action* budgets (`core/budgets.py`, migration 0062, REST + MCP + cockpit; see
  [`AGENT_BUDGETS.md`](AGENT_BUDGETS.md)). The open decision named in §18 was
  resolved deliberately: Athena meters **actions, not tokens/dollars**, because it
  never observes an agent's model spend and must not carry a cost column it cannot
  honestly populate. Token/cost metering stays additive, pending an external meter
  (Stage H). Wall-clock quotas remain unbuilt.
- ~~General **approval requests**~~ — **shipped** as opt-in per-actor approval
  gates (`core/approvals.py`, migration 0063, REST + MCP + cockpit; see
  [`APPROVALS.md`](APPROVALS.md)). The design decision this guide left open —
  deferred execution vs. gate-and-retry — was resolved as **gate + retry**: Athena
  never stores an agent's mutation and replays it later on the agent's behalf,
  because that re-executes a stale payload under authorization evaluated at a
  different moment. `issue.close` is the only gateable kind today; **dry-run
  preview remains unbuilt.**
- **Process-level pause/kill** (today's pause is a credential/identity freeze, not
  a signal to a running worker).
- ~~**Reversible commands / general undo**~~ — the compensating-action **model**
  shipped (registry, reversibility classes, `reverses_event_id`, re-evaluated
  authorization); the *coverage* did not. Widening it past state-free inverses
  needs structured prior state recorded at write time, not a bigger registry.
- ~~**Worker/node registration**~~ — **shipped** as a cooperative registry
  (`core/workers.py`, migration 0065; see [`WORKERS.md`](WORKERS.md)). Athena now
  models *where* an agent says it runs and can leave it a kill request. It still
  never models whether a process is alive: presence is `reporting_recently` or
  `stale`, and the kill is an instruction the worker collects and answers, so
  asked / acknowledged / stopped stay three separate facts.
- **Exception-driven push supervision** (attention state exists but there is no
  push alert; failures accumulate invisibly — F-5).
- **Memory/context feedback loop** (Mentor pages are playbooks agents read, but
  nothing writes run outcomes back into Mentor as durable learned context).
- **Athena↔Icarus integration** (§14): an async, API/MCP-based contract to
  dispatch work to an external execution fleet with no shared database.

---

## 2. Target architecture — the control plane for an autonomous fleet

Athena is the **control plane**, never the execution substrate. It owns *intent,
policy, identity, and evidence*; it does not run agent processes or hold model
weights. The autonomous loop it must fully close is:

```
Direct → Delegate → Observe → Intervene → Trust/Learn
  │         │          │          │            │
 issues    scoped     activity   pause/kill   undo + replay
 + docs    token +    trail +    + approve/   + context
 + accept  budget +   run        reject +     written back
 criteria  lease      lineage    budget cap   into Mentor
```

The invariant that makes this defensible: **the append-only `activity` log is the
source of truth, and every durable state change is a projection of a command that
wrote to it in one transaction.** Runs, leases, budgets, approvals, metrics, and
(future) undo are all *derived from or fenced by* that log. Nothing an agent does
escapes it.

Control-plane / data-plane split:

- **Control plane (Athena, this repo):** work items, docs, identities, tokens,
  policies (scopes/budgets/rate/approvals), leases, runs, activity, projections,
  webhooks. Single process, single SQLite file.
- **Data plane (external, e.g. Icarus):** the thing that actually edits a repo,
  runs a build, calls a model. Athena dispatches to it over an async API/MCP
  contract (§14) and receives evidence back. **No shared database. No circular
  package dependency.**

---

## 3. Module ownership and dependency direction

The enforced import graph is `web → (aegis | mentor) → core`, with `core` never
importing a feature or web module and nothing importing `main.py`. This is
machine-checked by `scripts/check_import_contracts.py` and must stay green.

| Layer | Path | Owns | Must not |
|---|---|---|---|
| core | `src/athena/core/` | db + migrations, auth/identity, users, tokens, activity, search, links, attachments, notifications, webhooks, idempotency, rate limits, run context/replay, portability, OIDC, **(new) budgets, approvals, undo ledger, worker registry** | import aegis/mentor/web/main |
| aegis | `src/athena/aegis/` | issues/projects/statuses/boards/sprints/labels/automation + **application commands** owning audited write transactions | import mentor or web |
| mentor | `src/athena/mentor/` | spaces/pages/versions/page-comments + its commands | import aegis or web |
| web | `src/athena/web/` | Jinja + HTMX thin client | own any data; bypass a command |
| mcp | `src/athena/mcp/` | thin client over REST for agents | mutate directly; add capability REST lacks |

**Placement rule for new cross-cutting primitives** (budgets, approvals, undo,
worker registry): they are *shared* concerns any module may reference, exactly
like `core/links` and `core/labels`, so they live in **`core/`** with
feature-specific policy composed in the feature command. A budget that caps issue
writes is enforced *inside* `aegis/issue_commands.py` by calling a
`core.budgets` predicate — the budget primitive is core, the enforcement point is
the command that owns the write.

---

## 4. The agent/job lifecycle and state machine

Today a "job" is implicit: an issue + a lease + a run id + activity. The target
model makes it **explicit and durable** without inventing a second work table —
it is a *projection over leases, runs, and the new policy/approval rows*.

Proposed lifecycle states for a unit of delegated work (a `job`, keyed by
`(issue_id, run_id)`):

```
                 ┌─────────────────────────────────────────────┐
                 ▼                                             │
 CREATED ─▶ DELEGATED ─▶ CLAIMED ─▶ RUNNING ─▶ AWAITING_APPROVAL ─┐
   │            │           │          │              │           │
   │            │           │          │              ▼           │
   │            │           │          │          APPROVED ───────┘
   │            │           │          │              │
   │            │           │          │              ▼
   │            │           │          ├──────────▶ COMPLETED
   │            │           │          ├──────────▶ YIELDED (handoff)
   │            │           │          ├──────────▶ FAILED
   │            │           │          └──────────▶ KILLED / BUDGET_EXCEEDED
   │            │           └──────────────────────▶ DECLINED
   └────────────┴─────────────────────────────────▶ CANCELLED
```

Mapping to existing primitives (do **not** duplicate them):

- `CREATED/DELEGATED` = issue exists + contributor/assignee set (`contributors.py`,
  `issue_commands.delegate`).
- `CLAIMED/RUNNING` = active lease + tagged run + fresh check-in (`leases.py`,
  `agent_run_checkins.py`, `fleet_work.py`).
- `AWAITING_APPROVAL` = **new** approval-request row bound to the run (§5, §9).
- `YIELDED` = open claim handoff (`claim_handoffs.py`).
- `KILLED/BUDGET_EXCEEDED` = **new** terminal facts stamped by pause/kill (§5) and
  the budget enforcer (§5).

Each transition is an **audited command** with a typed verb, so the job state
machine is reconstructable purely from `activity` (replay-safe). The projection
lives in `aegis/fleet_work.py` (extend, do not fork) and gains a `job_state`
field alongside `attention_state`.

---

## 5. Durable agent policies

A **policy** is the durable envelope a delegated agent runs inside. Today only
scopes, in-process rate limits, and pause exist. Target: one `core/policies`
primitive that the operator sets per-agent (and optionally per-project), stamped
with a **policy digest** that travels with every dispatched job for tamper-evident
enforcement.

| Policy | Today | Target | Enforcement point |
|---|---|---|---|
| **Scopes** | ✅ `tokens.py` (read/issue:write/docs:write/admin) | keep; add finer resource scopes only if a real need appears (stay lean) | `identity.require_token_scope` |
| **Budgets** | ❌ | durable counters per `(agent, window)`: action count, and an opaque "cost unit" the operator credits; decremented inside the command tx; refuses the write at zero | inside each command via `core.budgets.charge(conn, actor, cost, commit=False)` |
| **Rate limits** | ⚠️ in-process (`rate_limits.py`) | keep in-process for burst control; the durable budget above is the cross-restart bound | middleware + command |
| **Approvals** | ✅ opt-in gates (`approvals.py`, 0063) for `issue.close`, plus blocked-close | widen the action-kind vocabulary; the gate refuses and records a pending ask, and the operator's approval authorizes **one retry by that requester against that target** — never a stored, replayed payload | inside each gated command via `core.approvals.require(conn, actor, …)` |
| **Pause/kill** | ✅ credential freeze + cooperative kill request (`workers.py`, 0065) | keep both; a worker that ignores the request is surfaced (`acknowledged_but_reporting`), never force-stopped — Athena cannot signal a foreign process | identity + worker heartbeat contract |
| **Leases** | ✅ generation-fenced | keep; job state derives from them | `lease_commands.py` |
| **Retries / idempotency** | ✅ durable idempotency | keep; budgets must be charged **once per key** (charge on owner-claim, not on replay) | `IdempotencyMiddleware` + command |
| **Escalation** | ❌ | when a budget/approval/kill fires, emit a push notification + webhook + attention reason | notifications + webhooks + `fleet_work` |

**Budget design constraints (do not ship a fake column):**

1. Explicit domain type `core/budgets.py`: `Budget(agent_id, window, action_limit,
   cost_limit, action_used, cost_used, resets_at)`.
2. Durable schema `00XX_agent_budgets.sql`; charged/decremented **inside** the
   command's `db.transaction`, so the mutation and its budget debit commit or roll
   back together — never a separate commit (the same rule that governs audit).
3. Authorization: only an admin sets a budget; the command records a
   `budget_set` activity event; the agent may *read* its own budget via `whoami`.
4. Idempotency: a replayed keyed request must **not** double-charge; charge only on
   the owner-claim path (`main.py` `IdempotencyMiddleware`, `claim.kind == "owner"`),
   never on `replay`.
5. Failure: at zero budget the command raises a typed `BudgetExceeded` that maps to
   HTTP `429`/`402`-style with a stable `code`, records a `budget_exceeded`
   security event, and surfaces as an attention reason.
6. Tests: rejection at zero, no double-charge on retry, concurrent charge under
   `BEGIN IMMEDIATE`, attribution, and reset-window behavior.

---

## 6. A principled undo/reversal model that preserves audit history

Athena's log is append-only; **undo must never delete or rewrite history.** The
model is *compensation*, not deletion:

- Every command that is reversible declares a **compensating command** (its
  inverse). Reversibility class, borrowed from the operator's mental model:
  - **two-way** (create↔delete, label↔unlabel, assign↔unassign, archive↔restore):
    a direct inverse command exists.
  - **one-way** (a comment posted, a webhook delivered, an external side effect via
    Icarus): cannot be silently undone; undo records a *compensating annotation*
    and, where an external effect occurred, requires an explicit new forward action.
  - **trapdoor** (a token secret revealed, a destructive external deploy): no undo;
    the UI must refuse to offer one.
- An **undo request** names a target activity event id. The undo engine
  (`core/undo.py`) looks up the event's verb → its registered compensator, checks
  the actor may perform the inverse *now* (authorization is re-evaluated, not
  inherited), and runs the compensator as a **new** audited command whose event
  carries `reverses_event_id = <target>`. History gains two rows (original +
  reversal); nothing is erased.
- Replay integrity: because undo is a forward command, `run_replay` and lineage
  stay exact — a replay shows the action *and* its later reversal, which is the
  truthful record.

**Schema:** add `reverses_event_id INTEGER` to `activity` (migration), nullable,
FK to `activity.id`. **Registry:** a dict in `core/undo.py` mapping verb →
compensator callable + reversibility class. **REST/MCP:** `POST
/activity/{event_id}/undo` and MCP `undo_action(event_id)`; both fail closed for
one-way/trapdoor classes with a stable code. **Web:** an "Undo" affordance on
reversible activity rows only. **Tests:** each two-way pair round-trips; one-way
refuses; authorization re-checked; replay shows both rows.

---

## 7. Proposed schema and migration sequence

Migrations are **forward-only, strictly numbered, contiguous, checksum-bound**
(`core/db.py` runner; `docs/ARCHITECTURE.md`). The next free number at this commit
is **0062** (0061 was added this commit for the idempotency pause fence). Never
edit a shipped migration; add a new one.

Recommended sequence (each is one reviewable slice with its own PR):

| # | Migration | Adds | Unlocks |
|---|---|---|---|
| 0062 | `agent_budgets` | `agent_budgets` table + `budget_set`/`budget_exceeded` verbs are data, not schema | §5 budgets |
| 0063 | `approval_requests` | **shipped** as `agent_approval_policies(user_id, action_kind, …)` + `approval_requests(id, action_kind, target_kind, target_id, requested_by, run_id, state, decided_by, decided_at, decision_note, consumed_at, created_at)` with a partial unique index on the live intent. No `payload_json`: gate + retry stores an **intent**, never a replayable payload | §5/§9 approvals |
| 0064 | `activity_reverses` | `activity.reverses_event_id` | §6 undo |
| 0065 | `worker_registry` | **shipped** as `agent_workers(id, agent_id, worker_key, node_label, capabilities, first_seen_at, last_seen_at, last_token_id, kill_requested_at, kill_requested_by, kill_acknowledged_at, stopped_at)`, UNIQUE(agent_id, worker_key). Three kill columns, not one flag | §12 workers |
| 0066 | `project_visibility_membership_commands` | no schema change; a *code* migration porting F-3 to commands (may need an index) | close F-3 debt |
| 0067 | `sprint_status_audit` | no schema; code migration adding audit to sprint/status writes (F-4) | close F-4 debt |
| 0068 | `icarus_dispatch` | `icarus_dispatches(id, issue_id, run_id, parent_run_id, icarus_run_id, repo, base_commit, capability, policy_digest, approval_state, idempotency_key, evidence_ref, completion_ref, state)` | §14 integration |

Migration rules to honor for every one: add a matching authorization-revision
trigger to `idempotency_authorization_state` when the new table participates in an
access decision (see the pattern in `0043_durable_idempotency.sql` and the pause
fence added in `0061`), and add the new runtime file to the packaged-data manifest
checks if it is not a `.sql` under `core/migrations/` (those are already globbed).

---

## 8. REST and MCP contracts (example shapes)

Every durable capability gets **both** a REST endpoint and an MCP tool that reaches
the *same command*. Shapes below follow existing conventions (stable `code` on
errors, `Idempotency-Key` on mutations, `If-Match` where a precondition applies).

The first two shipped in Stages B and C; the sketches below are replaced by their
**as-built** contracts, which are the ones to code against —
[`AGENT_BUDGETS.md`](AGENT_BUDGETS.md) and [`APPROVALS.md`](APPROVALS.md) are
authoritative. The rest remain proposals.

**Budget — set (admin) and read (self) — as shipped:**

```
PUT    /users/{id}/budget          body {"window":"day","action_limit":500}
GET    /users/{id}/budget          (admin for anyone; any actor reads its OWN)
DELETE /users/{id}/budget          back to unlimited (idempotent)
  MCP: set_agent_budget / get_agent_budget / clear_agent_budget
  GET /users/me carries "budget" ; MCP whoami() likewise
```
Enforcement failure (any charged write at zero):
```
429 Retry-After: 3421
    {"detail":"agent budget exhausted: 50/50 metered actions used this day",
     "code":"agent_budget_exhausted","budget":{...}}
```
No `cost_limit`: Athena meters actions, never model spend it cannot observe.

**Approval — the ask is implicit (a gated command records and returns it),
the decision is explicit — as shipped:**

```
A gated command instead of mutating:
  202 {"detail":"issue.close requires operator approval",
       "code":"approval_required",
       "approval":{"id":7,"action_kind":"issue.close","target_kind":"issue",
                   "target_id":42,"run_id":"sol-1","state":"pending", ...}}
  Already rejected → 409 with "code":"approval_rejected" (an answer, not a delay)

POST /approvals/{id}/decision      (admin only)
  body {"decision":"approve"|"reject","note":"..."}
  → 200 {"id":7,"state":"approved","decided_by":1,"decided_at":"..."}
  MCP: decide_approval(id, decision, note)   (admin/human only)
GET  /approvals?state=pending       MCP: list_approvals(state)
PUT/DELETE /approvals/policies/{user_id}[/{action_kind}]
                                    MCP: set_approval_policy
  GET /users/me carries "approval_required" ; MCP whoami() likewise
```
No `If-Match` on the decision: only a `pending` request is decidable, so a
re-decide is a 409 on state rather than on a version. The approval authorizes the
requester's **retry**; there is no stored payload to replay.

**Undo — as shipped** ([`UNDO.md`](UNDO.md) is authoritative):

```
POST /activity/{event_id}/undo      Idempotency-Key optional
  → 201 {"reversed_event_id":880, "reversal":{...,"reverses_event_id":880}}
  → 422 {"detail":"'commented' is one_way: people have read it; …",
         "code":"undo_not_reversible"}
  Other codes: undo_event_not_found (404), undo_already_reversed (409),
  undo_imported_event (422), undo_no_effect (409),
  undo_refused_by_command (the command's own status)
  MCP: undo_action(event_id) ; GET /activity carries "reverses_event_id"
```

**Worker registry (§12):**

```
PUT /workers/heartbeat   body {"node_label":"box-1","capabilities":["repo.edit"]}
  → 200 {"worker_id":3,"kill_requested":false}
  MCP: worker_heartbeat(node_label, capabilities)
POST /workers/{id}/kill  (admin) → 200 {"kill_requested_at":"..."}
```

All new tools register in `mcp/server.py` (mutations via `@mutation_tool` so they
carry `Idempotency-Key` and structured errors) and `mcp/client.py`, and each must
appear in `tests/test_mcp_client.py`'s `MUTATION_CASES`/`MCP_MUTATION_CASES` (that
test enumerates the whole surface and will fail if a mutation tool lacks the
optional key — as it caught the two page-label tools added this commit).

---

## 9. Event and run-lineage model

Keep the existing model — it is a strength — and extend it:

- Runs are a **projection of `activity`** by shared `run_id`; there is no mutable
  runs table (`docs/RUNS.md`). A run binds to the first identity that writes it
  (`activity._bind_run`); cross-identity splicing raises `RunBindingError`.
- Lineage is `run_id` + `parent_run_id` + `forked_from_event_id`. Fork starts with
  a read-only contract request (`GET /activity/runs/{id}/fork`).
- **Reserved namespaces:** the server owns `automation:` (this commit) and should
  own an `icarus:` namespace for §14 the same way — client headers in a reserved
  namespace are dropped at the request edge (`run_context.set_client_run_id`), the
  engine/adapter mints them in-process.
- **Approval and budget events** join the lineage: `approval_requested`,
  `approval_approved`, `budget_exceeded`, `killed` are ordinary run-stamped
  activity rows, so replay reconstructs *why* a job stopped.
- **Known lineage gap to close (deferred F-6):** `parent_run_id` and
  `forked_from_event_id` are stored **unvalidated** — a client can name another
  actor's run as its parent. Fix inside `activity.record`: when `parent_run_id` is
  present, accept it only if it is bound to the same actor (or an actor that
  delegated to this one), else null the field (metadata semantics) — mirror the
  `run_bindings` check already used for `run_id`.

---

## 10. Operator cockpit requirements and attention semantics

The cockpit exists to let one human **steer by exception**. Current Mission
Control (`web/admin.py`, `templates/admin/agent_runs.html`,
`partials/fleet_active_work.html`) is honest and dense but has real gaps:

**Requirements:**

1. ~~**A needs-attention rollup on the landing surface.**~~ **SHIPPED** —
   `aegis/fleet_attention.py` builds it, the dashboard renders it admin-only, and
   `base.html` now links Mission Control and Security. Originally: Add an admin-only "Fleet attention" card: counts of
   `needs_attention` claims, failing automation rules, failing webhooks, and (new)
   pending approvals + budget breaches — each linking to its detail. This is the
   single highest-leverage usability fix (deferred F-7).
2. ~~**Surface security-failure signal.**~~ **SHIPPED** — `/security/events`,
   `/security/counts`, MCP `list_security_events`, and `/admin/security`.
   Originally: recorded but rendered nowhere (F-5). Add a bounded, admin-only "Security signals" panel + a REST
   endpoint + MCP `list_security_events(verb, since)` so probing-before-compromise
   is visible without knowing to grep the trail.
3. ~~**One failures surface, not three.**~~ **SHIPPED** as the rollup in (1): one
   card counting every exception surface, each linking to the page that owns it.
   The card deliberately computes nothing, so it cannot disagree with them.
4. ~~**Attention ordering must not clip attention rows.**~~ **SHIPPED** — both
   halves: attention-bearing rows now fill the bounded window from the urgent end,
   and `attention_state` filters the exact per-row state across web + REST + MCP.
   `examined_count` was added so "0 need attention" can no longer be misread as
   "none exist" on a clipped fleet.

**Attention semantics (keep exactly as designed in `docs/ACTIVE_WORK.md`):**
`needs_attention` is set only for recorded reasons; `observed` means "no known
reason applied at the snapshot", never "healthy/running". Do not infer completion
from quiet activity. New reasons to add as the primitives land:
`budget_exceeded`, `approval_pending`, `kill_requested`, `worker_stale`. Those are
counted in the rollup today but are **not** yet per-claim attention reasons — a
budget breach is a fleet signal, not a fact about one lease, and wiring them into
`_item` needs a claim→agent→policy join that does not exist yet.

---

## 11. Memory/context feedback loop through Mentor

The loop's fifth step (Trust/**Learn**) is the least built. Mentor pages are
playbooks agents *read* (`work_context.py` surfaces linked pages), but nothing
writes learned context *back*.

Target: a **durable, opt-in feedback command** that turns a run outcome into
Mentor knowledge, closing the loop:

- On job completion/yield, the operator (or an approved agent) can promote the
  run's summary — attempted work, evidence, resolution, the blocking question and
  its answer — into a Mentor page linked to the issue, via a command
  `mentor/page_commands.append_run_learning(...)` that creates or appends to a
  "Runbook: <issue>" page, atomic with a `page_learning_recorded` event.
- The write is a **page edit command** (already exists) plus a cross-link
  (`core/links`), so backlinks make it discoverable from both the issue and future
  work-context packets — the next agent reads it automatically.
- Guardrails: handoff/yield text is **untrusted advisory input**
  (`docs/ACTIVE_WORK.md`); promotion must sanitize (Mentor already renders through
  `nh3`) and must never auto-execute anything in it. Promotion is an explicit
  command, never automatic — the operator decides what becomes durable memory.
- MCP tool `record_run_learning(issue_ref, run_id, summary)` + REST
  `POST /issues/{ref}/learnings`; web affordance on the run replay view.

This is the "corrections feed back into Mentor as durable context the agents read
next time" clause of `docs/VISION.md`, made concrete.

---

## 12. Automation and worker/node architecture

**Automation** (`aegis/automation.py`) is mature: event + bounded-UTC-schedule
triggers, per-firing run lineage, durable schedule receipts, rule-failure state.
Remaining work: (a) route the `add_label`/`add_contributor` actions through
`issue_commands` with an automation policy flag (they still compose legacy
data-layer writes — deferred F-1a); (b) surface rule failures on the unified
failures panel (§10).

**Worker/node registration** is the biggest *missing* primitive and the bridge to
autonomy. Today Athena models *who* an agent is but never *where it runs* or
whether its process is alive (check-ins are cooperative self-reports, explicitly
not supervision — `docs/RUNS.md`). Target `core/workers.py`:

- A worker registers by heartbeat (`PUT /workers/heartbeat`), declaring a node
  label and capabilities. The registry row is durable; staleness is derived from
  server time exactly like check-ins.
- **Kill becomes actionable:** `POST /workers/{id}/kill` sets `kill_requested_at`;
  the worker learns of it on its next heartbeat response (`{"kill_requested":true}`)
  and is expected to stop. This is cooperative (Athena cannot signal a foreign OS
  process) — document that boundary exactly as the check-in doc documents its own.
- The worker registry is what an §14 Icarus adapter heartbeats into, giving the
  operator a live "which nodes are up, what can they do, which were told to stop"
  view — the missing half of Observe/Intervene.

Keep it lean: no cluster, no scheduler, no leader election (steering rule 4). A
worker is a row that heartbeats; that is all.

---

## 13. Security and privacy boundaries

Preserve every existing invariant; the following are the load-bearing ones the
next engineer must not weaken, plus the deferred security items.

**Must not weaken:** fail-closed authn/authz (`identity.py`); scopes narrow, never
widen, a role; visibility-gated reads with identical 404 for missing/hidden;
hidden rows never leak through counts/warnings/ETags (`work_context.py`,
`fleet_work.py`); SSRF-hardened webhooks (resolve-then-pin-IP, no redirect,
private-range block — `webhooks.py`); CSRF synchronizer tokens + login Origin
check (`web/csrf.py`, `web/auth.py`); idempotency revocation + authorization-
revision fencing (`idempotency.py`, now including pause — this commit); run
binding (`activity._bind_run`); reserved run-id namespaces (this commit);
migration checksum ledger (`db.py`); attachment no-follow download + path safety
(`attachments.py`).

**Deferred security items to schedule (from the review ledger, §18):**

- **F-6 lineage spoofing** (medium): validate `parent_run_id`/`forked_from_event_id`
  against `run_bindings` in `activity.record` (§9).
- **F-9 admin password reset unaudited** (medium): `web/admin.py`
  `update_user_password` does `set_password` then `revoke_all_sessions` in two
  commits with **no** audit event — a silent credential-takeover path. Port to a
  `core/user_commands.set_password` command that records a `password_reset` event
  (never the hash) atomically; add the missing REST endpoint for parity.
- **F-3 project visibility/membership** (medium): the three-commit
  access-control write can leave a **permanent unaudited** change on a crash. Port
  to `project_commands` mirroring the space-command pattern (which is already
  atomic).
- **New-primitive security:** budgets/approvals/undo/workers each add an authority
  surface — every one needs an authorization-revision trigger (idempotency fence),
  a fail-closed default, and adversarial tests for bypass.

**Privacy:** new projections (security panel, budgets, approvals) are admin-only
and must carry `Cache-Control: private, no-store` and vary on auth mechanism, like
the active-work and work-context surfaces.

---

## 14. Athena↔Icarus integration (async, API/MCP-based, no shared DB)

**Constraint (hard):** asynchronous, API/MCP-based, **no shared SQLite database,
no circular package dependency.** Athena is the control plane; Icarus is an
execution fleet. Neither imports the other; they communicate over HTTP/MCP with a
typed adapter contract and each keeps its own store.

### Adapter contract (the dispatch envelope)

The record Athena persists (`icarus_dispatches`, migration 0068) and transmits
carries exactly these fields — the required adapter contract:

| Field | Source | Meaning |
|---|---|---|
| `work_item_id` | Athena issue id | the Aegis work item being executed |
| `run_id` | Athena run | the control-plane run this dispatch belongs to |
| `parent_run_id` | Athena run | the run that spawned this dispatch (lineage) |
| `fork_run_ids` | Athena runs | child/fork runs derived from this dispatch |
| `icarus_run_id` | Icarus | the execution-side run id (opaque to Athena) |
| `repo` / `project` | Athena work item + policy | repository/project identity to act on |
| `base_commit` | dispatch | the commit the execution starts from |
| `capability` | policy | the requested capability (e.g. `repo.edit`, `ci.run`) |
| `policy_digest` | Athena policy | tamper-evident hash of the scopes/budget/approval state in force at dispatch |
| `approval_state` | Athena approvals | `not_required` / `pending` / `approved` / `rejected` |
| `idempotency_key` | Athena | so a re-dispatch is single-flight, reusing durable idempotency |
| `evidence_ref` | Icarus (async) | pointer to produced evidence (logs, diff, artifact) |
| `completion_ref` | Icarus (async) | pointer to the terminal result (PR URL, commit, failure) |

### Flow (asynchronous)

```
Athena                                   Icarus
  │  1. operator/agent delegates issue      │
  │  2. dispatch command:                    │
  │     - checks scopes+budget+approval      │
  │     - mints icarus:<run> reserved run    │
  │     - writes icarus_dispatches row       │
  │     - POST /dispatch (envelope) ────────▶│  3. accepts, starts execution
  │  ◀───── 202 {icarus_run_id} ────────────│     (its own store, its own runs)
  │  4. record dispatch_accepted event       │
  │                                          │  5. progress → webhook/callback
  │  ◀── POST /callbacks/icarus (signed) ────│     {icarus_run_id, evidence_ref}
  │  6. verify signature + idempotency key    │
  │     map icarus_run_id→dispatch            │
  │     record evidence as run-stamped        │
  │     activity (control-plane truth)        │
  │  ◀── POST /callbacks/icarus completion ──│  7. terminal: completion_ref
  │  8. record job terminal state; if the     │
  │     capability was one-way, no auto-undo  │
```

### Design rules

- **Dispatch is a command** (`aegis/icarus_commands.py` or `core/dispatch.py`):
  authorization, budget charge, approval check, envelope build, and the
  `dispatch_requested` activity event commit in one transaction *before* the
  outbound call; the outbound HTTP is a post-commit side effect (like webhook
  delivery) whose result is recorded as a follow-up event — never inside the tx.
- **Callbacks are authenticated and idempotent:** Icarus posts back with an HMAC
  signature (reuse `webhooks.sign`) and the dispatch `idempotency_key`; the handler
  verifies both, maps `icarus_run_id → dispatch`, and records evidence as
  control-plane activity. A replayed callback is a no-op.
- **No shared DB:** Athena never reads Icarus's database and vice-versa. Evidence
  is *referenced* (`evidence_ref`/`completion_ref` are opaque URLs/ids), not copied
  transactionally. The two systems reconcile through the async contract only.
- **No circular dependency:** Athena's `pyproject.toml` gains no Icarus dependency;
  the adapter speaks HTTP/MCP. If a Python client is wanted, it is a *thin* client
  package like `mcp/client.py`, not an import of Icarus internals.
- **Reserved `icarus:` run namespace** (mirror the `automation:` reservation) so a
  client cannot forge an execution-run stamp.
- **Policy digest** is verified on callback: if the digest that returns does not
  match the digest dispatched, the evidence is recorded but flagged
  `policy_digest_mismatch` (tamper-evident), never silently trusted.

---

## 15. Staged implementation roadmap (with exact paths)

Each stage is independently shippable, gated, and mergeable. Order optimizes for
closing the loop and paying down the highest-severity confirmed debt first.

**Stage A — Close confirmed command/security debt (no new surface). — SHIPPED.**
- F-9 password-reset command: `core/user_commands.py` (+ `password_reset` event),
  `web/admin.py`, new `core/users_api.py` endpoint, `tests/test_password_*`.
- F-3 project visibility/membership commands: `aegis/project_commands.py`,
  `aegis/projects.py` (add `commit=` kwargs), `core/access.py` (add `commit=`),
  `aegis/api.py`, `web/projects.py`, `tests/test_project_privacy_ui.py` (+ rollback
  test).
- F-6 lineage validation: `core/activity.py` `record`, `tests/test_run_binding.py`.
- F-4 sprint/status audit: `aegis/sprint_commands.py` (new), `aegis/sprints_api.py`,
  add to `COMMAND_MIGRATION.md`.

**Stage B — Budgets. — SHIPPED** (`docs/AGENT_BUDGETS.md`). `core/budgets.py`,
`0062_agent_budgets.sql`, charge points in `aegis/issue_commands.py` +
`mentor/page_commands.py`, `PUT /users/{id}/budget` (not `/agents/…`), MCP
`set_agent_budget` + `whoami` extension, `web/admin.py` panel,
`tests/test_agent_budgets.py`. Actions only — no cost dimension.

**Stage C — Approvals + human-in-the-loop. — SHIPPED** (`docs/APPROVALS.md`).
`core/approvals.py`, `0063_approval_requests.sql`, gate in
`aegis/issue_commands.py`, `POST /approvals/{id}/decision` + policy routes, MCP
`list_approvals`/`decide_approval`/`set_approval_policy`, cockpit
pending-approvals card, `tests/test_approvals.py`. Gate + retry, single-use, one
action kind (`issue.close`); **dry-run preview was not built** and remains open.

**Stage D — Undo. — SHIPPED** (`docs/UNDO.md`). `core/undo.py` + compensator
registry, `0064_activity_reverses.sql`, `POST /activity/{id}/undo`, MCP
`undo_action`, feed affordance, `tests/test_undo.py`. The registry is populated by
`aegis/issue_undo.py` and `mentor/page_undo.py` and wired from `main.py`, because
`core` may not import the domain layers — the guide's "a dict in `core/undo.py`"
would have violated the import contract. Note the asymmetry the Mentor
compensators must carry: `page_commands` leaves visibility and scope to its
transport boundary, so undo re-applies both itself or it becomes an escalation.

**Stage E — Worker registry + actionable kill. — SHIPPED** (`docs/WORKERS.md`).
`core/workers.py` + `core/worker_commands.py`, `0065_agent_workers.sql`,
`PUT /workers/heartbeat` and the kill routes, MCP `worker_heartbeat` /
`list_workers` / `request_worker_kill` / `cancel_worker_kill`, cockpit node view,
`tests/test_workers.py`. The kill signal is a **row the worker polls on its next
heartbeat**, exactly as this guide proposed — and the schema keeps asked,
acknowledged, and stopped apart, because collapsing them into a `killed` flag is
the one lie the feature exists to prevent.

**Stage F — Cockpit exception surfaces (F-5/F-7/F-8). — SHIPPED**
(`docs/EXCEPTION_SURFACES.md`). Security-signals panel + `/security/events` +
`/security/counts` + MCP `list_security_events`; the dashboard fleet-attention
rollup (which *is* the unified failures view — one card counting claims,
approvals, unanswered kills, automation, webhooks, budget breaches, and refusals,
each linking to the surface that owns it); `attention_state` filter on
`fleet_work` across web + REST + MCP. F-8 was the real bug: the window sorted
active-before-expired, so attention rows were the ones the limit dropped while the
summary could read "0 need attention". Rows now fill the window from the urgent end,
and `examined_count` distinguishes "none do" from "none of the ones we looked at
do".

**Stage G — Memory feedback loop. — SHIPPED** (`docs/RUN_LEARNINGS.md`). Landed
as `mentor/run_learnings.py` rather than a method on `page_commands` (it needs
link resolution, run validation, and two visibility checks — its own concern), plus
migration `0066_issue_runbooks.sql` binding one runbook page per issue,
`POST /issues/{id}/learnings` + `GET /issues/{id}/runbook`, MCP
`record_run_learning` / `get_issue_runbook`, a form on the run lineage view, and
`tests/test_run_learnings.py`. Two things the sketch did not say: the runbook
binding is a ROW, not a title lookup, so a rename cannot silently fork the memory
in two; and promoted text is stored **blockquoted** under an attribution header
Athena writes, so an untrusted summary cannot forge a second attribution beside
the real one. The REST route lives in Mentor while its path names an issue,
because the import contract makes Aegis and Mentor peers — documented in the
module.

**Stage H — Icarus integration. — SHIPPED** (`docs/DISPATCH.md`). Both modules,
as it turned out: `core/dispatch.py` owns the record, the digest, and the envelope;
`aegis/icarus_commands.py` owns the command and delivery. The migration is
**0067**, not 0068 — migrations must be contiguous and the code-only 0066/0067 this
table imagined never materialized. Plus the `icarus:` reservation in
`run_context.py`, `POST /issues/{id}/dispatch`, the HMAC-authenticated idempotent
callback, MCP `dispatch_to_icarus` / `list_dispatches`, and
`tests/test_icarus_dispatch.py`.

Three things the sketch did not say. The outbound call is a **post-commit side
effect** — a network call inside a transaction would hold SQLite's single writer
for as long as a stranger's server takes. Dispatch is **metered and gated** like
any other write, because otherwise an actor gated on `issue.close` could route
around the gate by asking an executor instead. And there is deliberately **no
cockpit view**: `GET /dispatches` and the activity trail carry it, and a page
whose only content is "what Athena was told" would invite reading it as fleet
status. Dispatch is **off unless configured**, and refuses with a 503 rather than
accumulating undeliverable rows.

**MCP parity backfill (F-2)** runs alongside every stage: any REST surface a stage
touches gets its MCP tool in the same PR (webhooks, filters, sprints, attachments,
watches, project/space create, event-feed filters).

---

## 16. Tests, evals, smoke checks, and release gates per stage

Every stage must keep the full local gate green (from `README.md` / `CONTRIBUTING.md`):

```
pip check ; freeze diff ; ruff check ; ruff format --check ;
mypy src/athena ; scripts/check_import_contracts.py ;
scripts/coverage.sh <fresh-dir> ; sdist→wheel→installed-boot smoke
```

Per-stage additional bars:

- **Command tests** (every durable write): success, rejection, **rollback when the
  audit recorder fails** (the pattern in `test_page_label_command_migration.py` and
  `test_issue_association_command_migration.py`), attribution, idempotence, and
  **transport parity** (REST == web == MCP reach the same command).
- **Concurrency tests**: `BEGIN IMMEDIATE` serialization for budgets/approvals/leases
  (the suite is Barrier-coordinated and deterministic — no `time.sleep`).
- **Authorization tests**: fail-closed default, scope required, admin-only surfaces,
  and *adversarial* bypass attempts (forged flags, wrong actor, reserved-namespace
  forgery — mirror `test_automation_run_id_reservation.py`).
- **Idempotency tests**: no double-charge/double-effect on replay; revocation and
  authorization-revision (incl. pause, this commit) fence stored responses.
- **Coverage**: keep line ≥ `pyproject.toml` `line_floor`, branch ≥ `branch_floor`,
  combined ≥ `combined_floor`; new code should not lower observed coverage.
- **Evals for the loop** (new): a scripted end-to-end that delegates → claims →
  hits a budget → requests approval → is approved → completes → undoes → promotes a
  learning, asserting the activity trail replays the whole story. This is the
  regression test for "the loop actually closes."

---

## 17. Migration and rollback strategy

- **Forward-only migrations** with checksum ledger (`core/db.py`): a new migration
  is additive; never edit a shipped one. `/readyz` and `athena-doctor` validate the
  contiguous, checksum-bound ledger and fail closed on tamper.
- **Backfill trust:** a legacy DB first backfilling checksums must trust the
  installed package — preserve a trusted package/archive and a matched pre-upgrade
  recovery pair (`docs/RELEASE_READINESS.md`).
- **Code migrations (F-3/F-4)** that add no schema still ship behind their own PR
  with rollback = revert the command wiring; because the data layer is unchanged,
  reverting cannot corrupt rows.
- **New-table rollback:** because Athena is single-file SQLite, rollback is a
  matched backup restore (`athena-restore`, atomic with rollback-on-double-failure).
  A dropped feature leaves an unused table — harmless; do **not** write a
  down-migration (forward-only is the contract).
- **Dispatch/undo/approval rows** are portability-sensitive: like operational
  handoff rows, decide explicitly whether selective export includes them
  (`portability.py`); default to **excluding** operational state so an imported
  bundle can never create actionable approvals/dispatches (mirror the handoff
  exclusion).

---

## 18. Open product decisions and residual risks (the findings ledger)

The Phase-2 review produced this ledger; verified findings are marked. Severity is
operator impact for a single-operator local/tailnet deployment. **Fixed this
commit** items are done; the rest are the deferred backlog Opus should schedule via
§15.

| ID | Sev | Invariant | File:symbol | Status |
|---|---|---|---|---|
| **P-1** | med | one command owns each write (page labels) | `core/labels.py` add/remove_label_to_page; `mentor/api.py`, `web/mentor.py` | **FIXED** — `page_commands.attach/detach_page_label`, REST/web/MCP parity, tests |
| **P-2** | med | fail-closed authz; pause freezes every authenticated action | `main.py` `IdempotencyMiddleware`; `0043`/`0049` | **FIXED** — paused keyed requests skip replay + `0061` fences receipts, tests (verified independently by review) |
| **P-3** | med | run identity / replay integrity | `aegis/automation.py` `_already_fired`/`automation_run_id`; `run_context.py` | **FIXED** — `automation:` namespace reserved on client header path, tests (CONFIRMED by adversarial verify) |
| F-1a | info | command ownership (automation actions) | `aegis/automation.py` `_perform_action` add_label/add_contributor | deferred — route through `issue_commands` w/ automation policy |
| F-2 | med | steering rule 1 (MCP parity) | `mcp/server.py` surface | deferred — webhooks, filters, sprints, attachments, watches, project/space create, event filters have no MCP tool |
| F-3 | med | command ownership + audit atomicity | `aegis/api.py` set_project_visibility/add/remove_member; `aegis/projects.py`; `core/access.py` | deferred — three-commit access-control write, permanent unaudited change on crash (CONFIRMED) |
| F-4 | med | durable writes are audited | `aegis/sprints_api.py`, `aegis/statuses.py` | deferred — sprint/status lifecycle has no audit event on any transport |
| F-5 | med | steer by exception | `core/security_events.py`; `web/`, `templates/` | deferred — recorded security failures surfaced nowhere |
| F-6 | med | run lineage attribution | `core/activity.py` `record` (parent/fork unvalidated) | deferred — cross-actor lineage spoofing |
| F-7 | med | cockpit surfaces decisions | `web/router.py` `aegis_dashboard`; `base.html` | deferred — no needs-attention rollup on landing surface |
| F-8 | low | attention semantics | `aegis/fleet_work.py` ORDER BY + no attention filter | deferred — expired (attention) claims clip first on busy fleets |
| F-9 | med | privilege writes are audited-atomic | `web/admin.py` `update_user_password` | deferred — admin password reset unaudited, non-atomic, no REST parity (CONFIRMED) |
| F-10 | med | agent check-in cap has a purge path | `core/agent_run_commands.py` heartbeat cap | deferred — permanent dead-end at `MAX_CHECKINS_PER_AGENT`, no retention/purge |
| F-11 | low | run-id canonicalization consistency | `core/run_context.py` `normalize` vs `strict_run_id` | deferred — NFC divergence → false `checkin_missing`; and >200-char header ending in space can 500 a tagged write |
| F-12 | low | in-transaction actor liveness | `core/identity.py` `_refuse_paused` | deferred — one-request window: an in-flight request commits after pause lands (no command re-checks `paused_at` in-tx) |
| F-13 | low | MCP error ergonomics | `mcp/server.py` read tools | **FIXED** — every registered read tool now preserves parsed `Retry-After`/`code`/`current_etag` fields in `ATHENA_ERROR_JSON`, matching mutation tools |

**Open product decisions (need an owner, not just code):**

- **Budget unit:** is the "cost" an abstract action-credit the operator sets, or a
  token/dollar figure fed from an external meter? (Affects §5 schema.)
- **Approval scope:** which action kinds are approval-gated by default vs opt-in
  per policy? (Affects §5/§9.)
- **Undo of one-way/external effects:** does undo of an Icarus-dispatched change
  trigger a *new* compensating dispatch, or only annotate? (Affects §6/§14.)
- **Worker trust:** does a worker heartbeat require its own scoped token, and can a
  worker act on behalf of multiple agent identities? (Affects §12 authz.)
- **Icarus authority:** does Icarus ever mint Athena work items, or only execute
  and report? (Guide assumes execute-and-report to keep the dependency acyclic.)

**Residual risks:** in-process rate limits reset on restart; idempotency receipt
and domain mutation remain separate transactions (indeterminate outcomes are
explicit, never auto-taken-over); attachment reconciliation detects but does not
repair; single-process/single-worker is the only supported shape (no HA, no leader
election) — keep it that way (steering rule 4).

---

## 19. The first ten implementation tasks for Opus

Ordered. Each is one PR on a `fable/`- or `claude/`-style branch, green through the
full gate, with the tests named. Tasks 1–4 pay down confirmed medium-severity debt
before any new surface (correctness/security first); 5–10 build the loop.

1. **F-9 — audited password-reset command.** `core/user_commands.set_password`
   (records `password_reset`, revokes sessions, one tx; never logs the hash),
   rewire `web/admin.py` `update_user_password` + self-service, add REST parity in
   `core/users_api.py`. Tests: attribution, atomic rollback, session-revocation,
   no-hash-in-trail.
2. **F-3 — project visibility/membership commands.** `aegis/project_commands.py`
   trio mirroring `space_commands`; add `commit=` kwargs to `aegis/projects.set_visibility`
   and `core/access` member helpers; rewire `aegis/api.py` + `web/projects.py`.
   Tests: atomic mutation+audit, crash-rollback, REST/web parity,
   `test_project_privacy_ui.py`.
3. **F-6 — validate run lineage.** In `core/activity.record`, check `parent_run_id`
   against `run_bindings` (same-actor or delegated); null it otherwise. Tests in
   `test_run_binding.py`: cross-actor parent rejected, legitimate fork preserved.
4. **F-4 — sprint/status audit commands.** `aegis/sprint_commands.py` recording
   `sprint_created/started/completed/deleted`; add to `COMMAND_MIGRATION.md`. Tests:
   audit present on every transport, parity.
5. **Budgets (Stage B).** `core/budgets.py` + `0062_agent_budgets.sql`; charge in
   issue/page commands; `PUT /agents/{id}/budget` + MCP `set_agent_budget` +
   `whoami` budget; cockpit panel. Tests: reject-at-zero, no-double-charge-on-retry,
   concurrent charge, attribution.
6. **F-5 + F-7 — cockpit exception surfaces.** `list_security_events` (REST+MCP) +
   admin security panel; dashboard fleet-attention rollup with a Mission Control
   link in `base.html`. Tests: visibility-gated, private cache headers, parity.
7. **Approvals (Stage C).** `core/approvals.py` + `0063`; flag hook returning
   `approval_required`; `POST /approvals/{id}/decision` + MCP; cockpit card. Tests:
   pending-blocks-mutation, approve-releases, reject-terminates, human-only decision.
8. **Undo (Stage D).** `core/undo.py` compensator registry + `0064_activity_reverses.sql`;
   `POST /activity/{id}/undo` + MCP `undo_action`; web affordance on reversible rows.
   Tests: two-way round-trip, one-way refusal, authorization re-check, replay shows
   both rows.
9. **Worker registry (Stage E).** `core/workers.py` + `0065`; heartbeat/kill
   endpoints + MCP tools; cockpit node view; add `worker_stale`/`kill_requested`
   attention reasons. Tests: heartbeat/staleness, cooperative kill signal, authz.
10. **Icarus dispatch (Stage H, first slice).** `core/dispatch.py` +
    `0068_icarus_dispatch.sql` + `icarus:` reserved namespace; dispatch command
    (authz+budget+approval+envelope+event in one tx, outbound call post-commit) and
    a signed, idempotent callback endpoint recording evidence as run-stamped
    activity. Tests: dispatch envelope shape (all §14 fields), signed-callback
    idempotency, policy-digest-mismatch flagging, no shared-DB / acyclic-dependency
    (import-contract check).

---

*Grounding note: every "current state" claim in this guide was verified against the
code at the commit it lands on. The three improvements shipped alongside it
(page-label command migration, idempotency pause fence, automation run-id namespace
reservation) are the worked examples of the command-ownership, fail-closed-authz,
and run-integrity patterns every task above must follow.*
