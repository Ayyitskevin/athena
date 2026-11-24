- **A HOLD or BLOCKED report asks whoever answers for the work, and an
  agent's last refusal is readable (F15).** A completion that reports `HOLD`
  or `BLOCKED` is a ranked attention row (`held_report`, `high`) for the
  issue's assignee, the account that delegated it and the operator, and a
  count on the fleet-attention card, until an account other than the reporter
  acts on the issue or anyone claims it again. The run-health read
  (`GET /activity/agent-runs`, `get_agent_run_health`, the agent-runs page)
  carries, per agent account, `last_refusal` (the newest 4xx it was answered:
  `code`, `status_code`, `request`, `at`) and `last_nothing_ready_at` (the
  newest time a pickup answered `nothing_ready`). Both are kept one row per
  agent, replaced in place, outside the activity trail: they are diagnosis,
  not record. Migration 0091 adds the table and a partial index over the
  completions that hold.
