"""WHY tests for fail-closed migration inventory and ledger integrity."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena import ops
from athena.core import db
from athena.main import create_app


def _write_migration(directory: Path, name: str, sql: str) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(sql)
    return path


def _schema_has_checksum(conn: sqlite3.Connection) -> bool:
    return "checksum" in {
        row["name"]
        for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
    }


def test_fresh_and_current_database_store_every_packaged_checksum(
    tmp_path, monkeypatch
):
    # WHY: a filename-only ledger cannot distinguish a reviewed migration from
    # changed SQL carrying the same name. Fresh installs must pin every file's bytes.
    migrations = tmp_path / "migrations"
    first = _write_migration(
        migrations, "0001_first.sql", "CREATE TABLE first (id INTEGER PRIMARY KEY);"
    )
    second = _write_migration(
        migrations,
        "0002_second.sql",
        "CREATE TABLE second (id INTEGER PRIMARY KEY);",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    conn = db.connect(tmp_path / "fresh.db")
    try:
        assert db.migrate(conn) == [first.name, second.name]
        rows = {
            row["version"]: row["checksum"]
            for row in conn.execute(
                "SELECT version, checksum FROM schema_migrations"
            ).fetchall()
        }
        assert rows == {
            first.name: hashlib.sha256(first.read_bytes()).hexdigest(),
            second.name: hashlib.sha256(second.read_bytes()).hexdigest(),
        }

        status = db.migration_status(conn)
        assert status.current
        assert status.required == (first.name, second.name)
        assert status.applied == status.required
        assert status.pending == ()
        assert db.migrate(conn) == []
    finally:
        conn.close()


def test_legacy_checksumless_ledger_is_backfilled_once(tmp_path, monkeypatch):
    # WHY: existing deployments predate checksum storage. Their known rows get one
    # atomic trust-on-upgrade backfill; subsequent checks use the pinned checksum.
    migrations = tmp_path / "migrations"
    migration = _write_migration(
        migrations,
        "0001_legacy.sql",
        "CREATE TABLE legacy_data (id INTEGER PRIMARY KEY);",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    conn = db.connect(tmp_path / "legacy.db")
    try:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)")
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            (migration.name,),
        )
        conn.commit()

        assert db.migrate(conn) == []
        assert _schema_has_checksum(conn)
        expected = hashlib.sha256(migration.read_bytes()).hexdigest()
        stored = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            (migration.name,),
        ).fetchone()["checksum"]
        assert stored == expected
        assert db.migrate(conn) == []
        assert db.migration_status(conn).current
    finally:
        conn.close()


def test_changed_packaged_migration_is_rejected_before_any_new_work(
    tmp_path, monkeypatch
):
    # WHY: editing an already-applied migration rewrites history. Even a comment-only
    # mutation must stop startup instead of silently accepting the changed artifact.
    migrations = tmp_path / "migrations"
    migration = _write_migration(
        migrations, "0001_fixed.sql", "CREATE TABLE fixed (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    conn = db.connect(tmp_path / "mutated.db")
    try:
        db.migrate(conn)
        original_checksum = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            (migration.name,),
        ).fetchone()["checksum"]
        migration.write_text(migration.read_text() + "\n-- changed after apply\n")

        with pytest.raises(db.MigrationIntegrityError, match="checksum mismatch"):
            db.migration_status(conn)
        with pytest.raises(db.MigrationIntegrityError, match="checksum mismatch"):
            db.migrate(conn)
        assert (
            conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?",
                (migration.name,),
            ).fetchone()["checksum"]
            == original_checksum
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("version", "message"),
    [
        ("not-a-version", "unknown applied migration"),
        ("0002_future.sql", "future migration"),
    ],
)
def test_unknown_and_future_applied_rows_are_rejected(
    tmp_path, monkeypatch, version, message
):
    # WHY: a database from an unknown or newer code line must never be served by
    # this binary; pretending it is current risks interpreting the wrong schema.
    migrations = tmp_path / "migrations"
    _write_migration(
        migrations, "0001_known.sql", "CREATE TABLE known (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / f"{version}.db")
    try:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, checksum) VALUES (?, ?)",
            (version, "0" * 64),
        )
        conn.commit()

        with pytest.raises(db.MigrationIntegrityError, match=message):
            db.migration_status(conn)
    finally:
        conn.close()


def test_legacy_backfill_refuses_unknown_history_before_altering_table(
    tmp_path, monkeypatch
):
    # WHY: checksum backfill necessarily trusts known legacy rows once. An unknown
    # row must stop that upgrade before the ledger is altered or falsely blessed.
    migrations = tmp_path / "migrations"
    _write_migration(
        migrations, "0001_known.sql", "CREATE TABLE known (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "legacy-unknown.db")
    try:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES ('0002_future.sql')"
        )
        conn.commit()

        with pytest.raises(db.MigrationIntegrityError, match="future migration"):
            db.migrate(conn)
        assert not _schema_has_checksum(conn)
    finally:
        conn.close()


def test_missing_applied_row_is_reported_as_incomplete(tmp_path, monkeypatch):
    # WHY: readiness must be based on the complete ledger, not merely the presence
    # of one row. A deleted entry makes the schema history unprovable.
    migrations = tmp_path / "migrations"
    first = _write_migration(
        migrations, "0001_first.sql", "CREATE TABLE first (id INTEGER PRIMARY KEY);"
    )
    second = _write_migration(
        migrations,
        "0002_second.sql",
        "CREATE TABLE second (id INTEGER PRIMARY KEY);",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "incomplete.db")
    try:
        db.migrate(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (second.name,))
        conn.commit()

        status = db.migration_status(conn, require_current=False)
        assert status.applied == (first.name,)
        assert status.pending == (second.name,)
        assert not status.current
        with pytest.raises(db.MigrationIntegrityError, match="missing migrations"):
            db.migration_status(conn)
    finally:
        conn.close()


def test_applied_row_without_its_packaged_file_is_rejected(tmp_path, monkeypatch):
    # WHY: replacing a migration with another file at the same sequence is history
    # loss, even when the replacement happens to contain identical SQL bytes.
    migrations = tmp_path / "migrations"
    _write_migration(
        migrations, "0001_first.sql", "CREATE TABLE first (id INTEGER PRIMARY KEY);"
    )
    second = _write_migration(
        migrations,
        "0002_original.sql",
        "CREATE TABLE second (id INTEGER PRIMARY KEY);",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "missing-package.db")
    try:
        db.migrate(conn)
        second.rename(migrations / "0002_replacement.sql")

        with pytest.raises(db.MigrationIntegrityError, match="not packaged"):
            db.migration_status(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (("bad.sql",), "invalid migration inventory entry"),
        (("0001_one.sql", "0001_two.sql"), "duplicate migration sequence"),
        (("0001_one.sql", "0003_three.sql"), "missing sequence"),
    ],
)
def test_invalid_packaged_inventory_fails_before_database_mutation(
    tmp_path, monkeypatch, names, message
):
    # WHY: the package itself is part of the trust boundary. Gaps, malformed names,
    # or duplicate sequence numbers must fail before creating even the ledger table.
    migrations = tmp_path / "migrations"
    for index, name in enumerate(names):
        _write_migration(
            migrations, name, f"CREATE TABLE table_{index} (id INTEGER PRIMARY KEY);"
        )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "invalid-inventory.db")
    try:
        with pytest.raises(db.MigrationIntegrityError, match=message):
            db.migrate(conn)
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_doctor_uses_shared_checksum_check(tmp_path, monkeypatch, capsys):
    # WHY: an operator check that disagrees with startup/readiness can authorize a
    # broken deploy. Doctor must reject the exact drift the migration runner rejects.
    migrations = tmp_path / "migrations"
    migration = _write_migration(
        migrations, "0001_doctor.sql", "CREATE TABLE doctor (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    db_path = tmp_path / "doctor.db"
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    migration.write_text(migration.read_text() + "\n-- drift\n")

    assert ops.doctor_main([str(db_path)]) == 1
    output = capsys.readouterr()
    assert "migration checksum mismatch" in output.err
    assert "athena-doctor: ok" not in output.out


def test_readyz_rejects_checksum_drift_without_leaking_details(tmp_path, monkeypatch):
    # WHY: orchestration needs a hard 503 on drift, while the public probe must not
    # reveal migration filenames or filesystem/database internals.
    migrations = tmp_path / "migrations"
    migration = _write_migration(
        migrations, "0001_ready.sql", "CREATE TABLE ready (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    with TestClient(create_app(tmp_path / "ready.db")) as client:
        migration.write_text(migration.read_text() + "\n-- drift\n")
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
    assert migration.name not in response.text


def test_readyz_rejects_an_incomplete_applied_ledger(tmp_path, monkeypatch):
    # WHY: checking only for one migration row reports healthy after a ledger row
    # is lost. Readiness must prove every packaged version is represented.
    migrations = tmp_path / "migrations"
    _write_migration(
        migrations, "0001_first.sql", "CREATE TABLE first (id INTEGER PRIMARY KEY);"
    )
    second = _write_migration(
        migrations,
        "0002_second.sql",
        "CREATE TABLE second (id INTEGER PRIMARY KEY);",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    db_path = tmp_path / "ready-incomplete.db"

    with TestClient(create_app(db_path)) as client:
        conn = db.connect(db_path)
        try:
            conn.execute(
                "DELETE FROM schema_migrations WHERE version = ?", (second.name,)
            )
            conn.commit()
        finally:
            conn.close()
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}


def test_lifespan_closes_startup_connection_when_migration_check_raises(
    tmp_path, monkeypatch
):
    # WHY: fail-closed startup still has to release SQLite locks and descriptors.
    # A drift exception must not strand the connection opened before migrate().
    migrations = tmp_path / "migrations"
    migration = _write_migration(
        migrations,
        "0001_startup.sql",
        "CREATE TABLE startup (id INTEGER PRIMARY KEY);",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    db_path = tmp_path / "startup-drift.db"
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    migration.write_text(migration.read_text() + "\n-- drift\n")

    opened: list[sqlite3.Connection] = []
    real_connect = db.connect

    def tracking_connect(path: str | Path) -> sqlite3.Connection:
        tracked = real_connect(path)
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(db, "connect", tracking_connect)
    with pytest.raises(db.MigrationIntegrityError, match="checksum mismatch"):
        with TestClient(create_app(db_path)):
            pass

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_legacy_backfill_refuses_an_unrecognized_ledger_schema(tmp_path, monkeypatch):
    # WHY: ALTERing a lookalike table with extra state could bless an unrelated or
    # corrupted ledger. Only the exact known legacy schema is safe to upgrade.
    migrations = tmp_path / "migrations"
    _write_migration(
        migrations, "0001_known.sql", "CREATE TABLE known (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "legacy-invalid-schema.db")
    try:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "unexpected TEXT)"
        )
        conn.commit()

        with pytest.raises(db.MigrationIntegrityError, match="invalid schema"):
            db.migrate(conn)
        assert not _schema_has_checksum(conn)
    finally:
        conn.close()


def test_nonprefix_applied_history_is_rejected_even_when_pending_is_allowed(
    tmp_path, monkeypatch
):
    # WHY: applying an earlier migration after a later one violates schema ordering.
    # A missing middle/prefix row is corruption, not ordinary pending work.
    migrations = tmp_path / "migrations"
    first = _write_migration(
        migrations, "0001_first.sql", "CREATE TABLE first (id INTEGER PRIMARY KEY);"
    )
    _write_migration(
        migrations,
        "0002_second.sql",
        "CREATE TABLE second (id INTEGER PRIMARY KEY);",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "nonprefix.db")
    try:
        db.migrate(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (first.name,))
        conn.commit()

        with pytest.raises(db.MigrationIntegrityError, match="contiguous"):
            db.migration_status(conn, require_current=False)
        with pytest.raises(db.MigrationIntegrityError, match="contiguous"):
            db.migrate(conn)
    finally:
        conn.close()


def test_symlinked_migration_is_rejected_before_database_mutation(
    tmp_path, monkeypatch
):
    # WHY: a symlink can change migration bytes outside the packaged inventory.
    # Migration trust is restricted to regular files contained in the package.
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    external = tmp_path / "external.sql"
    external.write_text("CREATE TABLE escaped (id INTEGER PRIMARY KEY);")
    (migrations / "0001_escaped.sql").symlink_to(external)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    conn = db.connect(tmp_path / "symlink.db")
    try:
        with pytest.raises(db.MigrationIntegrityError, match="invalid migration"):
            db.migrate(conn)
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
