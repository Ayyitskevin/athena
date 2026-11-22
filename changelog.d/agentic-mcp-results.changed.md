- **MCP tools answer one JSON object, within a size budget.** A list tool
  returns `{items, count, truncated, next}` instead of one block per row, and
  list and search rows no longer carry bodies. Every tool in the `seat`,
  `seat+knowledge` and `conductor` profiles answers within 16,000 characters
  (`get_issue_work_context` 32,000); a cut answer says so and names the call
  that reads the rest. Every tool carries read-only, destructive and idempotent
  hints and an output schema.
