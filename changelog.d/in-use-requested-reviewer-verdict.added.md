- **A review request now carries what the reviewer it named reported.** Each
  request row, in the review queue's `review_requests`, the review issue's full
  context and the desk, gains `reported_verdict` and `fulfillment_event_id`: the
  verdict and completion event of the named reviewer's own report on that review
  issue about that exact candidate, newest first, null until it reports. Before,
  an account nobody asked could complete an unrelated issue naming the candidate
  with PASS, the queue then read `reported_pass` over the requested reviewer's
  HOLD, and the request row said only `fulfilled`. No other account's review
  moves the new fields, and a report the reader cannot see reads as none. The
  conductor recipe now gates on the request row's `reported_verdict`.
  `review_state`, `latest_review` and unrequested reviews are unchanged, and the
  fields grant nothing. No migration (decision D4).
