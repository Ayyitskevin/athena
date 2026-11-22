# Agentic-use specification: fifteen decisions accepted as recommended

Date: 2026-11-22. This is the decision date.
Status: Accepted, owner instruction.
Superseded-by: None.

## Context

The [agentic-use specification](../plans/AGENTIC_USE_SPEC_ATHENA.md) follows
from the [review of `99a0286`](../plans/reports/repo-review-2026-11-21.md). It
lists fifteen decisions, D1 to D15, each with a recommendation. Several of its
requirements cannot be built until the decision they depend on is made, and
four of them amend a decision that was already accepted.

## Decision

All fifteen are accepted as the specification recommends. In short:

- **D1.** An unset `ATHENA_MCP_PROFILE` keeps meaning `full`. Printed
  configurations name a profile explicitly; revisit later.
- **D2.** Every REST refusal gains a `code`; bodies are not otherwise
  normalised now. New endpoints page with an opaque `cursor` in and
  `next_cursor` with `has_more` out.
- **D3.** After a `needs_input` yield, the issue is not ready for any account
  until it is answered. After a completion, it is not offered back to the
  account that completed it; other accounts are unaffected for now.
- **D4.** The review queue may carry a field that joins a request to the
  verdict that fulfilled it. `review_state` is unchanged.
- **D5.** An instance with no root catalog compares paths lexically under one
  implicit root and reports the basis as "lexical, root unknown". A project
  may require declared paths.
- **D6.** Comments, claims and delegation count against a second,
  coordination budget counter. Lease renewals stay exempt.
- **D7.** Tailnet mode refuses to start without an explicit
  `ATHENA_ANONYMOUS_READS` value.
- **D8.** A `.claude/` directory with a session bootstrap and a gate
  allow-list is committed, with the same bootstrap as a plain script.
- **D9.** Athena's own development is tracked in Athena, once someone operates
  an instance the lanes can reach. That instance is a live service and the
  self-merge carve-out in `AGENTS.md` does not cover it.
- **D10.** Tagging a release, setting prune verdicts, closing PR #438 and
  deleting merged branches remain the owner's own acts. Accepting this record
  does not perform them.
- **D11.** A request that observes an expired lease may record a
  `lease_lapsed` event, in its own transaction after the read snapshot.
- **D12.** Pause, offboard and token revocation may release the account's
  active leases as an explicit operator option, recorded per lease.
- **D13.** The approval command refuses every agent account as decider, and an
  account may not change its own gate or budget.
- **D14.** Desk cursors that are past the newest event are rewound once, by a
  forward-only migration that drops and recreates the advance-only trigger.
- **D15.** Whether four CI coverage runners are still needed is decided from a
  re-measured shard after the test-memory fix lands.

## Consequences

Acceptance authorizes the design, not the work. Each requirement still lands
as its own change with its regression test and the gate. Only Wave 0 of the
specification is authorized to be built now; D13 and D14 are the two it uses.

Four accepted records are amended, not replaced, and each keeps
`Superseded-by: None` until the change that implements the amendment lands and
adds its own successor record:

- D3 amends the ready rule in
  [Atomic ready-work pickup](2026-11-17-atomic-ready-pickup.md).
- D4 amends two sentences of
  [Requesting an exact review](2026-11-17-review-requests.md): request status
  being advisory only, and verdict and request-status fields staying separate.
- D11 adds a write to a read path, which the accepted
  [coordination reliability specification](../COORDINATION_RELIABILITY_SPEC.md)
  did not allow for.
- D12 sits beside ruling K1 in
  [Coordination and history rulings](2026-10-20-coordination-history.md).
  K1 stands: nothing is swept automatically.

D13 is stricter than `docs/APPROVALS.md` reads today. An operator who has given
an agent the admin role loses the ability to have that agent decide approvals.

## Sources

The owner accepted all recommendations on 2026-11-22, in the session that
produced PR #466, when asked how the fifteen decisions should be treated. The
same answer limited the first pass to Wave 0.
