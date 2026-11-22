- **A refused write no longer leaves a half-recorded trail.** A project delete
  refused for holding issues still wrote a permanent `deleted_project` event,
  and a refused issue unlink removed the link with no event. Both writes now
  commit with their event or not at all. `db.transaction` raises if anything
  commits underneath it, and a gate stage (`check_transaction_commits.py`)
  fails on a committing call inside a transaction.
