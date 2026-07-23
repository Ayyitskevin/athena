# Scheduled automation

Athena automation rules have two trigger types:

- `event` keeps the existing activity-cursor behavior: an issue activity verb matches
  a rule and immediately dispatches its action.
- `schedule` selects a bounded snapshot of active issues at a UTC instant and dispatches
  the same action once for each target.

Both use the existing in-process automation loop. There is no second scheduler service,
queue, worker, or deployment mode.

## Configuration

Admins can configure the same rule through **Admin → Automation**, REST, or MCP. A
one-shot REST rule looks like:

```json
{
  "name": "Sprint-end sweep",
  "trigger_type": "schedule",
  "trigger_verb": "scheduled",
  "schedule_at": "2030-01-31T17:00:00Z",
  "schedule_every_seconds": null,
  "conditions": {"sprint_id": 12, "status": "in_progress"},
  "action_type": "set_status",
  "action_params": {"status": "done"}
}
```

Set `schedule_every_seconds` for a recurring fixed UTC grid. The interval is 60 through
31,536,000 seconds. Athena accepts only second-precision canonical UTC timestamps in
`YYYY-MM-DDTHH:MM:SSZ` form. Local, naive, numeric-offset, and fractional timestamps
are rejected; schedules therefore have no DST interpretation.

Schedule rules accept the existing issue equality conditions (`project_id`, `status`,
`priority`, `assignee_id`, and `sprint_id`) plus `inactive_for_seconds`. The inactivity
condition compares the last issue activity with the scheduled slot, which supports
recurring stale-issue nudges without a second mutable "last touched" owner. Archived
issues never enter a schedule snapshot.

The supported actions and action parameters are identical to event rules. Event request
bodies remain backward compatible: omitted `trigger_type` means `event`, and event rules
must not contain schedule fields.

## Determinism, catch-up, and bounds

Claiming a slot, advancing its fixed UTC cursor, recording catch-up counts, snapshotting
targets, and creating the synthetic trigger activities are one SQLite write transaction.
The durable firing key `(rule_id, scheduled_for)` prevents a slot from being claimed
twice. A durable occurrence row separately records each target because a valid no-op or
failed action need not emit an action activity.

The single-process runner applies these bounds:

| Bound | Value | Behavior at the bound |
|---|---:|---|
| Due rules claimed per pass | 10 | Later rules remain visibly due/overdue for the next pass |
| Due rule rows scanned per pass | 100 | A durable cyclic rule-id cursor rotates later rows into the next bounded scan |
| Targets per firing | 50 | 51 or more fails the whole slot closed; Athena performs no partial sweep |
| Target actions attempted per pass | 50 | Remaining durable occurrences resume in issue-id order |
| Attempts per target | 3 | The target and firing become failed after the third error |
| One-shot catch-up window | 24 hours | An older unclaimed one-shot remains visible as overdue and does not run |

After recurring downtime, Athena claims only the latest aligned UTC grid slot. Older
slots collapse into `schedule_missed_count`; they are never replayed in an unbounded
burst. The selected targets are then durable across restarts even if issue fields change
before all target actions finish. Empty snapshots complete normally.

Disabling a rule prevents new claims and pauses pending occurrences. Re-enabling resumes
bounded work. Deleting a rule atomically cancels unfinished occurrences and firings while
retaining their history and trigger activities.

## Visibility and failure handling

Rule reads expose schedule configuration, next/last slot, cumulative missed slots, last
target/overflow counts, pending/failed occurrence counts, latest firing state, and one
computed status:

- `scheduled`, `due`, `overdue`, `processing`, or `completed` for normal lifecycle;
- `disabled` for a paused rule;
- `malformed` for persisted configuration the current runtime cannot safely interpret;
- `failing` for overflow or target-action failures; and
- `event` for the backward-compatible event-rule shape.

Malformed, target-overflow, and expired one-shot configurations fail closed. Action
exceptions or semantic command rejections increment `failure_count` and retain `last_error` /
`last_error_at`; one failed target does not prevent other bounded work from advancing.

## Attribution, lineage, and restart recovery

Each target gets a deterministic, Automation-authored `scheduled` activity. The existing
executor consumes that activity without a separate action path, so action attribution,
the Automation actor loop guard, issue commands, notifications, batches, and failure
instrumentation stay shared with event rules.

The action run id remains `automation:rule-<rule-id>:event-<trigger-event-id>`. Its parent
is the synthetic schedule-trigger run and its fork point is the synthetic trigger event.
If Athena stops after the action commits but before the occurrence receipt advances, the
next pass sees the action run id and suppresses duplicate comments or writes before
marking the occurrence complete. A no-op may safely run again and still advances its
separate receipt.

## Migration and operational boundary

Migration `0059_scheduled_automation.sql` defaults all existing rules to `event`, leaves
`automation_state.cursor` unchanged, and initializes the independent bounded-scan cursor
at zero. The new SQL file is packaged in both the sdist and wheel through Athena's
existing migration package-data rule.

Athena migrations are forward-only. Before upgrading a material database, take the
normal matched database/attachment backup. Rolling application code back across 0059 is
not a schema downgrade; restore the pre-upgrade database backup with the older build.

Scheduled automation supports one Athena process and one automation owner only. It does
not provide cron expressions, local time zones, calendars, HA/multiworker coordination,
or external durable queues.
