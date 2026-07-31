# Dispatching work to an external executor

Athena is a **control plane**. An execution fleet (Icarus) is a separate system
with its own store, its own runs, and its own idea of progress. They share no
database and neither imports the other: they reconcile over an asynchronous HTTP
contract, and `icarus_dispatches` (migration 0067) is Athena's half of it.

Dispatch is **off unless configured**. With `ATHENA_ICARUS_URL` and
`ATHENA_ICARUS_SECRET` unset — the default — every dispatch is refused with a 503
saying so. Half-working is worse than absent.

## What Athena knows, and what it was told

This is the whole design, and it is why the state names read the way they do.

| State | Means |
|---|---|
| `pending_delivery` | Athena recorded the decision; the executor has not acknowledged |
| `accepted` | The executor **said** it accepted, and gave its own run id |
| `undeliverable` | Athena could not hand it over (with the reason) |
| `completed` / `failed` | The executor **reported** a terminal outcome |

`accepted` does not mean work is running. Athena cannot see the far side and does
not pretend to — the same discipline `WORKERS.md` applies to a heartbeat and
`ACTIVE_WORK.md` applies to a check-in.

**Evidence is referenced, never copied.** `evidence_ref` and `completion_ref` are
opaque strings the executor chose. Copying artifacts across the boundary would make
one system's storage the other's problem, which is precisely what "no shared
database" rules out.

Callback v1 has **one immutable, canonical `evidence_ref` per dispatch**. The
first non-null value wins, an exact retry is a no-op, and a different value while
the dispatch is open is a 409 conflict. Athena cannot call one different pointer
"newer" because v1 carries no sender sequence. Once a dispatch is terminal,
outcome changes and evidence overwrites are absorbed. A legacy terminal callback
that omitted evidence may still have its one null evidence slot filled by delayed
progress; after that, it is immutable. A terminal callback should repeat the
canonical evidence pointer when one exists: then progress-before-terminal and
terminal-before-progress converge to the same row and the same two audit events.

## The envelope

Exactly these fields cross the boundary — there is no free-form payload, because an
envelope a reader cannot enumerate is one nobody can audit.

```json
{
  "schema": "athena.icarus_dispatch.v1",
  "dispatch_id": 12, "work_item_id": 42,
  "run_id": "icarus:9f3c…", "parent_run_id": "sol-7", "fork_run_ids": [],
  "icarus_run_id": null,
  "repo": "git@example.com:acme/app.git", "base_commit": "abc123",
  "capability": "repo.edit",
  "policy_digest": "5e884898…", "approval_state": "not_required",
  "idempotency_key": "9f3c…",
  "evidence_ref": null, "completion_ref": null
}
```

`fork_run_ids` is **derived** from the activity trail (runs whose `parent_run_id`
is this dispatch's run) rather than stored. Lineage already lives in one place, and
a second copy is one more thing that can disagree with the log.

## The flow

```text
Athena                                        Executor
  │ 1. command: role, scope, visibility,        │
  │    budget charge, approval gate,            │
  │    policy digest, dispatch row,             │
  │    dispatch_requested event — ONE TX        │
  │ ───── 2. POST /dispatch (signed) ─────────▶ │ 3. accepts
  │ ◀──── {"icarus_run_id": "..."} ──────────── │
  │ 4. state=accepted, dispatch_accepted event  │
  │ ◀──── 5. POST /callbacks/icarus (signed) ── │    progress: evidence_ref
  │ 6. verify HMAC, map run, check digest,      │
  │    record evidence as run-stamped activity  │
  │ ◀──── 7. POST /callbacks/icarus (signed) ── │    terminal: evidence_ref + outcome
  │ 8. state=completed|failed                   │
```

**Step 2 is a post-commit side effect.** Athena holds SQLite's single writer while
a transaction is open, so a network call inside one would block every other writer
for as long as a stranger's server feels like taking. It is also wrong on its own
terms: the durable fact is "Athena decided to dispatch this", and that fact must
survive a far side that never answers. A dispatch nobody could deliver stays
visible as `undeliverable` with the reason.

## Authorization, in both directions

**Outbound** is an ordinary authenticated write: role, `issue:write` scope, issue
visibility, a **budget charge**, and any **approval gate** — under dispatch's own
action kind, `dispatch.request`. It briefly borrowed `issue.close`'s policy row,
which conflated two intents the operator decides separately (and let a close
approval be spent by a dispatch); each is now its own gate, and an operator who
wants both gated sets both. The gate is consumed inside the same transaction, so a
later failure leaves it unspent.

**Inbound has no Athena credential at all.** The executor is not an Athena user and
holds no token; it authenticates with an HMAC over the exact request body using the
shared secret, compared in constant time. Each attempt first charges the shared
anonymous direct-peer-IP limiter (when configured), then verifies the signature
over the raw bounded body **before JSON parsing, a SQLite connection, or any
dispatch lookup**. Unsigned malformed JSON is therefore the same 401 as any other
bad signature, and a signature containing non-ASCII bytes is a failed credential,
not a process error. The route cannot be used to probe which dispatches exist.
It is deliberately narrow: it can attach evidence and report an outcome on a
dispatch Athena already created. It cannot create work, change an issue, or name
an actor.

## The policy digest is tamper-evident, not tamper-proof

The digest is a SHA-256 over the authorization state in force at dispatch: the
actor, its token scopes, the work item, repo, base commit, capability, approval
state, and budget window/limit. Scope order does not affect it — a digest that
depended on ordering would produce false mismatches.

When a dispatch first receives a callback with a **different** digest, Athena
records **one** `dispatch_policy_digest_mismatch` event for that dispatch run,
even if the callback arrives after a terminal report. It does not discard an
otherwise admissible evidence pointer: destroying the evidence would defeat
exactly the thing the digest was computed to produce. Replaying the same report
does not spam the trail. An authenticated report whose evidence pointer conflicts
still commits its first mismatch warning before returning 409. A syntactically
valid non-ASCII digest is a safely labelled mismatch, not a 500. A digest exists
to let you notice.

## Reserved run namespace

Athena mints an `icarus:<key>` run per dispatch, and that prefix is **reserved**
alongside `automation:` — a client sending `X-Athena-Run: icarus:anything` has the
value dropped rather than honored. Otherwise anyone could forge control-plane
evidence of what an executor did. Callback events are stamped with the dispatch's
run by Athena itself, so the executor's reports appear in that run's replay and
lineage.

## Egress safety

The outbound call reuses `core/webhooks`' SSRF hardening **in full**: URL scheme
validation, rejection of private/loopback/link-local/reserved addresses,
DNS-pinned connections so a rebind cannot redirect the request, no redirect
following, and HMAC signing. A control plane that can be made to POST anywhere is a
control plane that can be turned into a probe.

## Surfaces

```text
POST /issues/{id}/dispatch    {"repo": "...", "base_commit": "...", "capability": "repo.edit"}
GET  /dispatches?work_item_id=42&state=accepted
GET  /dispatches/{id}
POST /callbacks/icarus        # HMAC-signed, no Athena credential
dispatch_to_icarus(issue_id, repo, base_commit, capability)
list_dispatches(work_item_id=None, state=None)
```

Dispatch reads require an Athena identity and inherit the referenced issue's
visibility. A hidden dispatch and a missing dispatch both return `404`; list filters
visibility in SQLite before ordering and limiting, so private rows neither leak
their metadata nor consume an outsider's bounded page. The REST rule automatically
governs `list_dispatches` over MCP because the MCP client uses this API rather than a
parallel data path. Bearer tokens need `read` or `issue:write`; `docs:write` alone
does not cross the Aegis boundary.

Capabilities are a **closed set** (`repo.edit`, `ci.run`). An open one would mean
Athena forwarding capability names it has never heard of and cannot reason about.

## Configuration

| Variable | Default | Means |
|---|---|---|
| `ATHENA_ICARUS_URL` | *(unset)* | Executor base URL; `POST {URL}/dispatch` |
| `ATHENA_ICARUS_SECRET` | *(unset)* | Shared HMAC secret, both directions |
| `ATHENA_ICARUS_TIMEOUT_SECONDS` | 10 | Per-request outbound timeout |
| `ATHENA_EGRESS_PRIVATE_HOSTS` | *(empty)* | Exact hostnames Athena may POST to even though they resolve private/loopback (see below) |
| `ATHENA_ANON_RATE_LIMIT_PER_MINUTE` | `0` (off) | Shared direct-peer-IP limit for anonymous paths, including every callback attempt |

Both the URL and the secret must be set. A URL without a secret would mean sending
unsigned work to an unauthenticated endpoint.

**An executor on your own machine, LAN, or tailnet needs one more line.** The
SSRF guard that protects webhook delivery also guards dispatch, and it refuses
any host that resolves to a private, loopback, or link-local address — so
without help, `ATHENA_ICARUS_URL=http://127.0.0.1:8443` makes every dispatch
land `undeliverable: url resolves to a disallowed (internal) address`. That is
the guard doing its job against attacker-registered URLs; your executor's
address is not attacker-registered, so name it explicitly:

```
ATHENA_EGRESS_PRIVATE_HOSTS=127.0.0.1
```

The list is exact hostnames (comma-separated, case-insensitive, no wildcards),
set in the process environment — the same trust channel as the secret itself.
Delivery still pins the connection to the resolved address. This was found the
first time the dispatch loop ran against a real local counterparty, which is
what `scripts/field_exercise.py` now does in CI.

## The reference executor

[`examples/icarus_executor.py`](../examples/icarus_executor.py) is the smallest
honest counterparty to this contract: one stdlib-only file (a test pins that it
imports nothing from Athena — the two systems share a secret and a wire format,
never code) that verifies the envelope signature before parsing a byte, answers
acceptance with its own run id, echoes the policy digest verbatim, signs its
callbacks the same way, repeats its canonical evidence on the terminal callback,
retries transient failures (including the pre-acceptance 404 race), and launches
work once per repeated in-process `idempotency_key`. A production executor must
persist that single-flight key in its own store; a one-shot or memory-only
implementation would lose reports or repeat work across failures.

```
ATHENA_URL=http://127.0.0.1:8000 EXECUTOR_SECRET=change-me \
    python examples/icarus_executor.py
```

It "works" by immediately reporting evidence and a completed outcome — the
executor equivalent of hello-world. Everything it reports is a claim, which is
exactly the contract: Athena records what it is told and verifies nothing.

## Limitations

- **No redelivery loop.** A dispatch that fails to deliver stays `undeliverable`;
  nothing retries it automatically, and there is no background dispatcher. Adding
  one is a real feature, not a config flag.
- **No cancellation.** Athena can record that it asked; it has no way to un-ask.
- **No evolving evidence sequence.** Callback v1 can safely represent one
  canonical evidence pointer. Supporting several successive pointers requires a
  signed, durable callback sequence; a callback id alone can deduplicate but
  cannot decide which of two reordered reports is newer.
- **No callback receipt key yet.** The envelope carries the dispatch
  `idempotency_key`, but callback v1 correlates through the unique
  `icarus_run_id` and does not echo that key. A future sequenced callback contract
  should carry both the dispatch key and an event id/sequence.
- **Athena never verifies that work happened.** Every terminal state is the
  executor's claim, arriving over a channel authenticated by a shared secret. If
  that secret leaks, the claims are only as good as the secret.
- **A one-way capability has no undo.** `UNDO.md`'s compensation model does not
  reach across this boundary, and nothing here will try to reverse an external
  effect.
- **The executor is unspecified here.** This documents Athena's half of the
  production contract. The test-pinned reference verifies the wire example, but
  no code in this repository verifies a production executor's behavior or that
  its claimed work happened.
