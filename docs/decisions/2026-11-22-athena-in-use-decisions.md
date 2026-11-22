# Athena in use: thirteen decisions accepted by standing instruction

Date: 2026-11-22. This is the decision date.
Status: Accepted, owner's standing instruction; each is the owner's to reverse.
Superseded-by: None.

## Context

The [Athena in use specification](../plans/ATHENA_IN_USE_SPEC.md) follows from
the [second review round, at `0b42d40`](../plans/reports/repo-review-2026-11-22-round2.md).
It lists thirteen decisions, D16 to D28, each with the lead's recommendation.
The owner was away while it was written and had said how such points should be
handled.

## Decision

Each recommendation is accepted as written in the specification's "Decisions
for the owner" table. In short:

- **D16.** A review request naming an account, made by another account, is an
  answer to that account's own completion under the ready rule. `blocked_by`
  keeps its meaning. Pickup names a waiting review and does not claim it.
- **D17.** An issue's assignment or creator changing may fence only that
  issue's idempotency receipts, and a fenced marker may expire with its
  receipt, provided the build shows a replay never returns what its caller
  could no longer obtain. Otherwise only the expiry is built.
- **D18.** A lease renewal stops notifying the assignee and stays an event.
  The existing acknowledgement tool accepts many notifications.
- **D19.** A member may read the accounts eligible for assignment or
  delegation on an issue it can see: id, name and whether it is an agent.
- **D20.** An issue's prior title and body are kept on edit. A bearer-token
  caller needs `If-Match` to change either.
- **D21.** The six parts of a work order are headings the work-context packet
  recognises. A missing part refuses nothing.
- **D22.** A learning reaches the issue it was recorded on, that issue's
  parent, and its siblings under the same parent.
- **D23.** The default for `ATHENA_ANONYMOUS_READS` does not change in this
  specification. The state is reported; the default is decided with the next
  tagged release.
- **D24.** A second process on one database serves requests without the
  background loops and says so.
- **D25.** Structured request logs and a request id on every response. No
  metrics endpoint.
- **D26.** An explicit maintenance command may delete read notifications,
  expired sessions and expired idempotency markers past documented ages. The
  activity trail and its chain are never pruned.
- **D27.** Declared fleet seats move from source to data. An existing instance
  keeps its eight by migration.
- **D28.** `blocked` and `capacity` yields, and a direct claim by the settling
  account, keep the behaviour the ready-rule record gives them.

## Consequences

Acceptance authorizes the design, not a shortcut. Each requirement still lands
as its own change with its regression test and the gate.

The owner did not read these thirteen before they were recorded. Any of them
can be reversed by saying so; reversing one reopens the requirements that name
it, and work already merged under it is then changed by a new requirement, not
by rewriting history.

Three accepted positions are amended, not replaced. Each keeps
`Superseded-by: None` until the change that implements the amendment lands and
adds its own successor record:

- D16 adds one answer to the rule in
  [Work awaiting an answer is not ready](2026-11-22-ready-rule-awaiting-decision.md).
  D28 leaves the rest of that record as it is.
- D17 narrows the global authorization fence that `docs/OPERATIONS.md` and
  `docs/ARCHITECTURE.md` describe as deliberately broader than per-target
  invalidation.
- D20 makes a precondition required where it was optional. A bearer-token
  caller that edits an issue's text without `If-Match` works today and will be
  refused.

D26 does not touch ruling K1 in
[Coordination and history rulings](2026-10-20-coordination-history.md): no
lease is swept.

The standing instruction does not reach the owner's own acts, and nothing here
performs one: cutting or pushing a tag, pushing a container image, setting a
prune verdict, answering an item in
[DECISIONS_PENDING.md](../DECISIONS_PENDING.md), or pushing `main`.

## Sources

On 2026-11-22, in the session that produced the specification, the owner
wrote "go with your recs for the next spec", and later, before stepping away,
"default to your recs for me when you get to stopping points".
