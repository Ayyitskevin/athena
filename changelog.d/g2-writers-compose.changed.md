- **Every write takes `commit` or goes through `db.transaction`, and the
  transaction check fails on any new one that does not (G2).** Twenty-two
  writers ended in their own `conn.commit()` whatever their caller was doing,
  and four opened `BEGIN IMMEDIATE` by hand (`start_sprint`, `update_page`,
  `move`, `delete_page`), as did the import replay. Each now writes through
  `db.transaction`, with `immediate=True` where the hand-opened transaction
  serialized a check and its write, so it commits on its own as before and is a
  savepoint inside a caller's transaction instead of committing that
  transaction's first half. Rows, audit events and answers are unchanged.
  `scripts/check_transaction_commits.py` now also fails, outside `core/db.py`,
  on a commit no `commit` parameter governs and on `execute("BEGIN ...")`,
  `SAVEPOINT`, `RELEASE`, `ROLLBACK` or `COMMIT` written by hand. Two functions
  are allowed to manage their own transaction, each with its reason in the
  script: the activity recorder, which hands an open writer reservation back to
  its command, and the export's read snapshot, which always rolls back. A stale
  entry fails.
