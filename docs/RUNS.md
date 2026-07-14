# Athena Run Contract

Athena treats replayable runs as a projection of the append-only `activity` log.
There is no mutable authoritative `runs` table: a run exists when events share the
same run metadata. Cooperative check-ins are a separate sidecar signal and never
change replay history.

## Headers

Clients that want deterministic replay stamp writes with:

- `X-Athena-Run`: the current run id.
- `X-Athena-Parent-Run`: the parent run id, when this run was spawned by another.
- `X-Athena-Fork-From-Event`: the parent activity event id after whose shared prefix
  this run diverged.

Browser actions normally omit all three headers and remain ordinary activity rows.

## Cooperative Check-ins

An agent may report that it is still working under a client-chosen run identifier
without creating an activity event:

```text
PUT /agent-runs/heartbeat
Authorization: Bearer <agent token with a write scope>
Content-Type: application/json

{"run_id":"goal-123"}
```

The bearer identity must be a user marked as an agent. Its token must have at least
one write scope (`issue:write`, `docs:write`, or `admin`). The request body contains
exactly one nonblank, printable, 1–200 character `run_id`; the server derives the
agent and timestamps. Control, bidi-formatting, zero-width, line-separator, and
invalid Unicode characters are rejected; visible Unicode is NFC-normalized. MCP
callers use `heartbeat_agent_run(run_id)` under the same rules.

The first accepted PUT records `first_seen_at`. Every accepted repeat for that same
agent and run refreshes the server-owned `last_seen_at`, so callers should send PUTs
periodically while work continues. This refresh is intentionally not a durable
idempotency replay: omit `Idempotency-Key`, and never send a client timestamp.

The response reports `age_seconds` and a `reporting_state` of exactly
`reporting_recently` or `stale`. Athena calculates both from server time and
`ATHENA_AGENT_RUN_STALE_SECONDS` (90 seconds by default); client clock skew cannot
keep a report fresh. Mission Control exposes these check-ins separately from
activity-derived run health, including agents that have only checked in. Headline
counts use one latest report per agent across full retained history; the separate
recent-history rows and compatibility totals remain bounded.

Athena also caps durable check-in cardinality per agent with
`ATHENA_AGENT_RUN_MAX_CHECKINS_PER_AGENT` (1,000 by default). Once full, a new run
id receives `409`, while existing run ids remain refreshable. A client should keep
one stable id for one logical run, never mint a new id for each heartbeat.

A check-in is a cooperative observation, not a process supervisor or work lease. It
does not prove the agent's OS process is alive, append heartbeat events to the
activity log, auto-finish a run, revoke credentials, assign or take over work, or
authorize another actor to mutate the run. Stale state is an operator signal only.
A heartbeat-only identifier does not create a replayable activity run; that happens
only when activity events are written with the same run id.

Mission Control derives headline state from one newest check-in per agent across the
agent's full retained history. Older and parallel run ids stay in the bounded recent
history but do not each add another stale headline signal. Timestamp ties resolve by
run id, so REST, MCP, and web select the same deterministic latest report.

## Replay And Lineage

- `GET /activity?run_id=...` returns one run's activity newest-first.
- `GET /events?run_id=...` returns one run's activity oldest-first for replay.
- `GET /activity/runs/{run_id}/lineage` reconstructs the parent/child tree from
  `run_id`, `parent_run_id`, and `forked_from_event_id`.
- `GET /activity/runs/{run_id}/replay` returns a portable JSON artifact with the
  run's complete ordered events plus light lineage metadata.

Operators can write the same artifact from a database file:

```bash
athena-export-run /var/lib/athena/athena.db goal-123 /exports/run-goal-123.json
```

Existing artifact files are not replaced unless `--overwrite` is passed.
Admins can also open `/admin/agents/runs`, filter to one agent, and copy the
matching `athena-export-run` command for any tagged agent run.

## Determinism Contract

Replay is a read of recorded facts, not a re-execution of side effects. A consumer
can trust these event fields as the replay-safe envelope:

- `id`: monotonic activity id; this is the replay order.
- `actor_id`, `verb`, `target_kind`, `target_id`, `detail`: the recorded action.
- `run_id`, `parent_run_id`, `forked_from_event_id`: the run/fork coordinates.

These fields are observational, useful for humans but not enough to re-drive work:

- `actor_name`: resolved from the current user row.
- `created_at`: wall-clock timestamp for display; order by `id`, not time text.
- Rendered HTML, current issue/page state, search snippets, and notification state.

Handlers that want replayable work must record the result of any nondeterministic
operation as an event or artifact before another handler depends on it. Do not make a
projection depend on the current clock, random ids, network calls, or model output
unless that value was already written into the log. Changing a verb or the meaning of
`detail` is a contract change: add tests for the old and new shape rather than
silently changing replay semantics.

## Forking

Forking starts with a read-only contract request:

```text
GET /activity/runs/{parent_run_id}/fork?from_event_id={event_id}&fork_run_id={child_run_id}
```

The endpoint validates that `{event_id}` is visible and belongs to the parent run.
It returns the visible shared-prefix events through that point and the exact headers
to put on subsequent writes. Those later writes are the child fork.

This keeps the fork point on the log itself: replay the parent prefix through
`forked_from_event_id`, then continue with the child run's own events.
