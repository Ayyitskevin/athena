- **Comments, claims and delegations count against a second budget counter
  (C7, decision D6).** A budget may carry `coordination_limit`, counted apart
  from `action_limit` in the same window: issue and page comments, claims
  (pickup included) and delegations. Past it the write is refused `429
  agent_coordination_budget_exhausted` with `budget`, `retry_after_seconds`
  and `Retry-After`; writes that change the work are unaffected, and lease
  renewals, yields and completions are never metered. Omitted or `null`
  leaves coordination unlimited, so an existing budget behaves as it did.
  Set it with `PUT /users/{id}/budget`, `set_agent_budget` or the agents
  page; the budget read, `whoami` and the desk carry `coordination_limit`,
  `coordination_used` and `coordination_remaining`. Migration 0092 adds the
  two columns.
