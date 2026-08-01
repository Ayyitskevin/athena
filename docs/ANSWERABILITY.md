# Answerability: asks and answers per agent, never a score

`VISION.md`'s loop ends at **Trust / Learn**, and its second steering rule says
trust comes from *undo + inspect*, not from watching. Athena already records
every ask an operator can make of an agent — a run control addressed to it, a
kill request to one of its workers — and every ask an agent makes back (an
approval its gated action raised), plus every correction (an undo reversing its
event). What was missing was the one view that lays those asks next to their
answers, per agent.

Buzz's roadmap calls the neighboring idea "web-of-trust reputation across
relays". Athena adapts the useful half and refuses the rest, on purpose:

- **No score.** A single scalar would launder "the clock ran out twice" into
  "bad agent". A solo operator does not need a leaderboard of five agents; they
  need to see which asks went unanswered and decide why.
- **No new facts.** Every number is derived at read time from a table that
  already exists, with the same predicate the owning surface lists by — the
  controls lane matches `/admin/run-controls`, the kill lane reuses the workers
  page's own derived `kill_state`, approvals and reversals count the recorded
  rows. This projection owns no table and stores nothing
  (`core/answerability.py`).

## The lanes

| Lane | Asked by | Answered by | Counted |
|---|---|---|---|
| Run controls | operator | the bound agent | `open`, `expired_unanswered`, `completed`, `declined` |
| Worker kills | operator | the worker process | `told_to_stop` (asked, unconfirmed), `confirmed` |
| Approvals | the agent's gated action | operator | `pending`, `approved`, `rejected` |
| Reversed events | — | operator (undo) | native events of this agent a compensation reversed |

Two deliberate edges:

- A worker that acknowledged a kill **while still reporting running**
  (`acknowledged_but_reporting`) stays in `told_to_stop`. Saying "heard you"
  is not an answer; folding it into `confirmed` would let an agent acknowledge
  its way out of the number the operator is watching.
- `expired_unanswered` is the server clock's read-time verdict on an unsettled
  control — no stored state, no event, exactly as `RUN_CONTROLS.md` defines
  expiry.

Agents with nothing recorded appear **zero-filled**: "no asks yet" is an
explicit statement, not a missing row. Paused agents stay listed and flagged —
a paused agent with open asks is precisely what an operator wants to notice.

## Surfaces

```text
GET /fleet/answerability             → the ledger, every agent (admin)
GET /fleet/answerability?agent_id=N  → one agent; unknown or human id → 404
```

MCP: `agent_answerability(agent_id=None)` — the same read. The web rendering
is the **Answerability** section of `/admin/agents`, beside the approval queue
and worker registry it summarizes. All three surfaces render
`core/answerability.py`; no adapter computes a number of its own.

## What this does not claim

- **These are counts of recorded asks and answers, not competence, safety, or
  productivity.** An agent with three expired controls might be dead, busy, or
  simply never polling its inbox. Athena records that three asks went
  unanswered; the operator judges.
- **Absence of asks is not health.** An agent nobody steered, killed, or gated
  shows zeros everywhere — that is silence, not a clean bill.
- **A reversal is a correction, not a conviction.** Undo exists so operators
  fix things cheaply; a reversed event proves the operator changed an outcome,
  not that the agent misbehaved.
- The ledger is standing and all-time. There is no windowing, trend, or decay
  in v1 — deliberately, because half-life math is where reputation scores
  sneak back in. If bounded periods earn their way in later, they arrive with
  `FLEET_METRICS.md`'s period discipline.
