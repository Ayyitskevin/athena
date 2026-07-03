"""Migrations apply atomically — a partial failure rolls back and never wedges startup.

The runner used to executescript() each file in autocommit, so a multi-statement
migration that failed on statement K left statements 1..K-1 durably committed but no
schema_migrations row — and the next boot re-ran it from the top and died on e.g.
"table already exists". These tests pin the fix: a failing migration leaves NO trace
(no half-applied schema, no version row), earlier migrations in the same run stay
applied, and a subsequent corrected run succeeds instead of wedging.
"""
import sqlite3

import pytest

from athena.core import db


def _write(dir_, name, sql):
    (dir_ / name).write_text(sql)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _applied_versions(conn):
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}


def test_a_partial_migration_rolls_back_wholesale(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "0001_good.sql", "CREATE TABLE good (id INTEGER PRIMARY KEY);")
    # 0002's first statement succeeds, its second raises — the classic partial failure.
    _write(
        migrations,
        "0002_bad.sql",
        "CREATE TABLE half (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO does_not_exist (x) VALUES (1);\n",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    conn = db.connect(tmp_path / "atomic.db")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)

    # 0001 committed atomically before 0002 was attempted; 0002 left NO trace.
    assert _table_exists(conn, "good")
    assert not _table_exists(conn, "half")  # the first statement rolled back with the rest
    assert _applied_versions(conn) == {"0001_good.sql"}  # only the successful one recorded


def test_a_fixed_migration_reruns_cleanly_instead_of_wedging(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "0001_good.sql", "CREATE TABLE good (id INTEGER PRIMARY KEY);")
    bad = migrations / "0002_bad.sql"
    bad.write_text(
        "CREATE TABLE half (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO does_not_exist (x) VALUES (1);\n"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    conn = db.connect(tmp_path / "wedge.db")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)

    # The operator fixes 0002 and reboots. Under the old autocommit runner, `half` would
    # already exist from the partial run, so this CREATE TABLE would die — a permanent
    # wedge. With atomic migrations, the partial run left nothing, so it applies cleanly.
    bad.write_text("CREATE TABLE half (id INTEGER PRIMARY KEY);\n")
    applied = db.migrate(conn)

    assert applied == ["0002_bad.sql"]  # only the still-pending one runs
    assert _table_exists(conn, "half")
    assert _applied_versions(conn) == {"0001_good.sql", "0002_bad.sql"}
