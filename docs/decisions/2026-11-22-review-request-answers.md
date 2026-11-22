# A review request answers the reviewer's completion

Date: 2026-11-22. This is the decision date.
Status: Accepted, owner's standing instruction (D16).
Superseded-by: None.

## Context

[Work awaiting an answer is not ready](2026-11-22-ready-rule-awaiting-decision.md)
keeps an issue out of the ready lane of the account that completed it until
another account answers. A review request was not an answer. The second review
round reproduced the result as FLEET-4: after a reviewer's HOLD and the
conductor's second request, the desk asked for the review while the issue sat
under `not_offered: completed_by_you` and `claim_next_ready` answered
`nothing_ready`. With the conductor recipe's own `blocked_by` link, pickup also
answered `nothing_ready` while the desk showed the request, and said nothing
about it. Requirement F2 of the
[Athena in use specification](../plans/ATHENA_IN_USE_SPEC.md) fixes both.

## Decision

- A completion is also answered by a review request. The issue's newest review
  request answers the completion of the account it names, and only that
  account's, when it was made by another account after the completion, is
  native, asks about the source issue's current candidate, and its request and
  candidate events are visible to that account. A request the account cannot see
  answers nothing.
- The rule reads the review-request rows; it adds no answer verb. The
  `review_requested` event does not carry whom it names, and an answer verb
  would lift a `needs_input` hold and answer for every account.
- The `needs_input` hold, `blocked` and `capacity` yields, direct `claim_issue`
  and the meaning of a blocker are unchanged (D28). Pickup never claims a review
  issue with an open blocker.
- Every `claim_next_ready` answer that claims nothing carries `review_request`:
  the newest request the caller's desk shows as `requested`, with the
  `claim_issue` call and the review issue's ETag that take it, or null. It is
  part of the receipt and replays as recorded.

## Consequences

The amended rule is shared by `claim_next_ready`, the desk's ready lane and
total, `not_offered` and `is:ready`, so they still agree in every state. The rule
adds one correlated read of `issue_review_requests` per candidate issue whose
newest settlement is the caller's own completion; no table and no migration.

A conductor that asks again after a HOLD has answered the reviewer. A reviewer
that asks for its own review has not.

## Sources

- D16 in [Athena in use: thirteen decisions accepted by standing instruction](2026-11-22-athena-in-use-decisions.md),
  and D28 in the same record.
- [Athena in use specification](../plans/ATHENA_IN_USE_SPEC.md), requirement F2;
  [review report](../plans/reports/repo-review-2026-11-22-round2.md), finding FLEET-4.
- Implementation and tests: `src/athena/aegis/ready_queue.py`,
  `src/athena/aegis/ready_claim_commands.py`,
  `tests/test_ready_review_requests.py`, and the F2 scenario in
  `tests/test_fleet_trial.py`.
