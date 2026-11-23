- **Pickup no longer hides a review the desk asks for.** A review request naming
  you, made by another account after your completion, now answers that
  completion under the ready rule, so after a HOLD and a second request the
  review issue is ready for you again on the desk, `is:ready` and
  `claim_next_ready`. Every `claim_next_ready` answer that claims nothing carries
  `review_request`: the newest waiting request with the `claim_issue` call that
  takes its review issue, or null. Pickup still never claims a review issue with
  an open blocker, and a `needs_input` hold is unchanged (D16, FLEET-4).
