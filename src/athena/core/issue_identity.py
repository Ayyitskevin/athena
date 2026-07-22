"""Monotonic identities for issue rows whose activity outlives live state."""

from __future__ import annotations

import sqlite3


def allocate_issue_id(conn: sqlite3.Connection) -> int | None:
    """Reserve and return the next issue id inside the caller's transaction.

    Updating before reading acquires SQLite's writer reservation, so concurrent
    direct callers cannot observe the same value. A rollback also rolls back the
    allocation; committed ids are retained by migration-owned reservation rows.

    None is the compatibility signal for a database that has not reached
    migration 0055 yet: callers pass it to SQLite's INTEGER PRIMARY KEY and use
    lastrowid. A database recorded as migrated must never fall back, because that
    would make a damaged sequence silently reuse historical identities.
    """
    sequence_exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'issue_activity_id_sequence'"
    ).fetchone()
    if sequence_exists is None:
        migrations_exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        migrated = (
            migrations_exists is not None
            and conn.execute(
                "SELECT 1 FROM schema_migrations "
                "WHERE version = '0055_issue_lifecycle_facts.sql'"
            ).fetchone()
            is not None
        )
        if migrated:
            raise RuntimeError("issue activity id sequence is unavailable")
        return None

    updated = conn.execute(
        "UPDATE issue_activity_id_sequence SET next_id = next_id + 1 "
        "WHERE singleton = 1"
    )
    if updated.rowcount != 1:
        raise RuntimeError("issue activity id sequence is unavailable")
    row = conn.execute(
        "SELECT next_id - 1 AS issue_id FROM issue_activity_id_sequence "
        "WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("issue activity id sequence is unavailable")
    return int(row["issue_id"])
