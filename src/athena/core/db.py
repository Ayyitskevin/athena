"""Database access for Athena.

One SQLite file, opened in WAL mode, with a tiny forward-only migration runner.
Every other part of the app gets its connection from here, so the connection
settings (and the schema) live in exactly one place.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# The .sql migration files live next to this module, in migrations/.
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the settings Athena always wants."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row              # rows act like dicts: row["email"]
    conn.execute("PRAGMA journal_mode = WAL")   # readers don't block the writer
    conn.execute("PRAGMA foreign_keys = ON")    # actually enforce REFERENCES
    return conn


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
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        conn.commit()
        applied.append(version)
    return applied
