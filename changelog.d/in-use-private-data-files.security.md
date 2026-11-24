- **A database Athena creates is readable only by the service user.**
  `athena-serve --bootstrap` and `athena-doctor --migrate` create a missing
  database file `0600` (the target of a dangling symlink too), and a missing
  data directory `0700`, whatever the process umask; before, the documented
  bootstrap left them `0644` and `0755`, readable by every local account,
  while the database holds password hashes, CSRF tokens and webhook secrets.
  SQLite's `-wal` and `-shm` files take the database file's mode. An existing
  file or directory is not changed: `athena-doctor` warns on one open to group
  or others, and prints the `chmod` only where running it is safe and does
  what the line says (a regular file the caller owns, in a directory of the
  caller's own that is neither sticky nor world-writable, neither of them
  group-writable; the directory only when it holds nothing but Athena's
  entries, matched by exact name), otherwise it states the condition. The bootstrap block in
  `OPERATIONS.md` now creates `/var/lib/athena` itself `0700`.
