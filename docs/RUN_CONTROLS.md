# Run controls — steering a live run by request

[`VISION.md`](VISION.md)'s Intervene step promises the operator can steer by
exception. Between "let it run" and the blunt levers — pause the account, revoke
its tokens, ask a worker process to stop — there was nothing addressed to a
**run**. A run control is that middle lever: an operator records a bounded
request against one live run, and the agent bound to that run reads it and
answers. This is the smallest coherent "fleet room": not a chat, not a second
feed, not a queueing system — a request record over the run identity, worker,
check-in, and activity primitives Athena already has.

## Three words this feature refuses to say

**Steered.** A `steer` control records guidance. Whether the agent reads it,
follows it, or does something else entirely is the agent's answer to give, and
`state` reports only what has actually been said.

**Cancelled.** `request_cancel` asks the agent to wind one run down
cooperatively. Athena cannot signal a process (see
[`WORKERS.md`](WORKERS.md) — the same discipline at worker granularity), so a
run is never "cancelled" by Athena; at most its agent *claims* it wound down.

**Timed out the agent.** An unanswered control reads as `expired` once
`expires_at` passes. Expiry is derived from the server clock at read time —
nothing is written, no event is recorded, and it means exactly "the clock ran
out with no answer", never that anything stopped.

## The control vocabulary (closed, v1)

| Kind | The operator asks | A completion means the agent claims |
|---|---|---|
| `steer` | "Read this bounded guidance for the current run" (payload required, ≤ 4000 chars) | It acted on the guidance, summarized in ≤ 2000 chars |
| `request_cancel` | "Wind this run down cooperatively" (optional reason) | It stopped working under this run, summarized |
| `request_fresh_context` | "Close out with a structured handoff a fresh context can continue from" | It produced the bounded handoff below |

New kinds are deliberately a schema migration: each kind is a promise about what
settlement means.

## Lifecycle

A control is born `requested` and moves monotonically:

| State | Whose claim | Means |
|---|---|---|
| `requested` | Athena's own | An operator recorded the request; the agent has said nothing |
| `acknowledged` | the agent's | It says it received the request — receipt, not outcome |
| `completed` | the agent's | It says it did what was asked, with a bounded summary or handoff |
| `declined` | the agent's | It says it will not comply, with the reason |
| `expired` | the server clock's | `expires_at` passed with no settlement — derived at read time, never stored |

Acknowledgement is optional (an agent may complete or decline directly) and
idempotent (re-acknowledging is a no-op). Settlement is exactly once: the write
is a compare-and-set, so of two racing settlements one wins and the other is
refused with `409`. A settled control is frozen — database triggers refuse any
further change, and controls are never deleted. There is no operator withdrawal
in v1; choose `ttl_seconds` to bound how long an ask stands.

## Who may do what

- **Create**: an admin (role plus, for bearer tokens, the `admin` scope) — the
  same authority that can ask a worker to stop.
- **Read and settle**: only the agent the control is bound to, holding a live
  bearer token with a write scope; credentials, pause state, and run ownership
  are re-checked inside the settlement transaction. Admins can read everything.
  Everyone else sees an empty list and 404s, the same answers a missing control
  gives.
- A **paused** agent can do nothing — including reading the control asking it to
  stop — so creation against a paused agent's run is refused (`409`) rather than
  recording a request nobody can receive.

The bound agent is resolved at admission: the run binding
([`RUNS.md`](RUNS.md) — first tagged writer owns the run id) when one exists,
else the run's sole cooperative check-in. A run id nobody has written or
checked in under is a 404; an unbound run id that multiple agents have checked
in under is refused as ambiguous. Server-reserved runs (`automation:`,
`icarus:`) cannot receive controls — no agent polls them. If a run binding
later appears under a different actor than the control was admitted against,
settlement refuses with `409 run ownership changed` rather than trusting either
story.

A control may name a `worker_id` (see [`WORKERS.md`](WORKERS.md)) to say which
process the operator means. Workers authenticate with their agent's token, so
this narrows *intent*, never authority — any credential of the bound agent may
settle. Self-declared worker capabilities grant nothing here, as everywhere.

## REST

```text
POST /run-controls
Authorization: <admin>
Content-Type: application/json

{"run_id": "goal-123", "kind": "steer", "payload": "Focus on the flaky test",
 "ttl_seconds": 3600, "idempotency_key": "steer-goal-123-1"}
```

`201` returns the recorded control. `idempotency_key` is the domain single-flight
key (minted server-side when omitted): retrying with the same key returns the
same control with `"replayed": true`; reusing it for a *different* control is a
`409`. The `/run-controls` root also honors the transport `Idempotency-Key`
header contract. `ttl_seconds` is bounded 60–86400; the default is
`ATHENA_RUN_CONTROL_TTL_SECONDS` (3600).

```text
GET  /run-controls?run_id=...&state=open&limit=50   ← the agent's inbox / panel query
GET  /run-controls/{id}
POST /run-controls/{id}/acknowledge                 ← bound agent; body-less
POST /run-controls/{id}/decline      {"reason": "mid critical section"}
POST /run-controls/{id}/complete     {"summary": "switched to approach B"}
POST /run-controls/{id}/complete     {"handoff": {...}}   ← request_fresh_context only
```

`state` filters on the derived state (`requested`, `acknowledged`, `completed`,
`declined`, `expired`) plus `open` — unsettled and not yet expired. Settling a
settled or expired control, acknowledging late, or settling someone else's
control refuses (`409`/`404`); refusals record nothing.

MCP callers use `create_run_control`, `list_run_controls`, `get_run_control`,
`acknowledge_run_control`, `decline_run_control`, and `complete_run_control`
under the same rules. The web panel on
`/aegis/activity/runs/{run_id}/lineage` is the browser twin of the same
commands: admins record requests there; settlement is deliberately API/MCP-only,
because settling is the *agent process's* answer and a browser session is not
the agent.

## The fresh-context handoff

A `request_fresh_context` completion stores exactly this bounded object, and
nothing else:

| Field | Bound |
|---|---|
| `summary` (required) | ≤ 2000 chars |
| `unresolved_questions` | ≤ 10 items × 500 chars |
| `athena_refs` | ≤ 20 items × 200 chars — issue/run/page ids, not content |
| `evidence_refs` | ≤ 10 items × 500 chars — pointers, never copies |

Unknown fields are refused; the encoded object is capped at 8000 chars. Hidden
reasoning, transcripts, secrets, and ambient environment state have nowhere to
go — by construction, not by policy. (The claim-handoff in
[`ACTIVE_WORK.md`](ACTIVE_WORK.md) is the issue-scoped sibling of this shape.)

## The trail

Every recorded fact is an activity event in the existing log — there is no
second feed. `run_control_requested` is the operator's event;
`run_control_acknowledged`, `run_control_declined`, and `run_control_completed`
are the agent's, each written atomically with the control change and linked
back via `requested_event_id` / `acknowledged_event_id` / `settled_event_id`
(database triggers accept only native, unrestricted events that actually record
the fact). Expiry, being nobody's action, has no event.

The operator's request event carries the *operator's* run coordinates, never
the target run's — writing under the agent's run id would violate the run
binding. An agent that stamps its settlement calls with `X-Athena-Run: <run>`
joins its own run's replay, which is exactly where its answer belongs.

## Bounds and refusals

Payloads ≤ 4000 chars; summaries and decline reasons ≤ 2000; control characters
beyond newline/tab refused everywhere. Duplicate live requests are refused: at
most one unsettled, unexpired control per (run, kind), so the operator's queue
cannot silt up with restatements. All refusals are side-effect free.

## What this feature does not claim

A control never executes anything, schedules anything, or grants authority.
Acknowledgement proves receipt; completion and decline are identity-bound
claims by the agent; neither proves an operating-system effect, and Athena
will not report a run as steered, cancelled, or refreshed — only what was
asked and what the bound agent said back.
