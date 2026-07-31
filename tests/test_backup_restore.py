"""Tests for operator database backup and restore commands."""

import os
import sqlite3

import pytest

from athena import config, ops
from athena.core import attachments, backup, db, users


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


def test_backup_uses_private_unique_staging_file(tmp_path):
    source = _seed_database(tmp_path / "athena.db")
    snapshot = tmp_path / "athena.snapshot.db"
    fixed_temp_sentinel = tmp_path / f".{snapshot.name}.tmp"
    fixed_temp_sentinel.write_bytes(b"unrelated")

    backup.backup_database(source, snapshot)

    assert fixed_temp_sentinel.read_bytes() == b"unrelated"
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(f".{snapshot.name}.*.tmp"))


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


def test_restore_validates_backup_before_touching_target(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    target = _seed_database(tmp_path / "target.db", email="stale@example.com")
    original = target.read_bytes()
    sidecars = [
        target.with_name(f"{target.name}{suffix}") for suffix in ("-wal", "-shm")
    ]
    for sidecar in sidecars:
        sidecar.write_bytes(b"must-survive-validation")

    with pytest.raises(sqlite3.DatabaseError):
        backup.restore_database(corrupt, target, force=True)

    assert target.read_bytes() == original
    assert [sidecar.read_bytes() for sidecar in sidecars] == [
        b"must-survive-validation",
        b"must-survive-validation",
    ]


def test_restore_removes_stale_sidecars_before_atomic_swap(tmp_path, monkeypatch):
    source = _seed_database(tmp_path / "source.db", email="saved@example.com")
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = _seed_database(tmp_path / "target.db", email="stale@example.com")
    wal = target.with_name(f"{target.name}-wal")
    shm = target.with_name(f"{target.name}-shm")
    wal.write_bytes(b"stale-wal")
    shm.write_bytes(b"stale-shm")

    seen = {}
    real_replace = backup._replace_staged_database

    def _spy(staged, destination):
        seen["wal"] = wal.exists()
        seen["shm"] = shm.exists()
        return real_replace(staged, destination)

    monkeypatch.setattr(backup, "_replace_staged_database", _spy)
    backup.restore_database(snapshot, target, force=True)

    assert seen == {"wal": False, "shm": False}
    assert not wal.exists() and not shm.exists()
    assert _database_summary(target)["email"] == "saved@example.com"


def test_restore_recovers_existing_target_when_swap_fails(tmp_path, monkeypatch):
    source = _seed_database(tmp_path / "source.db", email="saved@example.com")
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = _seed_database(tmp_path / "target.db", email="stale@example.com")
    original_summary = _database_summary(target)
    real_replace = backup._replace_staged_database
    attempts = 0

    def _fail_once(staged, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected swap failure")
        return real_replace(staged, destination)

    monkeypatch.setattr(backup, "_replace_staged_database", _fail_once)
    with pytest.raises(OSError, match="injected swap failure"):
        backup.restore_database(snapshot, target, force=True)

    assert attempts == 2
    assert _database_summary(target) == original_summary
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_restore_retains_recovery_copy_when_automatic_recovery_fails(
    tmp_path, monkeypatch
):
    source = _seed_database(tmp_path / "source.db", email="saved@example.com")
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = _seed_database(tmp_path / "target.db", email="stale@example.com")

    def _always_fail(staged, destination):
        raise OSError("injected persistent swap failure")

    monkeypatch.setattr(backup, "_replace_staged_database", _always_fail)
    with pytest.raises(RuntimeError, match="consistent recovery copy remains"):
        backup.restore_database(snapshot, target, force=True)

    recovery_files = list(tmp_path.glob(f".{target.name}.*.tmp"))
    assert len(recovery_files) == 1
    assert _database_summary(recovery_files[0])["email"] == "stale@example.com"


def test_backup_rejects_missing_non_file_same_path_and_unsafe_retention(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="database path does not exist"):
        backup.backup_database(missing, tmp_path / "snapshot.db")

    source_directory = tmp_path / "source-directory"
    source_directory.mkdir()
    with pytest.raises(FileNotFoundError, match="database path is not a file"):
        backup.backup_database(source_directory, tmp_path / "snapshot.db")

    source = _seed_database(tmp_path / "source.db")
    with pytest.raises(ValueError, match="must be different"):
        backup.backup_database(source, source, overwrite=True)

    with pytest.raises(FileNotFoundError, match="backup directory does not exist"):
        backup.prune_backup_directory(tmp_path / "no-backups", "*.db", keep=1)
    not_directory = tmp_path / "not-a-directory"
    not_directory.write_bytes(b"file")
    with pytest.raises(NotADirectoryError, match="backup path is not a directory"):
        backup.prune_backup_directory(not_directory, "*.db", keep=1)

    with pytest.raises(ValueError, match="retention glob must not be empty"):
        backup.validate_retention_plan(tmp_path / "snapshot.db", "", keep=1)
    with pytest.raises(ValueError, match="without path separators"):
        backup.validate_retention_plan(tmp_path / "snapshot.db", "nested/*.db", keep=1)
    with pytest.raises(ValueError, match="keep must be at least 1"):
        backup.validate_retention_plan(tmp_path / "snapshot.db", "*.db", keep=0)


def test_restore_failure_without_existing_target_leaves_no_database(
    tmp_path, monkeypatch
):
    source = _seed_database(tmp_path / "source.db")
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = tmp_path / "new-target.db"

    def fail_swap(staged, destination):
        raise OSError("injected first-install swap failure")

    monkeypatch.setattr(backup, "_replace_staged_database", fail_swap)
    with pytest.raises(OSError, match="injected first-install swap failure"):
        backup.restore_database(snapshot, target)

    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_operator_cli_reports_missing_restore_and_attachment_paths(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    missing_attachments = tmp_path / "missing-attachments"

    assert ops.doctor_main([str(source), "--attach-dir", str(missing_attachments)]) == 1
    doctor_output = capsys.readouterr()
    assert "attachment directory does not exist" in doctor_output.err

    assert (
        ops.restore_main(
            [str(tmp_path / "missing-backup.db"), str(tmp_path / "restored.db")]
        )
        == 1
    )
    restore_output = capsys.readouterr()
    assert "database path does not exist" in restore_output.err


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
        ops.backup_main([str(source), str(snapshot), "--retention-glob", "athena-*.db"])
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


def test_doctor_reconciles_attachment_rows_and_blobs(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    attach_dir = tmp_path / "attachments"
    conn = db.connect(source)
    stored = attachments.store(
        conn,
        target_kind="issue",
        target_id=1,
        filename="evidence.txt",
        content_type="text/plain",
        data=b"evidence",
        uploaded_by=1,
        attach_dir=attach_dir,
    )
    conn.close()

    assert stored["sha256"]
    assert ops.doctor_main([str(source), "--attach-dir", str(attach_dir)]) == 0

    out = capsys.readouterr()
    assert "attachments: ok (1 blobs reconciled" in out.out


def test_doctor_fails_on_tampered_and_orphan_attachment_blobs(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    attach_dir = tmp_path / "attachments"
    conn = db.connect(source)
    stored = attachments.store(
        conn,
        target_kind="issue",
        target_id=1,
        filename="evidence.txt",
        content_type="text/plain",
        data=b"evidence",
        uploaded_by=1,
        attach_dir=attach_dir,
    )
    stored_name = attachments.get_stored_name(conn, stored["id"])
    assert stored_name is not None
    attachments.disk_path(attach_dir, stored_name).write_bytes(b"tampered")
    (attach_dir / "orphan.bin").write_bytes(b"orphan")
    conn.close()

    assert ops.doctor_main([str(source), "--attach-dir", str(attach_dir)]) == 1

    out = capsys.readouterr()
    assert "attachment integrity check failed" in out.err
    assert "tampered=1" in out.err
    assert "orphan_files=1" in out.err
    assert stored_name not in out.err


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


def test_doctor_deployment_mode_checks_exact_config_and_admin_recovery(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = db.connect(source)
    db.migrate(conn)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", source)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)

    assert (
        ops.doctor_main(
            [
                str(source),
                "--attach-dir",
                str(attach_dir),
                "--deployment",
            ]
        )
        == 0
    )

    out = capsys.readouterr()
    assert "administrator recovery: ok (1 active)" in out.out
    assert "network: ok (local; 127.0.0.1:8000; 2 Host authorities)" in out.out


def test_doctor_deployment_mode_refuses_a_different_configured_database(
    tmp_path, monkeypatch, capsys
):
    inspected = tmp_path / "inspected.db"
    configured = tmp_path / "configured.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    conn = db.connect(inspected)
    db.migrate(conn)
    users.create_user(
        conn,
        email="admin@example.com",
        name="Admin",
        password="password",
        role="admin",
    )
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", configured)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)

    assert (
        ops.doctor_main(
            [
                str(inspected),
                "--attach-dir",
                str(attach_dir),
                "--deployment",
            ]
        )
        == 1
    )

    assert "must exactly match ATHENA_DB" in capsys.readouterr().err


def test_doctor_cli_rejects_attachment_path_that_is_not_directory(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    attach_dir = tmp_path / "attachments"
    attach_dir.write_text("not a directory")

    assert ops.doctor_main([str(source), "--attach-dir", str(attach_dir)]) == 1

    out = capsys.readouterr()
    assert "attachment path is not a directory" in out.err


def test_doctor_rejects_symlinked_attachment_root_before_probe(tmp_path, capsys):
    source = _seed_database(tmp_path / "athena.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    attach_dir = tmp_path / "attachments"
    attach_dir.symlink_to(outside, target_is_directory=True)

    assert ops.doctor_main([str(source), "--attach-dir", str(attach_dir)]) == 1

    out = capsys.readouterr()
    assert "attachment path is not a directory" in out.err
    assert list(outside.iterdir()) == []


def test_restore_cleans_candidate_when_recovery_staging_fails(tmp_path, monkeypatch):
    source = _seed_database(tmp_path / "source.db", email="saved@example.com")
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = _seed_database(tmp_path / "target.db", email="stale@example.com")
    original_summary = _database_summary(target)
    real_stage = backup._stage_sqlite_database
    calls = 0

    def fail_recovery_stage(source_path, destination_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected recovery staging failure")
        return real_stage(source_path, destination_path)

    monkeypatch.setattr(backup, "_stage_sqlite_database", fail_recovery_stage)
    with pytest.raises(OSError, match="injected recovery staging failure"):
        backup.restore_database(snapshot, target, force=True)

    assert calls == 2
    assert _database_summary(target) == original_summary
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_restore_recovers_when_sidecar_cleanup_partially_fails(tmp_path, monkeypatch):
    source = _seed_database(tmp_path / "source.db", email="saved@example.com")
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = _seed_database(tmp_path / "target.db", email="stale@example.com")
    original_summary = _database_summary(target)
    real_remove = backup._remove_sqlite_sidecars
    attempts = 0

    def _fail_after_one_unlink(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            wal = path.with_name(f"{path.name}-wal")
            shm = path.with_name(f"{path.name}-shm")
            wal.write_bytes(b"stale-wal")
            shm.write_bytes(b"stale-shm")
            wal.unlink()
            raise PermissionError("injected sidecar cleanup failure")
        real_remove(path)

    monkeypatch.setattr(backup, "_remove_sqlite_sidecars", _fail_after_one_unlink)
    with pytest.raises(PermissionError, match="injected sidecar cleanup failure"):
        backup.restore_database(snapshot, target, force=True)

    assert attempts == 2
    assert _database_summary(target) == original_summary
    assert not target.with_name(f"{target.name}-wal").exists()
    assert not target.with_name(f"{target.name}-shm").exists()
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_restore_fsyncs_recovery_name_before_target_mutation(tmp_path, monkeypatch):
    source = _seed_database(tmp_path / "source.db", email="saved@example.com")
    snapshot = backup.backup_database(source, tmp_path / "source.backup.db")
    target = _seed_database(tmp_path / "target.db", email="stale@example.com")
    real_fsync_directory = backup._fsync_directory
    real_remove = backup._remove_sqlite_sidecars
    events = []

    def _record_fsync(path):
        events.append(("fsync", path))
        real_fsync_directory(path)

    def _record_sidecars(path):
        events.append(("sidecars", path))
        real_remove(path)

    monkeypatch.setattr(backup, "_fsync_directory", _record_fsync)
    monkeypatch.setattr(backup, "_remove_sqlite_sidecars", _record_sidecars)

    backup.restore_database(snapshot, target, force=True)

    assert events[0] == ("fsync", target.parent)
    assert events[1] == ("sidecars", target)
