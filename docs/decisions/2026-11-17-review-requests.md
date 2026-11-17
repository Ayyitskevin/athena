# Requesting an exact review

**Status** Accepted on 2026-11-17 following independent specification review.

**Context** A conductor could see a completed exact-version review, but a worker starting a new session still needed the target from the conductor.

**Decision** Record the named reviewer, review issue and candidate when the conductor requests the review. Use the existing issue lease to observe pickup and a later matching completion to observe fulfillment. A replacement submission requires an explicit request.

**Consequences** The desk, full review-issue context and project review queue can reconstruct the handoff. Reading a request requires visibility of the candidate and request events as well as both current issues. The fulfillment event has its own visibility check.

**Sources** Kevin requested implementation and review on 2026-11-17. See the [request specification](../REVIEW_REQUEST_SPEC.md) and [exact-version review decision](2026-11-17-exact-version-reviews.md).

**Superseded-by** None.
