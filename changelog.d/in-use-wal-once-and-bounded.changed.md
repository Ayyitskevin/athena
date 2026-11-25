- **The journal mode is set once per database, and the write-ahead log has a
  size limit (H9).** A connection reads two bytes of the database file's
  header to see that it is already in WAL mode and no longer asks SQLite for
  it each time: opening a connection went from 0.31 ms to 0.04 ms on an
  empty database. Every connection sets `journal_size_limit` to 32 MiB, so
  `athena.db-wal` is cut back after a burst of writes where it kept its
  largest size for good.
