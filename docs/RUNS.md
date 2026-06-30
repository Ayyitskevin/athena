# Athena Run Contract

Athena treats runs as a projection of the append-only `activity` log. There is no
mutable `runs` table: a run exists when events share the same run metadata.

## Headers

Clients that want deterministic replay stamp writes with:

- `X-Athena-Run`: the current run id.
- `X-Athena-Parent-Run`: the parent run id, when this run was spawned by another.
- `X-Athena-Fork-From-Event`: the parent activity event id after whose shared prefix
  this run diverged.

Browser actions normally omit all three headers and remain ordinary activity rows.

## Replay And Lineage

- `GET /activity?run_id=...` returns one run's activity newest-first.
- `GET /events?run_id=...` returns one run's activity oldest-first for replay.
- `GET /activity/runs/{run_id}/lineage` reconstructs the parent/child tree from
  `run_id`, `parent_run_id`, and `forked_from_event_id`.

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
