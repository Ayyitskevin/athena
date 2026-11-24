- **A bearer read no longer takes the writer lock, and a lock timeout is not
  a 500 (H7).** A token's `last_used_at` is stamped at most once a minute; a
  due stamp waits a quarter of a second for a busy writer and is then skipped,
  so authenticating never fails or stalls on a bookkeeping write. A statement
  that waits five seconds for the write lock answers `503 busy` with
  `Retry-After`, where it was an unhandled error; nothing it would have
  written was applied.
