- **A database refusal at start names what is wrong and what to do, safely.**
  Each refusal `athena-serve` and `athena-doctor` print about the database
  stays one line and still refuses, and now names the database file, the
  object (the missing, future or mismatched migration file; each missing,
  unexpected or changed table, index, trigger or view; the migration that
  failed) and the remedy. A command is printed only where it runs exactly as
  printed and does what the line says; otherwise the line states the
  condition and names the runbook section. `athena-doctor <db> --migrate` for
  missing migrations or a ledger without checksums; for a newer, mismatched,
  drifted or damaged database, stop Athena first and keep a file copy of the
  database with its `-wal` and `-shm` files, then the runbook's "Restore a
  matched pair", never a bare `--force`; an object Athena does not make is
  named, not answered with a restore; a wrong `ATHENA_DB` is named first and
  no path is invented; a bootstrap is an `export` of the token and a start
  run in one shell, so that shell can create the first user; a start carries
  the refused start's `--host` and `--port`; a `chmod u+w` names only the file
  or directory a write would fail on; a path holding a control character gets
  no command. A file that is not a database, a malformed one, a path under a
  missing directory or a file, a symbolic link into a missing directory and a
  database path that is a directory each have their own line. `athena-serve`
  now refuses a database file or data directory the account it runs as could
  not write (judged by what a write would meet, so a root-run instance with a
  0400 file still starts), which before passed preflight and failed every
  write; `athena-doctor --deployment` warns about it, with its exit code
  unchanged, since the account running it may be an operator account. Names
  and paths are printed with control characters escaped. `OPERATIONS.md` has
  a new section, Startup Refusals, that lists every one.
