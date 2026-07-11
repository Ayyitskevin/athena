"""Database access for Athena.

One SQLite file, opened in WAL mode, with a tiny forward-only migration runner.
Every other part of the app gets its connection from here, so the connection
settings (and the schema) live in exactly one place.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
import sqlite3
from pathlib import Path
from typing import Iterator

# The .sql migration files live next to this module, in migrations/.
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the settings Athena always wants."""
    # FastAPI may enter a synchronous yield dependency and run its endpoint/exit
    # on different worker threads. A request still uses this connection serially,
    # but SQLite's default creator-thread affinity would turn that legitimate handoff
    # into a production-only 500 (TestClient does not reproduce the scheduler hop).
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row              # rows act like dicts: row["email"]
    conn.execute("PRAGMA journal_mode = WAL")   # readers don't block the writer
    conn.execute("PRAGMA foreign_keys = ON")    # actually enforce REFERENCES
    # WAL lets readers and ONE writer coexist, but a second writer still needs the
    # write lock — and without a busy timeout SQLite gives up instantly with
    # "database is locked" (a 500) instead of waiting its turn. Wait up to 5s so
    # concurrent actors queue behind each other rather than erroring out.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(
    conn: sqlite3.Connection, *, immediate: bool = False
) -> Iterator[None]:
    """Run a group of writes as one atomic unit.

    Request handlers normally arrive with a fresh connection, so the outer form
    opens a real transaction and owns the final commit/rollback. A command may
    also be composed inside an existing transaction (imports and future compound
    commands need that), in which case a savepoint gives it the same all-or-nothing
    boundary without committing the caller's work.

    ``immediate`` acquires SQLite's single-writer reservation before any read used
    to validate the command. That closes the check-then-write race for commands
    whose authorization or audit detail depends on the current row. When composing
    inside an existing transaction, the caller owns its transaction mode; an outer
    writer must already have acquired the reservation if it needs that guarantee.

    Functions called inside this context must not commit independently; their
    transaction-aware variants receive ``commit=False`` from the command owner.
    """
    nested = conn.in_transaction
    savepoint = "athena_command"
    if nested:
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
    except BaseException:
        if nested and conn.in_transaction:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                # If finalizing destroyed/invalidated the savepoint, prefer a clean
                # connection over leaving the caller in a poisoned transaction.
                with suppress(sqlite3.Error):
                    conn.rollback()
        else:
            with suppress(sqlite3.Error):
                conn.rollback()
        raise


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every migration that hasn't run yet, in filename order.

    Returns the list of migrations applied this call (empty if already current).
    Safe to run on every startup: it only applies what's missing.
    """
    # A table that records which migrations have run. This is how the runner
    # "remembers" — without it, we'd re-apply everything every time.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()

    already = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}

    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.name
        if version in already:
            continue
        # Apply the migration AND record its version as one atomic unit. Without this,
        # executescript runs each statement in autocommit (SQLite has no implicit
        # transaction around a script, and no migration wraps itself in BEGIN/COMMIT),
        # so a multi-statement migration that failed partway left earlier statements
        # durably committed but no schema_migrations row — and the next boot re-ran it
        # from the top and wedged on e.g. "duplicate column name". Wrapping the file's
        # statements plus the version insert in one BEGIN/COMMIT makes it all-or-nothing:
        # a partial failure rolls back cleanly and the version is recorded only when the
        # whole file succeeded. (Safe here because no migration uses PRAGMA or its own
        # transaction control, which don't compose with an enclosing transaction.) The
        # version literal is a controlled filename; quotes are escaped defensively.
        version_literal = version.replace("'", "''")
        script = (
            "BEGIN;\n"
            + path.read_text()
            + "\n;\n"
            + f"INSERT INTO schema_migrations (version) VALUES ('{version_literal}');\n"
            + "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            # Discard the partial (uncommitted) transaction so a retry starts clean and
            # the half-applied schema never persists.
            conn.rollback()
            raise
        applied.append(version)
    return applied
