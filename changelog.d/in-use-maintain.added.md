- **`athena-maintain` removes rows that have no further use (H20).** Read
  notifications 90 days after they were read, browser sessions past their
  expiry and idempotency receipts past theirs; it prints what it removed,
  takes `--dry-run`, and is safe beside a running Athena. The activity trail
  is never pruned. `docs/OPERATIONS.md` gains "Growth and Retention".
