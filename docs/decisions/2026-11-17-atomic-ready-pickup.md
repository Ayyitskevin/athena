# Ready pickup serializes selection and acquisition

Date: 2026-11-17.
Status: Accepted implementation direction, maintainer request and independently reviewed specification.
Superseded-by: None.

## Context

The former `claim_next_ready` client helper used separate reads and a direct
claim. Direct claims already serialized possession of an individual issue, but
queue readiness and the account's held-work precheck were snapshots. A dependency
could change before acquisition, and concurrent requests from one account could
claim different issues. The limitation is recorded in the earlier
[coordination reliability specification](../COORDINATION_RELIABILITY_SPEC.md).

## Decision

Implement a server-owned ready pickup command under one immediate write
transaction. It validates the optional project, checks the account's active
possessions across all projects, selects ready work using existing queue rules,
and calls the existing claim owner. It constructs its acquired-state receipt before
committing. Ordinary direct claims retain their existing multiple-possession
policy; this restriction is specific to queue pickup.

Use the existing optional durable idempotency contract. Replayed success and
no-work responses are historical, never a renewed lease or a new queue poll.
Current run/credential fingerprints, authorization fences, finite receipt
retention and indeterminate crash handling remain unchanged.

## Consequences

Workers can request one task without coordinating client-side selection races.
An account already holding active work receives `already_holding`, including
when its request selects a different project. Deliberate direct claims can still
acquire additional work. A new possession has its ordinary generation, audit
event and handoff rules; queue pickup grants no process execution or acceptance
authority. Consumers must read full context before work.

The old client-only `lost_race` outcome is replaced: after serialized selection,
an empty queue reports `nothing_ready`; bounded ordinary-command conflicts report
`candidates_conflicted`. Unexpected errors remain errors. The specification owns
the complete response, compatibility and test contract.

## Sources

- Maintainer instruction in the 2026-11-17 Athena working session: “lets build
  atomic ready-work pickup next. put together the spec, get an independent review,
  then implement it and merge once all checks pass”.
- [Atomic pickup specification](../ATOMIC_PICKUP_SPEC.md), R2 independently
  accepted before implementation. Exact reviewed SHA256:
  `f2a2e3ca813c5922b9ea4deed4d9736a3aa290c007d9f0e05ed5071ca6a2b41d`.
- Existing owners: [ready queue](../../src/athena/aegis/ready_queue.py),
  [claims](../../src/athena/aegis/lease_commands.py),
  [durable retries](../../src/athena/core/idempotency.py).

This records the accepted implementation direction. It does not assert deployment
or replace the independent implementation review and local/hosted delivery gates.
