"""A test database is copied from one migrated template, and is the same database.

WHY: an empty database file meant every migration again, 0.18 s of the 0.22 s
an app took to start, some seven thousand times a run (K1.2 of
docs/plans/CONSOLIDATION_SPEC.md). tests/conftest.py now fills an empty
database from a template the worker migrated once. That is only sound if the
copy is the database the migrations would have built, and if anything that is
not an empty database with the packaged inventory still takes the real
migrations. Both halves are held here.
"""

from __future__ import annotations

import sqlite3

import conftest

from athena.core import db


def _schema(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
    ]


def _rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every table's rows, without the one column that records when."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    found: dict[str, list[tuple]] = {}
    for table in tables:
        try:
            columns = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info('{table}')")
                if row[1] != "applied_at"
            ]
            listed = ", ".join(f'"{column}"' for column in columns)
            found[table] = [
                tuple(row)
                for row in conn.execute(f'SELECT {listed} FROM "{table}" ORDER BY 1')
            ]
        except sqlite3.OperationalError:
            # A virtual table's shadow tables are read through the table itself.
            continue
    return found


def test_a_copied_database_is_the_migrated_database(tmp_path):
    migrated = db.connect(tmp_path / "migrated.db")
    first = db.connect(tmp_path / "first.db")
    copied = db.connect(tmp_path / "copied.db")
    try:
        applied = conftest._real_migrate(migrated)
        # Whichever test ran first in this worker built the template; if none
        # has, this call does. Either way the database after it is a copy.
        assert conftest._migrate_from_template(first) == applied
        assert conftest._migrate_from_template(copied) == applied
        assert len(applied) == len(db._migration_inventory())
        assert _schema(copied) == _schema(migrated)
        assert _rows(copied) == _rows(migrated)
        for pragma in ("journal_mode", "foreign_keys", "user_version"):
            assert (
                copied.execute(f"PRAGMA {pragma}").fetchone()[0]
                == migrated.execute(f"PRAGMA {pragma}").fetchone()[0]
            ), pragma
        # The ledger of the copy is the one production validates on every start.
        assert db.migration_status(copied) == db.migration_status(migrated)
        assert conftest._real_migrate(copied) == []
    finally:
        migrated.close()
        first.close()
        copied.close()


def test_a_database_that_is_not_empty_takes_the_real_migrations(tmp_path, monkeypatch):
    # An upgrade, a restore or a half-built fixture is never overwritten by a
    # copy: only a database with no object at all is filled from the template.
    calls: list[str] = []
    real = conftest._real_migrate

    def counted(conn):
        calls.append("real")
        return real(conn)

    monkeypatch.setattr(conftest, "_real_migrate", counted)
    conn = db.connect(tmp_path / "has-a-table.db")
    try:
        conn.execute("CREATE TABLE mine (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO mine (note) VALUES ('kept')")
        conn.commit()
        applied = conftest._migrate_from_template(conn)
        assert calls == ["real"]
        assert len(applied) == len(db._migration_inventory())
        assert conn.execute("SELECT note FROM mine").fetchone()[0] == "kept"
    finally:
        conn.close()


def test_a_test_with_its_own_inventory_gets_that_inventory(
    tmp_path, migration_inventory_through
):
    # The template is kept per inventory: a test that limits the migrations to
    # an older schema gets that schema, not the newest one's copy.
    migration_inventory_through("0010_pages.sql")
    conn = db.connect(tmp_path / "older.db")
    try:
        applied = db.migrate(conn)
        assert applied[-1] == "0010_pages.sql"
        assert len(applied) == 10
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "pages" in tables
        assert "issue_leases" not in tables  # migration 0054, not in this inventory
    finally:
        conn.close()
    # And a second empty database under the same limited inventory is a copy
    # of the limited one.
    again = db.connect(tmp_path / "older-again.db")
    try:
        assert db.migrate(again) == applied
        assert "issue_leases" not in {
            row[0]
            for row in again.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        again.close()
