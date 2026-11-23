# A read that observes a lapsed lease records it

Date: 2026-11-22. This is the decision date.
Status: Accepted, owner instruction (D11).
Superseded-by: None.

## Context

A lease's expiry makes "active" a pure function of the clock, with no
background sweeper. The read paths derived `lease_expired` and showed it to the
holder and to admins, and to nobody else. The review of `0b42d40` reproduced the
result as FLEET-2: a 60-second lease lapsed, and four wait timeouts over 100
seconds showed the conductor that delegated the work nothing; no event, no
notification, no attention row. Requirement C4 of the
[agentic-use specification](../plans/AGENTIC_USE_SPEC_ATHENA.md), carried into
[Athena in use](../plans/ATHENA_IN_USE_SPEC.md), fixes it.

The fix makes a read path write. The accepted
[coordination reliability specification](../COORDINATION_RELIABILITY_SPEC.md)
says readers use a read snapshot and take no writer lock (its section "C2-C4:
coherent, bounded read models"), and did not allow for a write. Decision D11
asked whether a request that observes the lapse may record it; the owner
accepted that it may, in its own transaction after the read snapshot. This
record is the successor D11 said would land with the change.

## Decision

- **No sweeper.** The first request that observes an expired, unsettled lease
  records one native `lease_lapsed` event for that lease generation. Unsettled
  means the lease row is still there. A done or archived issue records nothing.
- **Who observes.** An account observes the leases it answers for: it is the
  issue's assignee, or it delegated the issue to the holder. Two reads observe:
  the event feed (`GET /events`, so `wait_for_events`) on entry, before it
  reads, and the attention ranking (`GET /attention/ranking`) after it reads.
  An account records only lapses on issues it may see.
- **Readers keep their snapshot and take no writer lock while they read.** An
  observer reads its candidates with no transaction open. The one command,
  `lease_lapse_commands.record_lease_lapse`, writes in its own `BEGIN IMMEDIATE`
  transaction, re-reading the possession under the lock. A write that cannot get
  the lock is logged and left for the next observer; the read answers regardless.
- **Once per generation, without a migration.** The command writes only if no
  native `lease_lapsed` event's detail begins with that generation. Writers are
  serialized, so a second observer finds the first one's event. A generation
  never becomes live again (migration 0057's triggers), so a reacquired lease
  that lapses later is a new lapse. Imported rows with the verb do not count.
- **Actor and subject.** The event's actor is the observing account. The holder,
  who did nothing, is the subject, in the detail:
  `generation <g>; holder_id <n>; expired_at <t>; holder <name>`. The event
  carries no run coordinates.
- **Who is told.** The assignee and the delegating account get an inbox row,
  even when one of them is the observer, if they can see the issue; watchers get
  the ordinary fan-out. The attention ranking shows an account without admin
  authority the claims it holds and, on issues it can see, the claims it
  answers for.
- **Automation does not act on it.** `lease_lapsed` is not a trigger verb, a
  wildcard rule skips it as it skips imported history, and it does not reset a
  schedule's inactivity clock.
- **Nothing is released.** Ruling K1 in
  [Coordination and history rulings](2026-10-20-coordination-history.md) stands:
  the row stays until its holder clears it or the next claim replaces it.

## Consequences

The coordination reliability specification's rule that readers take no writer
lock is amended in one respect: two reads may write after (or, on the feed,
before) their read, in a separate immediate transaction. The read snapshot and
its coherence rules are unchanged.

A read can now cost a write, at most twenty events per request, and one bounded
candidate query per feed or attention read. A caller that cannot see an issue
neither triggers nor learns of its lapse. The observing account appears as the
actor of an event it did not cause, which the detail and this record explain.
The once-per-generation check reads the issue's activity through the existing
`(target_kind, target_id, id)` index.

## Sources

- The owner accepted D11 as recommended on 2026-11-22, recorded in
  [Agentic-use specification: fifteen decisions accepted as recommended](2026-11-22-agentic-use-decisions.md):
  "A request that observes an expired lease may record a `lease_lapsed` event,
  in its own transaction after the read snapshot."
- [Agentic-use specification](../plans/AGENTIC_USE_SPEC_ATHENA.md), section
  "C4: failure is an event"; [Athena in use](../plans/ATHENA_IN_USE_SPEC.md),
  requirement C4; [review report](../plans/reports/repo-review-2026-11-22-round2.md),
  finding FLEET-2.
- Implementation and tests: `src/athena/aegis/lease_lapse_commands.py`,
  `src/athena/aegis/leases.py` (`unrecorded_lapses`), `tests/test_lease_lapsed.py`,
  and the C4 scenario in `tests/test_fleet_trial.py`.
