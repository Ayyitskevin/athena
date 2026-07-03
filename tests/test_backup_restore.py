"""Tests for operator database backup and restore commands."""

import os
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


def _migration_count(path):
    conn = sqlite3.connect(path)
    count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    conn.close()
    return count


def _write_retained_file(path, *, mtime_ns):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"old backup")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


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


def test_backup_cli_prunes_old_retained_snapshots(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db", email="retained@example.com")
    backup_dir = tmp_path / "backups"
    oldest = _write_retained_file(
        backup_dir / "athena-2026-01-01.db",
        mtime_ns=1_000,
    )
    kept_old = _write_retained_file(
        backup_dir / "athena-2026-01-02.db",
        mtime_ns=2_000,
    )
    unrelated = _write_retained_file(
        backup_dir / "other-2026-01-01.db",
        mtime_ns=500,
    )
    snapshot = backup_dir / "athena-2026-01-03.db"

    assert ops.backup_main([str(source), str(snapshot), "--keep", "2"]) == 0

    out = capsys.readouterr()
    assert "Pruned 1 old backup(s) matching 'athena-*.db'; kept newest 2" in out.out
    assert not oldest.exists()
    assert kept_old.exists()
    assert unrelated.exists()
    assert _database_summary(snapshot)["email"] == "retained@example.com"


def test_backup_cli_validates_retention_before_writing_snapshot(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    snapshot = tmp_path / "manual.db"

    assert ops.backup_main([str(source), str(snapshot), "--keep", "2"]) == 1

    out = capsys.readouterr()
    assert "backup path name must match retention glob" in out.err
    assert not snapshot.exists()


def test_backup_cli_retention_glob_requires_keep(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    snapshot = tmp_path / "athena-2026-01-01.db"

    assert (
        ops.backup_main(
            [str(source), str(snapshot), "--retention-glob", "athena-*.db"]
        )
        == 1
    )

    out = capsys.readouterr()
    assert "--retention-glob requires --keep" in out.err
    assert not snapshot.exists()


def test_doctor_cli_checks_database_and_attachment_dir(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db", email="doctor@example.com")
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()

    assert ops.doctor_main([str(source), "--attach-dir", str(attach_dir)]) == 0

    out = capsys.readouterr()
    assert "database: ok" in out.out
    assert "attachments: ok" in out.out
    assert "athena-doctor: ok" in out.out


def test_doctor_cli_refuses_unmigrated_database(tmp_path, capsys):
    source = tmp_path / "empty.db"
    sqlite3.connect(source).close()

    assert ops.doctor_main([str(source)]) == 1

    out = capsys.readouterr()
    assert "schema_migrations is missing" in out.err


def test_doctor_cli_can_migrate_fresh_database(tmp_path, capsys):
    source = tmp_path / "fresh.db"

    assert ops.doctor_main([str(source), "--migrate"]) == 0

    out = capsys.readouterr()
    assert "database: ok" in out.out
    assert "applied " in out.out
    assert _migration_count(source) > 0


def test_doctor_cli_rejects_attachment_path_that_is_not_directory(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    attach_dir = tmp_path / "attachments"
    attach_dir.write_text("not a directory")

    assert ops.doctor_main([str(source), "--attach-dir", str(attach_dir)]) == 1

    out = capsys.readouterr()
    assert "attachment path is not a directory" in out.err
