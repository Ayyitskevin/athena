- **A budget exhausted mid-task says what to do and tells who answers for the
  work, once per window (F13).** The 429 `agent_budget_exhausted` now carries
  `held_leases` (the issue and generation of each lease the refused account
  holds) and `retry_after_seconds`, and its `next` is `yield_claim` with reason
  `capacity`: a yield is not metered, so the seat can hand the work back to
  the ready lane where it could only wait out the window holding it. The
  first refusal of a budget window records one `holder_budget_exhausted` event
  on each held issue and tells the issue's assignee and the account that
  delegated it, when they can see the issue; the holder is not told again.
  The exhaustion itself is recorded once per account and window where it was
  recorded for every refused request, so the operator's attention list has
  one row for it, saying how much was spent, how many leases the account
  holds and the first request refused. Nothing is released automatically.
