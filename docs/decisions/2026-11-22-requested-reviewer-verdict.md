# A review request carries what its requested reviewer reported

Date: 2026-11-22. This is the decision date.
Status: Accepted, owner instruction (D4).
Superseded-by: None.

## Context

[Requesting an exact review](2026-11-17-review-requests.md) records a named
reviewer, a review issue and a candidate, and shows the request's status beside
the review queue's verdict fields. Its specification kept the two apart: request
status is advisory, and the queue keeps its verdict and request-status fields
separate. The queue's `review_state` follows the candidate's newest linked
review by any account.

The fleet trial reproduced the consequence as FLEET-3. A conductor asks Rex to
review candidate 1, and Rex reports HOLD. An account nobody asked completes an
unrelated issue naming candidate 1 with PASS. The queue then reads
`reported_pass` with that account as `latest_review.reported_by`, and the request
row, `fulfilled`, carries no verdict. A conductor following the recipe could not
read what the reviewer it asked had said. Requirement C5 of the
[in-use specification](../plans/ATHENA_IN_USE_SPEC.md), carried from the
[agentic-use specification](../plans/AGENTIC_USE_SPEC_ATHENA.md), fixes it; its
decision D4 allowed the queue to join a request to the verdict that fulfilled it.

## Decision

The shared review request item, served in the review queue's
`review_requests.items[]`, the review issue's full work context
(`review_request`) and the desk's `asks.review_requests`, gains two fields:

- `fulfillment_event_id`: the completion event that fulfilled the request, or
  null.
- `reported_verdict`: the verdict that completion's native report states, one
  of `PASS`, `PASS-WITH-NOTES`, `HOLD`, `BLOCKED`, or null when the request is
  not fulfilled or the report has no verdict.

They are set exactly when the request's status is `fulfilled`, from the
completion that status already rests on: the newest exact-candidate review
linked to the request's candidate and review issue, recorded after the request
by the requested reviewer. The read checks the reviewer and the review issue
itself; it does not rely on the completion command, which also refuses anyone
else's review on a requested review issue.

- Another account's review never moves the fields, on any issue.
- The requested reviewer's review of another candidate, or of the same
  candidate on another issue, does not move them either: the request names a
  review issue, and only a review there answers it.
- When the requested reviewer reports twice on the same candidate, the newer
  report wins, as `latest_review` takes the newest review. A newer report
  without a verdict reads null; an older verdict is never substituted.
- Hidden reads as missing. The newest fulfilling completion is chosen before
  visibility; if the reader cannot see it, the request reads exactly as
  unfulfilled, and if the reader cannot see the request it is not served.
- Each request carries its own report. A replacement candidate needs a new
  request, which starts null; a fulfilled request keeps its report after its
  candidate is replaced; a request superseded before any report stays null.
  There is no cancel.
- Imported history creates no request, candidate or review link, so it carries
  no verdict.

The conductor recipe gates on `reported_verdict` of the request it made, found
by `reviewer_id` on the candidate's queue row.

## Consequences

Two sentences of the review request specification are superseded: the request
row now carries a verdict beside its status, and the conductor gates on it
rather than on the advisory status alone.
[REVIEW_REQUEST_SPEC.md](../REVIEW_REQUEST_SPEC.md) and
[REVIEW_QUEUE_SPEC.md](../REVIEW_QUEUE_SPEC.md) carry the amendment.

`review_state`, `latest_review` and how an unrequested review behaves are
unchanged: a workspace with no review requests answers byte for byte as before.
Exact-candidate binding ([exact-version reviews](2026-11-17-exact-version-reviews.md))
is unchanged, and so is the rule that
[completion reports carry no authority](2026-11-04-completion-reports.md): the
fields are a fact about who reported what. They grant nothing, approve nothing
and enforce nothing; the recipe is what tells a conductor to gate on them.

No migration. The fields are derived from rows that already exist: the request,
its candidate's review links and the native completion event. The read adds one
indexed lookup of the fulfilling event per returned request row, within the
existing bound of five (queue), one (context) or twenty (desk). Every request
row's answer grows by the two fields, null on the desk, where fulfilled
requests are not listed.

## Sources

- The owner accepted D4 as recommended on 2026-11-22, recorded in
  [Agentic-use specification: fifteen decisions accepted as recommended](2026-11-22-agentic-use-decisions.md):
  "The review queue may carry a field that joins a request to the verdict that
  fulfilled it. `review_state` is unchanged."
- [In-use specification](../plans/ATHENA_IN_USE_SPEC.md), requirement C5;
  [round-two review](../plans/reports/repo-review-2026-11-22-round2.md),
  finding FLEET-3.
- Implementation and tests: `src/athena/aegis/review_request_projection.py`,
  `tests/test_review_request_verdict.py`, and the C5 scenario in
  `tests/test_fleet_trial.py`.
