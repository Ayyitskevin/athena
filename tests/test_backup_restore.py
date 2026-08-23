"""Tests for operator database backup and restore commands."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import socket
import sqlite3
import stat
import sys

import pytest

from athena import config, ops
from athena.core import attachments, backup, db, recovery, users


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


def _store_recovery_attachment(database, attach_dir, data, *, filename="proof.bin"):
    conn = db.connect(database)
    stored = attachments.store(
        conn,
        target_kind="issue",
        target_id=1,
        filename=filename,
        content_type="application/octet-stream",
        data=data,
        uploaded_by=1,
        attach_dir=attach_dir,
    )
    stored_name = attachments.get_stored_name(conn, stored["id"])
    conn.close()
    assert stored_name is not None
    return stored_name


def _recovery_source(tmp_path, *payloads):
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = _seed_database(tmp_path / "live.db")
    attach_dir = tmp_path / "live-attachments"
    attach_dir.mkdir()
    names = tuple(
        _store_recovery_attachment(
            database,
            attach_dir,
            payload,
            filename=f"proof-{position}.bin",
        )
        for position, payload in enumerate(payloads, start=1)
    )
    return database, attach_dir, names


def _read_manifest(bundle_path):
    return json.loads((bundle_path / "manifest.json").read_text())


def _write_manifest(bundle_path, manifest):
    (bundle_path / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    os.chmod(bundle_path / "manifest.json", 0o600)


def _refresh_database_manifest(bundle_path):
    database = bundle_path / "athena.db"
    payload = database.read_bytes()
    manifest = _read_manifest(bundle_path)
    manifest["database"]["byte_size"] = len(payload)
    manifest["database"]["sha256"] = hashlib.sha256(payload).hexdigest()
    _write_manifest(bundle_path, manifest)


def _tree_evidence(root):
    evidence = {}
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        relative = str(path.relative_to(root)) or "."
        payload = path.read_bytes() if path.is_file() else None
        evidence[relative] = (
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            payload,
        )
    return evidence


def test_recovery_bundle_empty_and_multi_blob_layout_is_private(tmp_path):
    empty_database, empty_attachments, _ = _recovery_source(tmp_path / "empty")
    empty_bundle = tmp_path / "empty-bundle"

    empty = recovery.create_recovery_bundle(
        empty_database,
        empty_attachments,
        empty_bundle,
    )

    assert empty.attachment_count == 0
    assert recovery.verify_recovery_bundle(empty_bundle) == empty
    assert {path.name for path in empty_bundle.iterdir()} == {
        "athena.db",
        "attachments",
        "manifest.json",
    }
    assert not list((empty_bundle / "attachments").iterdir())

    database, attach_dir, names = _recovery_source(
        tmp_path / "full",
        b"first",
        b"second payload",
    )
    (attach_dir / "live-orphan.bin").write_bytes(b"not database-owned")
    bundle_path = tmp_path / "full-bundle"
    old_umask = os.umask(0)
    try:
        created = recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    finally:
        os.umask(old_umask)

    assert created.attachment_count == 2
    assert created.attachment_bytes == len(b"first") + len(b"second payload")
    assert set(path.name for path in (bundle_path / "attachments").iterdir()) == set(
        names
    )
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle_path / "attachments").stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in bundle_path.rglob("*")
        if path.is_file()
    )
    verified = recovery.verify_recovery_bundle(bundle_path)
    assert verified.path == bundle_path
    assert verified.attachment_count == 2
    assert _read_manifest(bundle_path)["schema"] == "athena.recovery_bundle.v1"
    assert "live-orphan.bin" not in {path.name for path in bundle_path.rglob("*")}


def test_recovery_bundle_cli_create_verify_and_drill(tmp_path, capsys):
    database, attach_dir, names = _recovery_source(tmp_path, b"drill evidence")
    bundle_path = tmp_path / "bundle"
    drill_path = tmp_path / "drill"

    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 0
    )
    assert "Created recovery bundle" in capsys.readouterr().out
    assert (
        ops.doctor_main(
            [
                str(bundle_path),
                "--recovery-bundle",
                "--drill-dir",
                str(drill_path),
                "--max-recovery-point-age-seconds",
                "60",
                "--max-data-recovery-seconds",
                "60",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "recovery bundle: ok" in output
    assert (
        "recorded Athena version "
        f"{json.dumps(_read_manifest(bundle_path)['athena_version'])}" in output
    )
    assert "data-recovery drill: ok" in output
    assert "athena-doctor: ok" in output
    assert {path.name for path in drill_path.iterdir()} == {
        "athena.db",
        "attachments",
    }
    assert (drill_path / "attachments" / names[0]).read_bytes() == b"drill evidence"
    assert _database_summary(drill_path / "athena.db") == _database_summary(database)


def test_recovery_doctor_escapes_untrusted_version_provenance(tmp_path, capsys):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    manifest = _read_manifest(bundle_path)
    manifest["athena_version"] = "forged\n\x1b[31mterminal"
    _write_manifest(bundle_path, manifest)

    assert ops.doctor_main([str(bundle_path), "--recovery-bundle"]) == 0

    output = capsys.readouterr().out
    assert manifest["athena_version"] not in output
    assert json.dumps(manifest["athena_version"]) in output


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--recovery-bundle"], "requires --attach-dir"),
        (
            ["--recovery-bundle", "--attach-dir", "attachments", "--overwrite"],
            "cannot be combined",
        ),
        (
            ["--recovery-bundle", "--attach-dir", "attachments", "--keep", "2"],
            "cannot be combined",
        ),
        (["--attach-dir", "attachments"], "requires --recovery-bundle"),
    ],
)
def test_recovery_backup_option_matrix_rejects_ambiguous_modes(
    tmp_path, capsys, arguments, message
):
    database = tmp_path / "database.db"
    destination = tmp_path / "destination"
    with pytest.raises(SystemExit) as error:
        ops.backup_main([str(database), str(destination), *arguments])
    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--recovery-bundle", "--migrate"], "cannot be combined"),
        (["--recovery-bundle", "--attach-dir", "attachments"], "cannot be combined"),
        (["--drill-dir", "drill"], "require --recovery-bundle"),
        (
            ["--recovery-bundle", "--max-data-recovery-seconds", "1"],
            "requires --drill-dir",
        ),
        (["--max-recovery-point-age-seconds", "1"], "require --recovery-bundle"),
    ],
)
def test_recovery_doctor_option_matrix_rejects_ambiguous_modes(
    tmp_path, capsys, arguments, message
):
    with pytest.raises(SystemExit) as error:
        ops.doctor_main([str(tmp_path / "path"), *arguments])
    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("extra_root", "missing or unexpected root entries"),
        ("missing_manifest", "missing or unexpected root entries"),
        ("weak_root_mode", "must have mode 0700"),
        ("weak_manifest_mode", "must have mode 0600"),
        ("tampered_database", "does not match its manifest"),
        ("missing_blob", "missing or unexpected entries"),
        ("extra_blob", "missing or unexpected entries"),
        ("tampered_blob", "does not match the recovery database"),
        ("weak_blob_mode", "must have mode 0600"),
    ],
)
def test_recovery_verifier_rejects_layout_mode_and_content_drift(
    tmp_path, mutation, expected
):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    blob = bundle_path / "attachments" / names[0]

    if mutation == "extra_root":
        (bundle_path / "unexpected").write_bytes(b"extra")
    elif mutation == "missing_manifest":
        (bundle_path / "manifest.json").unlink()
    elif mutation == "weak_root_mode":
        os.chmod(bundle_path, 0o755)
    elif mutation == "weak_manifest_mode":
        os.chmod(bundle_path / "manifest.json", 0o644)
    elif mutation == "tampered_database":
        with (bundle_path / "athena.db").open("ab") as handle:
            handle.write(b"tamper")
    elif mutation == "missing_blob":
        blob.unlink()
    elif mutation == "extra_blob":
        (bundle_path / "attachments" / "extra").write_bytes(b"extra")
    elif mutation == "tampered_blob":
        blob.write_bytes(b"tampered")
    elif mutation == "weak_blob_mode":
        os.chmod(blob, 0o644)

    with pytest.raises((OSError, ValueError), match=expected):
        recovery.verify_recovery_bundle(bundle_path)


@pytest.mark.parametrize("entry_type", ["symlink", "fifo", "directory", "socket"])
def test_recovery_verifier_rejects_nonregular_attachment_entries(
    tmp_path, monkeypatch, entry_type
):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    blob = bundle_path / "attachments" / names[0]
    blob.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"evidence")
    listener = None
    if entry_type == "symlink":
        blob.symlink_to(outside)
    elif entry_type == "fifo":
        os.mkfifo(blob)
    elif entry_type == "directory":
        blob.mkdir()
    else:
        listener = socket.socket(socket.AF_UNIX)
        monkeypatch.chdir(blob.parent)
        listener.bind(blob.name)
    try:
        with pytest.raises((OSError, ValueError)):
            recovery.verify_recovery_bundle(bundle_path)
    finally:
        if listener is not None:
            listener.close()


def test_recovery_verifier_rejects_hardlinks_and_symlinked_roots(tmp_path):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    blob = bundle_path / "attachments" / names[0]
    os.link(blob, tmp_path / "second-link")
    with pytest.raises(ValueError, match="must not be hard-linked"):
        recovery.verify_recovery_bundle(bundle_path)

    alias = tmp_path / "bundle-alias"
    alias.symlink_to(bundle_path, target_is_directory=True)
    with pytest.raises(OSError):
        recovery.verify_recovery_bundle(alias)


@pytest.mark.parametrize(
    "manifest_mutation, expected",
    [
        ("unknown", "missing or unknown fields"),
        ("wrong_schema", "unsupported schema"),
        ("bad_path", "invalid database path"),
        ("negative_count", "non-negative integer"),
        ("boolean_count", "non-negative integer"),
        ("bad_hash", "not SHA-256"),
        ("reversed_time", "timestamps are reversed"),
        ("future_time", "timestamp is in the future"),
    ],
)
def test_recovery_verifier_rejects_strict_manifest_contract(
    tmp_path, manifest_mutation, expected
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    manifest = _read_manifest(bundle_path)

    if manifest_mutation == "unknown":
        manifest["unexpected"] = True
    elif manifest_mutation == "wrong_schema":
        manifest["schema"] = "athena.recovery_bundle.v2"
    elif manifest_mutation == "bad_path":
        manifest["database"]["path"] = "../athena.db"
    elif manifest_mutation == "negative_count":
        manifest["attachments"]["count"] = -1
    elif manifest_mutation == "boolean_count":
        manifest["attachments"]["count"] = True
    elif manifest_mutation == "bad_hash":
        manifest["database"]["sha256"] = "A" * 64
    elif manifest_mutation == "reversed_time":
        manifest["capture_started_at"] = "2026-01-02T00:00:00.000000Z"
        manifest["capture_completed_at"] = "2026-01-01T00:00:00.000000Z"
    elif manifest_mutation == "future_time":
        future = datetime.now(timezone.utc) + timedelta(days=1)
        manifest["capture_completed_at"] = future.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
    _write_manifest(bundle_path, manifest)

    with pytest.raises(ValueError, match=expected):
        recovery.verify_recovery_bundle(bundle_path)


@pytest.mark.parametrize("payload", [b'{"schema":"x","schema":"y"}', b'{"x":NaN}'])
def test_recovery_verifier_rejects_duplicate_keys_and_nonstandard_json(
    tmp_path, payload
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    (bundle_path / "manifest.json").write_bytes(payload)

    with pytest.raises(ValueError, match="not strict JSON"):
        recovery.verify_recovery_bundle(bundle_path)


def test_recovery_verifier_rejects_deeply_nested_manifest_without_traceback(tmp_path):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    nested_payload = b"[" * 10_000 + b"0" + b"]" * 10_000
    (bundle_path / "manifest.json").write_bytes(nested_payload)

    with pytest.raises(ValueError, match="not strict JSON"):
        recovery.verify_recovery_bundle(bundle_path)


def test_recovery_verifier_bounds_manifest_size_and_utf8(tmp_path):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    manifest_path = bundle_path / "manifest.json"
    manifest_path.write_bytes(b"{" + b" " * (64 * 1024) + b"}")
    with pytest.raises(ValueError, match="exceeds its size limit"):
        recovery.verify_recovery_bundle(bundle_path)

    manifest_path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="not valid UTF-8"):
        recovery.verify_recovery_bundle(bundle_path)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("root_array", "must be an object"),
        ("schema_number", "must be a string"),
        ("version_empty", "invalid Athena version"),
        ("version_long", "is too long"),
        ("timestamp_shape", "not canonical UTC"),
        ("timestamp_calendar", "timestamp is invalid"),
        ("latest_empty", "invalid latest migration"),
        ("latest_mismatch", "migration does not match manifest"),
        ("attachment_path", "invalid attachment path"),
        ("attachment_total", "attachments do not match their manifest"),
    ],
)
def test_recovery_verifier_rejects_additional_manifest_mismatches(
    tmp_path, mutation, expected
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    manifest = _read_manifest(bundle_path)

    if mutation == "root_array":
        (bundle_path / "manifest.json").write_text("[]\n")
    else:
        if mutation == "schema_number":
            manifest["schema"] = 1
        elif mutation == "version_empty":
            manifest["athena_version"] = ""
        elif mutation == "version_long":
            manifest["athena_version"] = "x" * 65
        elif mutation == "timestamp_shape":
            manifest["capture_started_at"] = "2026-01-01T00:00:00Z"
        elif mutation == "timestamp_calendar":
            manifest["capture_started_at"] = "2026-02-30T00:00:00.000000Z"
        elif mutation == "latest_empty":
            manifest["database"]["latest_migration"] = ""
        elif mutation == "latest_mismatch":
            manifest["database"]["latest_migration"] = "0001_initial.sql"
        elif mutation == "attachment_path":
            manifest["attachments"]["path"] = "../attachments"
        else:
            manifest["attachments"]["byte_size"] = 1
        _write_manifest(bundle_path, manifest)

    with pytest.raises(ValueError, match=expected):
        recovery.verify_recovery_bundle(bundle_path)


@pytest.mark.parametrize(
    "database_mutation, expected",
    [
        (
            "negative_size",
            "attachment metadata row 1 is invalid",
        ),
        (
            "invalid_type",
            "invalid types",
        ),
        (
            "invalid_stored_name",
            "invalid stored name",
        ),
        (
            "invalid_hash",
            "metadata row 1 is invalid",
        ),
        (
            "missing_target",
            "missing target",
        ),
        (
            "foreign_key",
            "foreign key check failed",
        ),
        (
            "schema_drift",
            "schema does not match",
        ),
        (
            "future_migration",
            "future migration",
        ),
    ],
)
def test_recovery_verifier_rejects_semantic_and_database_drift(
    tmp_path, database_mutation, expected
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    conn = sqlite3.connect(bundle_path / "athena.db")
    try:
        if database_mutation == "negative_size":
            conn.execute("UPDATE attachments SET byte_size = -1")
        elif database_mutation == "invalid_type":
            conn.execute("UPDATE attachments SET byte_size = 'eight'")
        elif database_mutation == "invalid_stored_name":
            conn.execute("UPDATE attachments SET stored_name = '../unsafe'")
        elif database_mutation == "invalid_hash":
            conn.execute("UPDATE attachments SET sha256 = ?", ("A" * 64,))
        elif database_mutation == "missing_target":
            conn.execute("UPDATE attachments SET target_id = 999999")
        elif database_mutation == "foreign_key":
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE attachments SET uploaded_by = 999999")
        elif database_mutation == "schema_drift":
            conn.execute("CREATE TABLE unexpected_schema (id INTEGER PRIMARY KEY)")
        else:
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (?, ?)",
                ("9999_future.sql", "0" * 64),
            )
        conn.commit()
    finally:
        conn.close()
    _refresh_database_manifest(bundle_path)

    with pytest.raises(ValueError, match=expected):
        recovery.verify_recovery_bundle(bundle_path)


def test_recovery_capture_snapshot_omits_later_commit_and_uncommitted_blob(
    tmp_path, monkeypatch
):
    database, attach_dir, original_names = _recovery_source(tmp_path, b"original")
    bundle_path = tmp_path / "bundle"
    real_snapshot = recovery._snapshot_recovery_database
    later = {}

    def snapshot_then_upload(
        source_path,
        source_descriptor,
        source_identity,
        destination_path,
        destination_directory_descriptor,
    ):
        result = real_snapshot(
            source_path,
            source_descriptor,
            source_identity,
            destination_path,
            destination_directory_descriptor,
        )
        later["name"] = _store_recovery_attachment(
            database,
            attach_dir,
            b"committed later",
            filename="later.bin",
        )
        return result

    monkeypatch.setattr(recovery, "_snapshot_recovery_database", snapshot_then_upload)
    created = recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert created.attachment_count == 1
    assert set(path.name for path in (bundle_path / "attachments").iterdir()) == set(
        original_names
    )
    assert later["name"] not in original_names

    monkeypatch.setattr(recovery, "_snapshot_recovery_database", real_snapshot)
    second_bundle = tmp_path / "uncommitted-bundle"
    writer = db.connect(database)
    pending = attachments.insert_and_publish(
        writer,
        target_kind="issue",
        target_id=1,
        filename="pending.bin",
        content_type="application/octet-stream",
        data=b"published but uncommitted",
        uploaded_by=1,
        attach_dir=attach_dir,
    )
    pending_name = attachments.get_stored_name(writer, pending.attachment["id"])
    assert pending_name is not None
    try:
        second = recovery.create_recovery_bundle(database, attach_dir, second_bundle)
    finally:
        writer.rollback()
        writer.close()
        pending.blob_path.unlink()
    assert second.attachment_count == 2
    assert pending_name not in {
        path.name for path in (second_bundle / "attachments").iterdir()
    }


def test_recovery_capture_includes_committed_wal_state_from_pinned_source(tmp_path):
    database, attach_dir, _ = _recovery_source(tmp_path)
    writer = db.connect(database)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO issues (title, created_by) VALUES (?, ?)",
            ("Committed in WAL", 1),
        )
        writer.commit()
        assert database.with_name(f"{database.name}-wal").exists()

        bundle_path = tmp_path / "bundle"
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)
        recovered = sqlite3.connect(bundle_path / "athena.db")
        try:
            assert recovered.execute(
                "SELECT title FROM issues WHERE title = ?",
                ("Committed in WAL",),
            ).fetchone() == ("Committed in WAL",)
        finally:
            recovered.close()
    finally:
        writer.close()


def test_recovery_capture_fails_if_snapshot_blob_disappears_before_open(
    tmp_path, monkeypatch
):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    real_copy = recovery._copy_attachment
    removed = False

    def delete_then_copy(source_descriptor, destination_descriptor, row):
        nonlocal removed
        if not removed:
            (attach_dir / names[0]).unlink()
            removed = True
        return real_copy(source_descriptor, destination_descriptor, row)

    monkeypatch.setattr(recovery, "_copy_attachment", delete_then_copy)
    with pytest.raises(FileNotFoundError):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert not bundle_path.exists()
    assert len(list(tmp_path.glob(".bundle.*.tmp"))) == 1


def test_recovery_capture_can_finish_after_source_unlink_once_descriptor_is_open(
    tmp_path, monkeypatch
):
    database, attach_dir, names = _recovery_source(tmp_path, b"pinned evidence")
    bundle_path = tmp_path / "bundle"
    real_open = recovery._open_regular_at
    removed = False

    def open_then_unlink(directory_descriptor, name, **kwargs):
        nonlocal removed
        result = real_open(directory_descriptor, name, **kwargs)
        if not removed and name == names[0] and kwargs.get("expected_mode") is None:
            (attach_dir / name).unlink()
            removed = True
        return result

    monkeypatch.setattr(recovery, "_open_regular_at", open_then_unlink)
    created = recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert removed
    assert created.attachment_count == 1
    assert (bundle_path / "attachments" / names[0]).read_bytes() == b"pinned evidence"


@pytest.mark.parametrize(
    "entry_type", ["symlink", "fifo", "directory", "socket", "hardlink"]
)
def test_recovery_capture_rejects_unsafe_live_blob_types(
    tmp_path, monkeypatch, entry_type
):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    blob = attach_dir / names[0]
    outside = tmp_path / "outside"
    outside.write_bytes(b"evidence")
    listener = None
    if entry_type == "hardlink":
        os.link(blob, tmp_path / "second-link")
    else:
        blob.unlink()
        if entry_type == "symlink":
            blob.symlink_to(outside)
        elif entry_type == "fifo":
            os.mkfifo(blob)
        elif entry_type == "directory":
            blob.mkdir()
        else:
            listener = socket.socket(socket.AF_UNIX)
            monkeypatch.chdir(blob.parent)
            listener.bind(blob.name)
    try:
        with pytest.raises((OSError, ValueError)):
            recovery.create_recovery_bundle(database, attach_dir, tmp_path / "bundle")
    finally:
        if listener is not None:
            listener.close()
    assert not (tmp_path / "bundle").exists()
    assert len(list(tmp_path.glob(".bundle.*.tmp"))) == 1


def test_recovery_capture_rejects_existing_alias_nested_and_unsafe_sources(tmp_path):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "sentinel"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        recovery.create_recovery_bundle(database, attach_dir, existing)
    assert sentinel.read_bytes() == b"preserve"

    broken = tmp_path / "broken"
    broken.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(FileExistsError):
        recovery.create_recovery_bundle(database, attach_dir, broken)

    with pytest.raises(ValueError, match="protected source tree"):
        recovery.create_recovery_bundle(database, attach_dir, attach_dir / "bundle")

    attach_alias = tmp_path / "attachment-alias"
    attach_alias.symlink_to(attach_dir, target_is_directory=True)
    with pytest.raises(NotADirectoryError):
        recovery.create_recovery_bundle(database, attach_alias, tmp_path / "bundle")

    database_alias = tmp_path / "database-alias"
    database_alias.symlink_to(database)
    with pytest.raises(FileNotFoundError, match="not a regular file"):
        recovery.create_recovery_bundle(database_alias, attach_dir, tmp_path / "bundle")

    database_link = tmp_path / "database-link"
    os.link(database, database_link)
    with pytest.raises(ValueError, match="must not be hard-linked"):
        recovery.create_recovery_bundle(database, attach_dir, tmp_path / "bundle")


def test_recovery_capture_rejects_renamed_attachment_root_as_destination_parent(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    moved_attachments = tmp_path / "moved-attachments"
    bundle_path = moved_attachments / "bundle"
    real_open = recovery._open_source_attachment_directory

    def move_root_after_open(path):
        opened = real_open(path)
        attach_dir.rename(moved_attachments)
        attach_dir.mkdir()
        return opened

    monkeypatch.setattr(
        recovery,
        "_open_source_attachment_directory",
        move_root_after_open,
    )
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 1
    )

    assert "attachment directory was replaced" in capsys.readouterr().err
    assert not bundle_path.exists()
    assert not list(moved_attachments.glob(".bundle.*.tmp"))


@pytest.mark.parametrize("replacement", [b"short", b"tampered"])
def test_recovery_capture_rejects_live_blob_size_or_hash_mismatch(
    tmp_path, replacement
):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    (attach_dir / names[0]).write_bytes(replacement)

    with pytest.raises(ValueError, match="does not match the database snapshot"):
        recovery.create_recovery_bundle(database, attach_dir, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("stream_change", ["append", "truncate"])
def test_recovery_capture_detects_live_blob_change_during_stream(
    tmp_path, monkeypatch, stream_change
):
    payload = b"x" * (recovery._HASH_CHUNK_BYTES + 16)
    database, attach_dir, names = _recovery_source(tmp_path, payload)
    blob = attach_dir / names[0]
    blob_inode = blob.stat().st_ino
    real_read = recovery.os.read
    changed = False

    def mutate_after_first_chunk(descriptor, count):
        nonlocal changed
        chunk = real_read(descriptor, count)
        if not changed and chunk and os.fstat(descriptor).st_ino == blob_inode:
            changed = True
            if stream_change == "append":
                with blob.open("ab") as handle:
                    handle.write(b"growth")
            else:
                with blob.open("r+b") as handle:
                    handle.truncate(len(chunk))
        return chunk

    monkeypatch.setattr(recovery.os, "read", mutate_after_first_chunk)
    with pytest.raises(ValueError, match="changed|grew"):
        recovery.create_recovery_bundle(database, attach_dir, tmp_path / "bundle")

    assert changed
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    "missing_kind, expected",
    [
        ("database", "database path does not exist"),
        ("attachments", "attachment directory does not exist"),
        ("parent", "parent directory does not exist"),
    ],
)
def test_recovery_capture_reports_missing_required_paths(
    tmp_path, missing_kind, expected
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    destination = tmp_path / "bundle"
    if missing_kind == "database":
        database.unlink()
    elif missing_kind == "attachments":
        attach_dir.rmdir()
    else:
        destination = tmp_path / "missing-parent" / "bundle"

    with pytest.raises(FileNotFoundError, match=expected):
        recovery.create_recovery_bundle(database, attach_dir, destination)


def test_recovery_capture_fails_if_source_path_is_replaced_during_snapshot(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    moved_database = tmp_path / "moved-athena.db"
    real_connect_readonly = recovery._connect_readonly
    replaced = False

    def replace_path_then_connect(path):
        nonlocal replaced
        database.rename(moved_database)
        database.write_bytes(b"replacement path")
        replaced = True
        return real_connect_readonly(path)

    monkeypatch.setattr(recovery, "_connect_readonly", replace_path_then_connect)
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 1
    )

    assert replaced
    error = capsys.readouterr().err
    assert "source database path was replaced" in error
    assert "private stage retained at" in error
    assert not bundle_path.exists()
    assert len(list(tmp_path.glob(".bundle.*.tmp"))) == 1


def test_recovery_capture_publication_race_preserves_winner_and_private_stage(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    real_rename = recovery._rename_no_replace_at

    def publish_race(parent_descriptor, source_name, destination_name):
        if destination_name != bundle_path.name:
            return real_rename(parent_descriptor, source_name, destination_name)
        os.mkdir(destination_name, dir_fd=parent_descriptor)
        winner = tmp_path / destination_name / "winner"
        winner.write_bytes(b"concurrent owner")
        raise FileExistsError("injected publication race")

    monkeypatch.setattr(recovery, "_rename_no_replace_at", publish_race)
    with pytest.raises(FileExistsError, match="injected publication race"):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert (bundle_path / "winner").read_bytes() == b"concurrent owner"
    assert len(list(tmp_path.glob(".bundle.*.tmp"))) == 1


def test_recovery_capture_rejects_nonempty_stage_substitution_before_open(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    real_mkdir = recovery.os.mkdir
    created_stage = None
    replacement_stage = None

    def replace_stage_after_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal created_stage, replacement_stage
        real_mkdir(path, mode, dir_fd=dir_fd)
        if not isinstance(path, str) or not path.startswith(".bundle."):
            return
        assert dir_fd is not None
        created_stage = tmp_path / f"{path}.created"
        os.rename(
            path,
            created_stage.name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
        )
        real_mkdir(path, 0o700, dir_fd=dir_fd)
        replacement_stage = tmp_path / path
        (replacement_stage / "sentinel").write_bytes(b"concurrent owner")

    monkeypatch.setattr(recovery.os, "mkdir", replace_stage_after_mkdir)
    with pytest.raises(
        recovery.RecoveryStagePreservedError,
        match="replaced during allocation",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert created_stage is not None
    assert replacement_stage is not None
    assert not bundle_path.exists()
    assert not list(created_stage.iterdir())
    assert (replacement_stage / "sentinel").read_bytes() == b"concurrent owner"


def test_recovery_capture_pins_stage_before_any_content_write(tmp_path, monkeypatch):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    bundle_path = tmp_path / "bundle"
    real_make_stage = recovery._make_private_stage
    moved_stage = None
    replacement_stage = None

    def move_and_replace_stage(destination, *, owner=None):
        nonlocal moved_stage, replacement_stage
        stage = real_make_stage(destination, owner=owner)
        moved_stage = tmp_path / f"{stage.name}.moved"
        os.rename(
            stage.name,
            moved_stage.name,
            src_dir_fd=destination.parent_descriptor,
            dst_dir_fd=destination.parent_descriptor,
        )
        os.mkdir(stage.name, 0o700, dir_fd=destination.parent_descriptor)
        replacement_stage = tmp_path / stage.name
        (replacement_stage / "sentinel").write_bytes(b"concurrent owner")
        (replacement_stage / "athena.db").write_bytes(b"must not be overwritten")
        return stage

    monkeypatch.setattr(recovery, "_make_private_stage", move_and_replace_stage)
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="failed recovery stage was replaced",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert moved_stage is not None
    assert replacement_stage is not None
    assert not bundle_path.exists()
    assert (replacement_stage / "sentinel").read_bytes() == b"concurrent owner"
    assert (replacement_stage / "athena.db").read_bytes() == b"must not be overwritten"
    assert recovery.verify_recovery_bundle(moved_stage).attachment_count == 1


def test_recovery_capture_never_overwrites_raced_staged_database(tmp_path, monkeypatch):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    bundle_path = tmp_path / "bundle"
    real_stage_database = recovery._stage_sqlite_database

    def inject_destination_after_snapshot(source, destination, **kwargs):
        staged = real_stage_database(source, destination, **kwargs)
        destination.write_bytes(b"concurrent owner")
        return staged

    monkeypatch.setattr(
        recovery,
        "_stage_sqlite_database",
        inject_destination_after_snapshot,
    )
    with pytest.raises(FileExistsError, match="destination appeared"):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert (retained[0] / "athena.db").read_bytes() == b"concurrent owner"
    assert len(list(retained[0].glob(".athena.db.*.tmp"))) == 1


def test_recovery_capture_rejects_nonregular_staged_database(tmp_path, monkeypatch):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    bundle_path = tmp_path / "bundle"
    real_stage_database = recovery._stage_sqlite_database

    def replace_snapshot_with_directory(source, destination, **kwargs):
        staged = real_stage_database(source, destination, **kwargs)
        staged.path.unlink()
        staged.path.mkdir()
        return staged

    monkeypatch.setattr(
        recovery,
        "_stage_sqlite_database",
        replace_snapshot_with_directory,
    )
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="staged recovery database was replaced",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert len(list(retained[0].glob(".athena.db.*.tmp"))) == 1
    assert next(retained[0].glob(".athena.db.*.tmp")).is_dir()


def test_recovery_snapshot_temp_swap_cannot_overwrite_link_target(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    victim = _seed_database(tmp_path / "victim.db", issue="must remain unchanged")
    victim_before = victim.read_bytes()
    bundle_path = tmp_path / "bundle"
    real_connect = recovery._connect
    swapped_path = None

    def replace_temp_name_then_connect(path):
        nonlocal swapped_path
        if swapped_path is None and str(path).startswith("/proc/self/fd/"):
            swapped_path = path.resolve(strict=True)
            swapped_path.unlink()
            swapped_path.symlink_to(victim)
        return real_connect(path)

    monkeypatch.setattr(recovery, "_connect", replace_temp_name_then_connect)
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="staged recovery database was replaced during snapshot",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert swapped_path is not None
    assert victim.read_bytes() == victim_before
    assert _database_summary(victim)["issue"] == "must remain unchanged"
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert next(retained[0].glob(".athena.db.*.tmp")).is_symlink()


def test_recovery_capture_rejects_staged_database_swap_during_no_replace(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    bundle_path = tmp_path / "bundle"
    real_rename = recovery._rename_no_replace_at
    stolen_name = None

    def swap_database_then_rename(parent_descriptor, source_name, destination_name):
        nonlocal stolen_name
        if destination_name == "athena.db":
            stolen_name = f"{source_name}.verified"
            os.rename(
                source_name,
                stolen_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            replacement = os.open(
                source_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_descriptor,
            )
            try:
                os.write(replacement, b"unverified database")
            finally:
                os.close(replacement)
        real_rename(parent_descriptor, source_name, destination_name)

    monkeypatch.setattr(
        recovery,
        "_rename_no_replace_at",
        swap_database_then_rename,
    )
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="staged recovery database was replaced during rename",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert stolen_name is not None
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert (retained[0] / "athena.db").read_bytes() == b"unverified database"
    assert (retained[0] / stolen_name).is_file()


def test_recovery_capture_rejects_stage_swap_inside_atomic_publication(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    bundle_path = tmp_path / "bundle"
    real_rename = recovery._rename_no_replace_at
    stolen_name = ".verified-stage-preserved"

    def swap_stage_then_publish(parent_descriptor, source_name, destination_name):
        if destination_name != bundle_path.name:
            return real_rename(parent_descriptor, source_name, destination_name)
        os.rename(
            source_name,
            stolen_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.mkdir(source_name, 0o700, dir_fd=parent_descriptor)
        fake_stage = tmp_path / source_name
        (fake_stage / "sentinel").write_bytes(b"unverified")
        real_rename(parent_descriptor, source_name, destination_name)

    monkeypatch.setattr(recovery, "_rename_no_replace_at", swap_stage_then_publish)
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="rollback failed",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert (bundle_path / "sentinel").read_bytes() == b"unverified"
    assert recovery.verify_recovery_bundle(tmp_path / stolen_name).attachment_count == 1


def test_recovery_capture_rejects_child_swap_after_verification(tmp_path, monkeypatch):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    bundle_path = tmp_path / "bundle"
    real_publish = recovery._publish_private_stage

    def replace_manifest_then_publish(stage, destination, *, protected):
        manifest = stage.path / "manifest.json"
        manifest.unlink()
        manifest.write_bytes(b"unverified")
        os.chmod(manifest, 0o600)
        real_publish(stage, destination, protected=protected)

    monkeypatch.setattr(
        recovery,
        "_publish_private_stage",
        replace_manifest_then_publish,
    )
    with pytest.raises(
        ValueError, match="changed during recovery (processing|publication)"
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert (retained[0] / "manifest.json").read_bytes() == b"unverified"


def test_recovery_capture_rejects_destination_parent_replacement_after_rename(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    publish_parent = tmp_path / "publish-parent"
    publish_parent.mkdir()
    moved_parent = tmp_path / "moved-publish-parent"
    bundle_path = publish_parent / "bundle"
    real_rename = recovery._rename_no_replace_at
    moved = False

    def move_parent_after_publish(parent_descriptor, source_name, destination_name):
        nonlocal moved
        real_rename(parent_descriptor, source_name, destination_name)
        if not moved and destination_name == bundle_path.name:
            publish_parent.rename(moved_parent)
            publish_parent.mkdir()
            moved = True

    monkeypatch.setattr(
        recovery,
        "_rename_no_replace_at",
        move_parent_after_publish,
    )
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="rolled back",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert not bundle_path.exists()
    assert not (moved_parent / "bundle").exists()
    retained = list(moved_parent.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert recovery.verify_recovery_bundle(retained[0]).attachment_count == 1


def test_recovery_capture_rolls_back_parent_move_into_attachment_source(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    publish_parent = tmp_path / "bundle-parent"
    publish_parent.mkdir()
    bundle_path = publish_parent / "bundle"
    moved_parent = attach_dir / publish_parent.name
    real_rename = recovery._rename_no_replace_at
    moved = False

    def move_parent_then_rename(parent_descriptor, source_name, destination_name):
        nonlocal moved
        if not moved and destination_name == bundle_path.name:
            publish_parent.rename(moved_parent)
            moved = True
        real_rename(parent_descriptor, source_name, destination_name)

    monkeypatch.setattr(
        recovery,
        "_rename_no_replace_at",
        move_parent_then_rename,
    )
    with pytest.raises(recovery.RecoveryRaceError, match="rolled back"):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert not (moved_parent / "bundle").exists()
    retained = list(moved_parent.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert recovery.verify_recovery_bundle(retained[0]).attachment_count == 1
    moved_parent.rename(publish_parent)
    assert not bundle_path.exists()


def test_recovery_capture_rolls_back_parent_move_after_publication_fsync(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    publish_parent = tmp_path / "bundle-parent"
    publish_parent.mkdir()
    bundle_path = publish_parent / "bundle"
    moved_parent = attach_dir / publish_parent.name
    real_validation = recovery._require_published_stage_safe
    validations = 0

    def move_parent_on_second_validation(stage, destination, *, protected):
        nonlocal validations
        validations += 1
        if validations == 2:
            publish_parent.rename(moved_parent)
        return real_validation(stage, destination, protected=protected)

    monkeypatch.setattr(
        recovery,
        "_require_published_stage_safe",
        move_parent_on_second_validation,
    )
    with pytest.raises(recovery.RecoveryRaceError, match="rolled back"):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert validations == 2
    assert not (moved_parent / "bundle").exists()
    retained = list(moved_parent.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    moved_parent.rename(publish_parent)
    assert not bundle_path.exists()


def test_recovery_capture_reports_failed_final_validation_rollback(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"verified evidence")
    bundle_path = tmp_path / "bundle"
    real_publish = recovery._publish_private_stage
    real_rename = recovery._rename_no_replace_at

    def replace_manifest_then_publish(stage, destination, *, protected):
        manifest = stage.path / "manifest.json"
        manifest.unlink()
        manifest.write_bytes(b"unverified")
        os.chmod(manifest, 0o600)
        real_publish(stage, destination, protected=protected)

    def refuse_final_retraction(parent_descriptor, source_name, destination_name):
        if source_name == bundle_path.name and destination_name.startswith(".bundle."):
            raise OSError("injected final rollback failure")
        real_rename(parent_descriptor, source_name, destination_name)

    monkeypatch.setattr(
        recovery,
        "_publish_private_stage",
        replace_manifest_then_publish,
    )
    monkeypatch.setattr(
        recovery,
        "_rename_no_replace_at",
        refuse_final_retraction,
    )
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="failed final validation and rollback failed",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert (bundle_path / "manifest.json").read_bytes() == b"unverified"


def test_recovery_no_replace_primitive_fails_on_existing_and_missing_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    parent_descriptor = recovery._open_directory_path(tmp_path, label="test parent")
    try:
        with pytest.raises(FileExistsError):
            recovery._rename_no_replace_at(
                parent_descriptor,
                source.name,
                destination.name,
            )
        assert source.is_dir() and destination.is_dir()

        with pytest.raises(FileNotFoundError):
            recovery._rename_no_replace_at(
                parent_descriptor,
                "missing",
                "new",
            )
    finally:
        os.close(parent_descriptor)


def test_recovery_failure_never_deletes_a_replaced_stage_directory(tmp_path):
    destination = tmp_path / "bundle"
    protected = tmp_path / "protected"
    protected.mkdir()
    with recovery._new_directory_destination(
        destination,
        label="test recovery bundle",
        forbidden_root=protected,
    ) as pinned_destination:
        stage = recovery._make_private_stage(pinned_destination)
        try:
            os.rename(
                stage.name,
                "moved-stage",
                src_dir_fd=pinned_destination.parent_descriptor,
                dst_dir_fd=pinned_destination.parent_descriptor,
            )
            os.mkdir(stage.name, dir_fd=pinned_destination.parent_descriptor)
            sentinel = tmp_path / stage.name / "sentinel"
            sentinel.write_bytes(b"preserve")

            with pytest.raises(
                recovery.RecoveryRaceError,
                match="failed recovery stage was replaced",
            ):
                recovery._record_preserved_stage(
                    pinned_destination,
                    stage,
                    reason=OSError("injected failure"),
                )
            assert sentinel.read_bytes() == b"preserve"
            assert (tmp_path / "moved-stage").is_dir()
        finally:
            os.close(stage.descriptor)


def test_recovery_stage_initialization_failure_is_retained(tmp_path, monkeypatch):
    database, attach_dir, _ = _recovery_source(tmp_path)

    def fail_stage_mode(descriptor, mode):
        raise OSError("injected stage mode failure")

    monkeypatch.setattr(recovery.os, "fchmod", fail_stage_mode)
    with pytest.raises(
        recovery.RecoveryStagePreservedError,
        match="private stage retained",
    ):
        recovery.create_recovery_bundle(database, attach_dir, tmp_path / "bundle")

    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert retained[0].is_dir()


def test_recovery_stage_initialization_interrupt_retains_stage_and_exit_130(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path)

    def interrupt_stage_mode(descriptor, mode):
        raise KeyboardInterrupt

    monkeypatch.setattr(recovery.os, "fchmod", interrupt_stage_mode)
    bundle_path = tmp_path / "bundle"
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-backup: interrupted" in error
    assert "private stage retained at" in error
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1


def test_recovery_stage_mkdir_interrupt_retains_created_entry_and_exit_130(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    real_mkdir = recovery.os.mkdir

    def mkdir_then_interrupt(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith(".bundle."):
            raise KeyboardInterrupt

    monkeypatch.setattr(recovery.os, "mkdir", mkdir_then_interrupt)
    bundle_path = tmp_path / "bundle"
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-backup: interrupted" in error
    assert "private stage retained at" in error
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1


def test_recovery_stage_return_interrupt_retains_owned_stage_and_closes_descriptor(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    real_make_stage = recovery._make_private_stage
    allocated_descriptor = None

    def allocate_then_interrupt(destination, *, owner=None):
        nonlocal allocated_descriptor
        stage = real_make_stage(destination, owner=owner)
        allocated_descriptor = stage.descriptor
        raise KeyboardInterrupt

    monkeypatch.setattr(recovery, "_make_private_stage", allocate_then_interrupt)
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-backup: interrupted" in error
    assert "private stage retained at" in error
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert str(retained[0]) in error
    assert allocated_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(allocated_descriptor)


def test_recovery_backup_interrupt_reports_retained_stage_and_exit_130(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"

    def interrupt_snapshot(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(recovery, "_snapshot_recovery_database", interrupt_snapshot)
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-backup: interrupted" in error
    assert "private stage retained at" in error
    assert len(list(tmp_path.glob(".bundle.*.tmp"))) == 1
    assert not bundle_path.exists()


def test_recovery_backup_interrupt_after_publication_rolls_back_to_stage(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    real_publish = recovery._publish_private_stage

    def publish_then_interrupt(stage, destination, *, protected):
        real_publish(stage, destination, protected=protected)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        recovery,
        "_publish_private_stage",
        publish_then_interrupt,
    )
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-backup: interrupted" in error
    assert "private stage retained at" in error
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert recovery.verify_recovery_bundle(retained[0]).attachment_count == 1


def test_recovery_bundle_result_interrupt_happens_before_commit(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    real_result = recovery.RecoveryBundle
    constructions = 0

    def interrupt_second_result(*args, **kwargs):
        nonlocal constructions
        constructions += 1
        if constructions == 2:
            raise KeyboardInterrupt
        return real_result(*args, **kwargs)

    monkeypatch.setattr(recovery, "RecoveryBundle", interrupt_second_result)
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    monkeypatch.setattr(recovery, "RecoveryBundle", real_result)
    error = capsys.readouterr().err
    assert constructions == 2
    assert "athena-backup: interrupted" in error
    assert "private stage retained at" in error
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert recovery.verify_recovery_bundle(retained[0]).attachment_count == 1


def test_recovery_backup_interrupt_after_commit_reports_committed_path(
    tmp_path, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    previous_trace = sys.gettrace()

    def interrupt_committed_return(frame, event, arg):
        if (
            event == "line"
            and frame.f_code is recovery.create_recovery_bundle.__code__
            and frame.f_locals.get("committed") is True
        ):
            sys.settrace(previous_trace)
            raise KeyboardInterrupt
        return interrupt_committed_return

    sys.settrace(interrupt_committed_return)
    try:
        result = ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
    finally:
        sys.settrace(previous_trace)

    error = capsys.readouterr().err
    assert result == 130
    assert "athena-backup: interrupted" in error
    assert f"recovery artifact committed at {bundle_path}" in error
    assert "preserve and verify it" in error
    assert not list(tmp_path.glob(".bundle.*.tmp"))
    assert recovery.verify_recovery_bundle(bundle_path).attachment_count == 1


def test_recovery_backup_caller_interrupt_reports_existing_destination_unknown(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    real_create = ops.create_recovery_bundle

    def create_then_interrupt(*args, **kwargs):
        real_create(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(ops, "create_recovery_bundle", create_then_interrupt)
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-backup: interrupted" in error
    assert "publication ownership and durability are unknown" in error
    assert str(bundle_path) in error
    assert not list(tmp_path.glob(".bundle.*.tmp"))
    assert recovery.verify_recovery_bundle(bundle_path).attachment_count == 1


def test_recovery_doctor_interrupt_reports_retained_stage_and_exit_130(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    drill_path = tmp_path / "drill"

    def interrupt_copy(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(recovery, "_copy_verified_database", interrupt_copy)
    assert (
        ops.doctor_main(
            [
                str(bundle_path),
                "--recovery-bundle",
                "--drill-dir",
                str(drill_path),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-doctor: interrupted" in error
    assert "private stage retained at" in error
    assert len(list(tmp_path.glob(".drill.*.tmp"))) == 1
    assert not drill_path.exists()
    assert recovery.verify_recovery_bundle(bundle_path).attachment_count == 1


def test_recovery_drill_stage_return_interrupt_reports_stage_and_closes_descriptor(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    source_before = _tree_evidence(bundle_path)
    drill_path = tmp_path / "drill"
    real_make_stage = recovery._make_private_stage
    allocated_descriptor = None

    def allocate_then_interrupt(destination, *, owner=None):
        nonlocal allocated_descriptor
        stage = real_make_stage(destination, owner=owner)
        allocated_descriptor = stage.descriptor
        raise KeyboardInterrupt

    monkeypatch.setattr(recovery, "_make_private_stage", allocate_then_interrupt)
    assert (
        ops.doctor_main(
            [
                str(bundle_path),
                "--recovery-bundle",
                "--drill-dir",
                str(drill_path),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert "athena-doctor: interrupted" in error
    assert "private stage retained at" in error
    assert not drill_path.exists()
    retained = list(tmp_path.glob(".drill.*.tmp"))
    assert len(retained) == 1
    assert str(retained[0]) in error
    assert allocated_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(allocated_descriptor)
    assert _tree_evidence(bundle_path) == source_before


def test_recovery_drill_result_interrupt_happens_before_commit(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    drill_path = tmp_path / "drill"
    real_result = recovery.RecoveryDrill

    def interrupt_result(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(recovery, "RecoveryDrill", interrupt_result)
    assert (
        ops.doctor_main(
            [
                str(bundle_path),
                "--recovery-bundle",
                "--drill-dir",
                str(drill_path),
            ]
        )
        == 130
    )

    monkeypatch.setattr(recovery, "RecoveryDrill", real_result)
    error = capsys.readouterr().err
    assert "athena-doctor: interrupted" in error
    assert "private stage retained at" in error
    assert not drill_path.exists()
    retained = list(tmp_path.glob(".drill.*.tmp"))
    assert len(retained) == 1
    assert (retained[0] / "athena.db").read_bytes() == (
        bundle_path / "athena.db"
    ).read_bytes()
    assert recovery.verify_recovery_bundle(bundle_path).attachment_count == 1


def test_recovery_drill_interrupt_after_commit_reports_committed_path(tmp_path, capsys):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    drill_path = tmp_path / "drill"
    previous_trace = sys.gettrace()

    def interrupt_committed_return(frame, event, arg):
        if (
            event == "line"
            and frame.f_code is recovery.materialize_recovery_bundle.__code__
            and frame.f_locals.get("committed") is True
        ):
            sys.settrace(previous_trace)
            raise KeyboardInterrupt
        return interrupt_committed_return

    sys.settrace(interrupt_committed_return)
    try:
        result = ops.doctor_main(
            [
                str(bundle_path),
                "--recovery-bundle",
                "--drill-dir",
                str(drill_path),
            ]
        )
    finally:
        sys.settrace(previous_trace)

    error = capsys.readouterr().err
    assert result == 130
    assert "athena-doctor: interrupted" in error
    assert f"recovery artifact committed at {drill_path}" in error
    assert "preserve and verify it" in error
    assert not list(tmp_path.glob(".drill.*.tmp"))
    assert (drill_path / "athena.db").read_bytes() == (
        bundle_path / "athena.db"
    ).read_bytes()
    assert recovery.verify_recovery_bundle(bundle_path).attachment_count == 1


def test_recovery_capture_preserves_published_bundle_on_parent_fsync_failure(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    real_fsync = recovery.os.fsync
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)

    def fail_after_publication(descriptor):
        metadata = os.fstat(descriptor)
        if (
            metadata.st_dev,
            metadata.st_ino,
        ) == parent_identity and bundle_path.exists():
            raise OSError("injected parent fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(recovery.os, "fsync", fail_after_publication)
    with pytest.raises(
        recovery.RecoveryBundleDurabilityError,
        match="durability could not be confirmed",
    ):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    monkeypatch.setattr(recovery.os, "fsync", real_fsync)
    assert recovery.verify_recovery_bundle(bundle_path).attachment_count == 1
    assert not list(tmp_path.glob(".bundle.*.tmp"))


def test_recovery_backup_interrupt_during_publication_fsync_rolls_back(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    real_fsync = recovery.os.fsync
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    interrupted = False

    def interrupt_publication_fsync(descriptor):
        nonlocal interrupted
        metadata = os.fstat(descriptor)
        if (
            not interrupted
            and (metadata.st_dev, metadata.st_ino) == parent_identity
            and bundle_path.exists()
        ):
            interrupted = True
            raise KeyboardInterrupt
        return real_fsync(descriptor)

    monkeypatch.setattr(recovery.os, "fsync", interrupt_publication_fsync)
    assert (
        ops.backup_main(
            [
                str(database),
                str(bundle_path),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 130
    )

    error = capsys.readouterr().err
    assert interrupted
    assert "athena-backup: interrupted" in error
    assert "private stage retained at" in error
    assert not bundle_path.exists()
    retained = list(tmp_path.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    assert recovery.verify_recovery_bundle(retained[0]).attachment_count == 1


def test_recovery_capture_rolls_back_parent_move_during_publication_fsync(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    publish_parent = tmp_path / "bundle-parent"
    publish_parent.mkdir()
    bundle_path = publish_parent / "bundle"
    moved_parent = attach_dir / publish_parent.name
    parent_identity = (publish_parent.stat().st_dev, publish_parent.stat().st_ino)
    real_fsync = recovery.os.fsync
    moved = False

    def move_parent_and_fail(descriptor):
        nonlocal moved
        metadata = os.fstat(descriptor)
        if (
            not moved
            and (metadata.st_dev, metadata.st_ino) == parent_identity
            and recovery._entry_exists(descriptor, bundle_path.name)
        ):
            publish_parent.rename(moved_parent)
            moved = True
            raise OSError("injected parent fsync race")
        return real_fsync(descriptor)

    monkeypatch.setattr(recovery.os, "fsync", move_parent_and_fail)
    with pytest.raises(recovery.RecoveryRaceError, match="rolled back"):
        recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    assert not (moved_parent / "bundle").exists()
    retained = list(moved_parent.glob(".bundle.*.tmp"))
    assert len(retained) == 1
    moved_parent.rename(publish_parent)
    assert not bundle_path.exists()


def test_recovery_capture_prepublication_failure_retains_only_its_private_stage(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    unrelated = tmp_path / ".bundle.unrelated.tmp"
    unrelated.mkdir()
    (unrelated / "sentinel").write_bytes(b"preserve")

    def fail_copy(*args, **kwargs):
        raise OSError("injected attachment copy failure")

    monkeypatch.setattr(recovery, "_copy_attachment", fail_copy)
    with pytest.raises(OSError, match="injected attachment copy failure"):
        recovery.create_recovery_bundle(database, attach_dir, tmp_path / "bundle")

    assert (unrelated / "sentinel").read_bytes() == b"preserve"
    retained = [path for path in tmp_path.glob(".bundle.*.tmp") if path != unrelated]
    assert len(retained) == 1
    assert stat.S_IMODE(retained[0].stat().st_mode) == 0o700


def test_recovery_verification_and_drill_do_not_mutate_source_bundle(tmp_path):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    before = _tree_evidence(bundle_path)

    first = recovery.verify_recovery_bundle(bundle_path)
    second = recovery.verify_recovery_bundle(bundle_path)
    drill_path = tmp_path / "drill"
    drill = recovery.materialize_recovery_bundle(bundle_path, drill_path)

    assert first == second == drill.source_bundle
    assert _tree_evidence(bundle_path) == before
    assert (drill_path / "athena.db").read_bytes() == (
        bundle_path / "athena.db"
    ).read_bytes()
    assert not (bundle_path / "athena.db-wal").exists()
    assert not (bundle_path / "athena.db-shm").exists()
    assert not list(bundle_path.rglob(".athena-doctor-*"))
    assert (drill_path / "attachments" / names[0]).read_bytes() == b"evidence"
    assert stat.S_IMODE(drill_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((drill_path / "athena.db").stat().st_mode) == 0o600


def test_recovery_restore_uses_writable_drill_and_preserves_readonly_bundle(tmp_path):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    source_summary = _database_summary(database)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    drill_path = tmp_path / "restore-candidate"
    recovery.materialize_recovery_bundle(bundle_path, drill_path)

    os.chmod(bundle_path, 0o500)
    bundle_before = _tree_evidence(bundle_path)
    restored_database = tmp_path / "restored.db"
    try:
        backup.restore_database(drill_path / "athena.db", restored_database)
        assert _database_summary(restored_database) == source_summary
        assert _tree_evidence(bundle_path) == bundle_before
        assert not (bundle_path / "athena.db-wal").exists()
        assert not (bundle_path / "athena.db-shm").exists()
        assert (drill_path / "attachments" / names[0]).read_bytes() == b"evidence"
    finally:
        os.chmod(bundle_path, 0o700)


@pytest.mark.parametrize("changed_directory", ["attachments", "root"])
def test_recovery_verifier_detects_tree_change_during_check(
    tmp_path, monkeypatch, changed_directory
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    real_stability_check = recovery._require_attachment_rows_stable

    def mutate_after_hash(directory_descriptor, initial):
        real_stability_check(directory_descriptor, initial)
        parent = (
            bundle_path / "attachments"
            if changed_directory == "attachments"
            else bundle_path
        )
        (parent / "appeared-during-check").write_bytes(b"race")

    monkeypatch.setattr(
        recovery,
        "_require_attachment_rows_stable",
        mutate_after_hash,
    )
    expected = (
        "attachment directory changed"
        if changed_directory == "attachments"
        else "recovery bundle changed"
    )
    with pytest.raises(ValueError, match=expected):
        recovery.verify_recovery_bundle(bundle_path)


def test_recovery_verifier_rejects_ancestor_replacement_before_return(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    verify_parent = tmp_path / "verify-parent"
    verify_parent.mkdir()
    bundle_path = verify_parent / "bundle"
    moved_parent = tmp_path / "moved-verify-parent"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    real_stability_check = recovery._require_attachment_rows_stable
    replaced = False

    def replace_ancestor_after_content_check(directory_descriptor, initial):
        nonlocal replaced
        real_stability_check(directory_descriptor, initial)
        verify_parent.rename(moved_parent)
        verify_parent.mkdir()
        bundle_path.mkdir(mode=0o700)
        (bundle_path / "sentinel").write_bytes(b"unverified")
        replaced = True

    monkeypatch.setattr(
        recovery,
        "_require_attachment_rows_stable",
        replace_ancestor_after_content_check,
    )
    with pytest.raises(
        recovery.RecoveryRaceError,
        match="recovery bundle path was replaced",
    ):
        recovery.verify_recovery_bundle(bundle_path)

    assert replaced
    assert (bundle_path / "sentinel").read_bytes() == b"unverified"


def test_recovery_drill_refuses_existing_nested_and_source_ancestor_paths(tmp_path):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        recovery.materialize_recovery_bundle(bundle_path, existing)
    with pytest.raises(ValueError, match="protected source tree"):
        recovery.materialize_recovery_bundle(bundle_path, bundle_path / "drill")
    with pytest.raises(FileExistsError):
        recovery.materialize_recovery_bundle(bundle_path, tmp_path)


def test_recovery_drill_rechecks_containment_inside_publication(tmp_path, monkeypatch):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    drill_parent = tmp_path / "drill-parent"
    drill_parent.mkdir()
    drill_path = drill_parent / "drill"
    moved_parent = bundle_path / drill_parent.name
    real_rename = recovery._rename_no_replace_at
    moved = False

    def move_parent_then_rename(parent_descriptor, source_name, destination_name):
        nonlocal moved
        if not moved and destination_name == drill_path.name:
            drill_parent.rename(moved_parent)
            moved = True
        real_rename(parent_descriptor, source_name, destination_name)

    monkeypatch.setattr(recovery, "_rename_no_replace_at", move_parent_then_rename)
    with pytest.raises(recovery.RecoveryRaceError, match="rolled back"):
        recovery.materialize_recovery_bundle(bundle_path, drill_path)

    assert not (moved_parent / "drill").exists()
    retained = list(moved_parent.glob(".drill.*.tmp"))
    assert len(retained) == 1
    moved_parent.rename(drill_parent)
    assert not drill_path.exists()
    assert recovery.verify_recovery_bundle(bundle_path).attachment_count == 1


def test_recovery_drill_failure_retains_private_stage_and_does_not_touch_bundle(
    tmp_path, monkeypatch
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    before = _tree_evidence(bundle_path)

    def fail_copy(*args, **kwargs):
        raise OSError("injected drill copy failure")

    monkeypatch.setattr(recovery, "_copy_attachment", fail_copy)
    drill_path = tmp_path / "drill"
    with pytest.raises(OSError, match="injected drill copy failure"):
        recovery.materialize_recovery_bundle(bundle_path, drill_path)

    assert not drill_path.exists()
    retained = list(tmp_path.glob(".drill.*.tmp"))
    assert len(retained) == 1
    assert stat.S_IMODE(retained[0].stat().st_mode) == 0o700
    assert _tree_evidence(bundle_path) == before


@pytest.mark.parametrize(
    "drift, expected",
    [
        ("root_layout", "unexpected layout"),
        ("database_content", "does not match its bundle"),
        ("attachment_layout", "attachment layout is incomplete"),
    ],
)
def test_recovery_drill_validates_materialized_pair_before_publication(
    tmp_path, monkeypatch, drift, expected
):
    database, attach_dir, _ = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    real_copy_database = recovery._copy_verified_database
    real_copy_attachment = recovery._copy_attachment

    if drift in {"root_layout", "database_content"}:

        def drift_database(source, destination):
            result = real_copy_database(source, destination)
            if drift == "root_layout":
                (destination.parent / "unexpected").write_bytes(b"drift")
            else:
                conn = sqlite3.connect(destination)
                conn.execute("UPDATE issues SET title = 'silently substituted'")
                conn.commit()
                conn.close()
            return result

        monkeypatch.setattr(recovery, "_copy_verified_database", drift_database)
    else:

        def remove_copied_attachment(source_descriptor, destination_descriptor, row):
            result = real_copy_attachment(
                source_descriptor,
                destination_descriptor,
                row,
            )
            os.unlink(row.stored_name, dir_fd=destination_descriptor)
            return result

        monkeypatch.setattr(recovery, "_copy_attachment", remove_copied_attachment)

    drill_path = tmp_path / "drill"
    with pytest.raises(ValueError, match=expected):
        recovery.materialize_recovery_bundle(bundle_path, drill_path)
    assert not drill_path.exists()
    retained = list(tmp_path.glob(".drill.*.tmp"))
    assert len(retained) == 1
    assert stat.S_IMODE(retained[0].stat().st_mode) == 0o700


def test_recovery_cli_stale_and_elapsed_thresholds_fail_closed(tmp_path, capsys):
    database, attach_dir, _ = _recovery_source(tmp_path)
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    manifest = _read_manifest(bundle_path)
    manifest["capture_started_at"] = "2000-01-01T00:00:00.000000Z"
    manifest["capture_completed_at"] = "2000-01-01T00:00:01.000000Z"
    _write_manifest(bundle_path, manifest)

    assert (
        ops.doctor_main(
            [
                str(bundle_path),
                "--recovery-bundle",
                "--max-recovery-point-age-seconds",
                "1",
            ]
        )
        == 1
    )
    assert "valid but stale" in capsys.readouterr().err

    fresh_bundle = tmp_path / "fresh-bundle"
    recovery.create_recovery_bundle(database, attach_dir, fresh_bundle)
    drill_path = tmp_path / "slow-drill"
    assert (
        ops.doctor_main(
            [
                str(fresh_bundle),
                "--recovery-bundle",
                "--drill-dir",
                str(drill_path),
                "--max-data-recovery-seconds",
                "0",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "completed but exceeded its limit" in error
    assert str(drill_path) in error
    assert drill_path.exists()


def test_recovery_cli_uses_the_materialized_bundle_for_its_age_decision(
    tmp_path, monkeypatch, capsys
):
    database, attach_dir, _ = _recovery_source(tmp_path)
    fresh_bundle = tmp_path / "fresh-bundle"
    stale_bundle = tmp_path / "stale-bundle"
    recovery.create_recovery_bundle(database, attach_dir, fresh_bundle)
    recovery.create_recovery_bundle(database, attach_dir, stale_bundle)
    manifest = _read_manifest(stale_bundle)
    manifest["capture_started_at"] = "2000-01-01T00:00:00.000000Z"
    manifest["capture_completed_at"] = "2000-01-01T00:00:01.000000Z"
    _write_manifest(stale_bundle, manifest)
    stale_summary = recovery.verify_recovery_bundle(stale_bundle)
    drill_path = tmp_path / "drill"

    def materialize_swapped_bundle(bundle_path, requested_drill_path):
        assert bundle_path == fresh_bundle
        requested_drill_path.mkdir(mode=0o700)
        return recovery.RecoveryDrill(
            path=requested_drill_path,
            source_bundle=stale_summary,
        )

    monkeypatch.setattr(
        ops,
        "materialize_recovery_bundle",
        materialize_swapped_bundle,
    )
    assert (
        ops.doctor_main(
            [
                str(fresh_bundle),
                "--recovery-bundle",
                "--drill-dir",
                str(drill_path),
                "--max-recovery-point-age-seconds",
                "1",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "valid but stale" in error
    assert f"drill retained at {drill_path}" in error


@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_recovery_doctor_failure_does_not_disclose_stored_name(
    tmp_path, capsys, failure
):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    bundle_path = tmp_path / "bundle"
    recovery.create_recovery_bundle(database, attach_dir, bundle_path)
    blob = bundle_path / "attachments" / names[0]
    if failure == "missing":
        blob.unlink()
    else:
        blob.write_bytes(b"tampered")

    assert ops.doctor_main([str(bundle_path), "--recovery-bundle"]) == 1
    error = capsys.readouterr().err
    expected = (
        "missing or unexpected entries" if failure == "missing" else "does not match"
    )
    assert expected in error
    assert names[0] not in error


def test_recovery_backup_cli_missing_blob_does_not_disclose_stored_name(
    tmp_path, capsys
):
    database, attach_dir, names = _recovery_source(tmp_path, b"evidence")
    (attach_dir / names[0]).unlink()

    assert (
        ops.backup_main(
            [
                str(database),
                str(tmp_path / "bundle"),
                "--recovery-bundle",
                "--attach-dir",
                str(attach_dir),
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "could not be copied" in error
    assert names[0] not in error
