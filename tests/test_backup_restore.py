"""Tests for operator database backup and restore commands."""

import sqlite3

import pytest

from athena import ops
from athena.core import backup, db


def _seed_database(path, *, email="admin@example.com", issue="Saved issue"):
    conn = db.connect(path)
    db.migrate(conn)
    cur = conn.execute(
        "INSERT INTO users (email, name) VALUES (?, ?)",
        (email, "Admin"),
    )
    conn.execute(
        "INSERT INTO issues (title, created_by) VALUES (?, ?)",
        (issue, cur.lastrowid),
    )
    conn.commit()
    conn.close()
    return path


def _database_summary(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT users.email, issues.title "
        "FROM users JOIN issues ON issues.created_by = users.id "
        "ORDER BY issues.id"
    ).fetchone()
    migrations = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    conn.close()
    return {"email": row["email"], "issue": row["title"], "migrations": migrations}


def test_backup_database_copies_schema_and_data(tmp_path):
    source = _seed_database(tmp_path / "athena.db")
    snapshot = tmp_path / "snapshots" / "athena.db"
    source_summary = _database_summary(source)

    assert backup.backup_database(source, snapshot) == snapshot

    assert snapshot.exists()
    assert source_summary["migrations"] > 0
    assert _database_summary(snapshot) == source_summary


def test_backup_database_refuses_to_overwrite_without_flag(tmp_path):
    source = _seed_database(tmp_path / "athena.db")
    snapshot = backup.backup_database(source, tmp_path / "athena.backup.db")

    with pytest.raises(FileExistsError, match="backup path already exists"):
        backup.backup_database(source, snapshot)

    for suffix in ("-wal", "-shm"):
        snapshot.with_name(f"{snapshot.name}{suffix}").write_bytes(b"stale")
    backup.backup_database(source, snapshot, overwrite=True)
    assert _database_summary(snapshot)["email"] == "admin@example.com"
    for suffix in ("-wal", "-shm"):
        assert not snapshot.with_name(f"{snapshot.name}{suffix}").exists()


def test_restore_database_refuses_existing_target_without_force(tmp_path):
    source = _seed_database(
        tmp_path / "source.db",
        email="saved@example.com",
        issue="Saved issue",
    )
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = _seed_database(
        tmp_path / "target.db",
        email="stale@example.com",
        issue="Stale issue",
    )
    for suffix in ("-wal", "-shm"):
        target.with_name(f"{target.name}{suffix}").write_bytes(b"stale")

    with pytest.raises(FileExistsError, match="target database already exists"):
        backup.restore_database(snapshot, target)

    assert backup.restore_database(snapshot, target, force=True) == target
    for suffix in ("-wal", "-shm"):
        assert not target.with_name(f"{target.name}{suffix}").exists()
    assert _database_summary(target) == {
        "email": "saved@example.com",
        "issue": "Saved issue",
        "migrations": _database_summary(source)["migrations"],
    }


def test_backup_and_restore_cli_entry_points(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db", email="cli@example.com")
    snapshot = tmp_path / "athena.snapshot.db"
    restored = tmp_path / "restored.db"

    assert ops.backup_main([str(source), str(snapshot)]) == 0
    out = capsys.readouterr()
    assert "Backed up" in out.out

    assert ops.restore_main([str(snapshot), str(restored)]) == 0
    out = capsys.readouterr()
    assert "Restored" in out.out
    assert _database_summary(restored)["email"] == "cli@example.com"
