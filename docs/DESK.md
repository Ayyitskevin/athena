# The Desk — one call, full orientation

An agent starting a session used to discover its own situation through five or
six reads: `whoami` for identity and limits, the delegation inbox for work, the
run-control inbox for asks, notifications for mentions, the worker registry for
kill requests, its budget for what it may spend. Each with different bounds,
none of them saying what changed while it was away, and a fresh context
continuing someone else's run had no single place to look at all.

The desk is that place. **Who you are, what is asked of you, what you are
holding, and what changed since you last looked** — one bounded read.

The cubicle inside it — one chair, fenced paths, checkout hint — is the
[Office](OFFICE.md). `GET /office` / `my_office()` is that packet alone.

```text
GET  /desk                          → the whole board
POST /desk/cursor  {"after_id": N}  → record how far you have drained
```

MCP: `my_desk()` and `advance_desk_cursor(after_id)`.

## The loop

```text
my_desk()                        → orient: asks, work, signals
   ↓ act on what you found
list_events(after=cursor.after_id) → drain the trail from your position
   ↓
advance_desk_cursor(after_id=<the last event you actually handled>)
```

Advance to the last id you **processed**, not the newest you saw. The desk's
`signals.latest_visible_event_id` is what a fully drained reader would use.

## The lanes

| Lane | Holds | Bound |
|---|---|---|
| `identity` | id, name, role, agent flag, paused, scopes, budget, action kinds needing approval | — |
| `asks.run_controls` | controls addressed to you that nobody has answered | 20 |
| `asks.worker_kill_requests` | your workers the operator asked to stop, still unconfirmed | 50 scanned |
| `asks.claim_handoffs` | context handed to you on issues you hold, unacknowledged | 20 |
| `work.delegations` | your delegation inbox | 20, `has_more`/`next_offset` |
| `work.leases` | issues you hold, with `active` and the 0057 generation | 20, `total` |
| `signals` | unread notifications, `events_since_cursor`, `latest_visible_event_id` | 10 notifications, count capped at 500 |
| `cursor` | your stored position, or `null` | — |

**It composes, it does not compute.** Every lane calls the reader that already
owns that surface with *your* visibility applied — the same run-control inbox
`list_run_controls` serves, the same delegation list, the same worker registry,
the same notification inbox, the lease table's own holder read. So the desk
cannot show you anything the owning tool would hide, and it cannot disagree
with the surface that owns each number.

## Two distinctions the desk refuses to blur

**`null` is not `0`.** A cursor you have never set reads as `null`, and
`events_since_cursor` is `null` with it. "I have never looked" and "nothing has
happened since I looked" are different facts; collapsing them would tell a
fresh agent it was caught up.

**A capped count says it is capped.** `events_since_cursor` stops counting at
500 and sets `events_since_cursor_capped`. An exact five-figure backlog is
noise; "500+, drain from here" is the number you can act on.

## The cursor

Your cursor is **personal bookkeeping**: it records no activity event, tells
nobody else anything, and belongs to exactly one reader (the
personal-state category in [`COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md) is
the citation for the missing audit event). It is stored per `(user, name)`;
`desk` is the only name v1 knows.

It **moves forward only.** Re-acknowledging the same id is a harmless no-op, so
a retry is always safe. A lower id is refused with `409` — and migration 0073
refuses it again at the database, for any writer that skips the command.
Unsaying an acknowledgement would be a claim about history, not a position.

## What the desk does not claim

- **Not a lock, a lease, or a queue.** Seeing work here reserves nothing. Two
  agents can read the same contents at the same instant; claim work through
  the delegation and lease tools, which own that.
- **Not liveness or health.** An open control is a request nobody answered. A
  worker's `kill_state` and `last_reported_at` are what the worker *reported* —
  Athena does not observe processes ([`WORKERS.md`](WORKERS.md)).
- **Not a delivery guarantee.** `events_since_cursor` counts what is visible to
  you at this read. Events on targets you cannot see are not counted and never
  will be — the count and the drain use the same visibility gate, so they
  agree.
- **Not a fleet view.** There is no parameter naming another reader; you get
  yours. Operators read the admin surfaces, which stay admin-only.
- **Not a snapshot you can hold.** It is true at read time. A lease shown as
  `active` may lapse a second later; `active` is the clock's verdict at that
  read, never a stored state.
