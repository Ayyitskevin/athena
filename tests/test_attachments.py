"""File attachments on issues and pages.

These encode the contract that matters: a file round-trips (upload -> list ->
download with the right bytes), the client's filename can never become a path
(traversal is neutralized) and the blob lands under a random name, downloads are
forced to be saved (not rendered inline), size/emptiness are enforced, only the
uploader can delete, viewers can't upload, and every change is audited.
"""

import os
from pathlib import Path
import sqlite3
import stat

import pytest

from athena import config
from athena.core import attachment_commands, attachments, db
from athena.mentor import pages, spaces
from athena.main import create_app
from fastapi.testclient import TestClient


def _app(tmp_path, name):
    db_file = tmp_path / name
    return create_app(db_file), db_file


def _admin(client):
    # First user via the bootstrap path becomes admin.
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def _file(name="notes.txt", data=b"hello world", ctype="text/plain"):
    return {"file": (name, data, ctype)}


H1 = {"X-Athena-Actor": "1"}


class _FaultInjectingConnection(sqlite3.Connection):
    fail_commit = False
    rollback_calls = 0

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("injected commit failure")
        super().commit()

    def rollback(self) -> None:
        self.rollback_calls += 1
        super().rollback()


def _storage_conn(tmp_path) -> _FaultInjectingConnection:
    conn = sqlite3.connect(tmp_path / "storage.db", factory=_FaultInjectingConnection)
    assert isinstance(conn, _FaultInjectingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('storage@e.com', 'Storage')")
    conn.commit()
    return conn


def _store_blob(
    conn: sqlite3.Connection,
    attach_dir: Path,
    *,
    data: bytes = b"hello",
    filename: str = "blob.bin",
) -> dict:
    return attachments.store(
        conn,
        target_kind="issue",
        target_id=1,
        filename=filename,
        content_type="application/octet-stream",
        data=data,
        uploaded_by=1,
        attach_dir=attach_dir,
    )


def _blob_path(conn: sqlite3.Connection, attach_dir: Path, attachment_id: int) -> Path:
    stored_name = attachments.get_stored_name(conn, attachment_id)
    assert stored_name is not None
    return attachments.disk_path(attach_dir, stored_name)


def test_store_uses_private_atomic_stage_and_syncs_file_and_directory(
    tmp_path, monkeypatch
):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "blobs"
    real_replace = Path.replace
    real_fsync = os.fsync
    replacements: list[tuple[str, int, bool, bytes]] = []
    synced_types: list[str] = []

    def inspect_replace(staged: Path, destination: Path) -> Path:
        destination = Path(destination)
        replacements.append(
            (
                staged.name,
                stat.S_IMODE(staged.stat().st_mode),
                destination.exists(),
                staged.read_bytes(),
            )
        )
        return real_replace(staged, destination)

    def inspect_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        synced_types.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(Path, "replace", inspect_replace)
    monkeypatch.setattr(attachments.os, "fsync", inspect_fsync)

    stored = _store_blob(conn, attach_dir, data=b"atomic bytes")
    blob = _blob_path(conn, attach_dir, stored["id"])

    assert replacements == [
        (
            replacements[0][0],
            0o600,
            False,
            b"atomic bytes",
        )
    ]
    assert replacements[0][0].startswith(f".{blob.name}.")
    assert replacements[0][0].endswith(".tmp")
    assert synced_types == ["file", "directory"]
    assert blob.read_bytes() == b"atomic bytes"
    assert list(attach_dir.glob(".*.tmp")) == []
    assert attachments.reconcile_storage(conn, attach_dir).ok is True
    conn.close()


def test_store_rejects_symlinked_storage_root_before_writing(tmp_path):
    conn = _storage_conn(tmp_path)
    outside = tmp_path / "outside-storage"
    outside.mkdir()
    attach_dir = tmp_path / "linked-storage"
    attach_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NotADirectoryError, match="attachment path is not a directory"):
        _store_blob(conn, attach_dir)

    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert list(outside.iterdir()) == []
    conn.close()


def test_store_insert_failure_rolls_back_without_creating_a_blob(tmp_path):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "insert-failure"

    with pytest.raises(sqlite3.IntegrityError):
        attachments.store(
            conn,
            target_kind="issue",
            target_id=1,
            filename="bad.bin",
            content_type="application/octet-stream",
            data=b"must not survive",
            uploaded_by=999,
            attach_dir=attach_dir,
        )

    assert conn.rollback_calls == 1
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert list(attach_dir.iterdir()) == []
    conn.close()


def test_store_write_failure_rolls_back_row_and_removes_private_stage(
    tmp_path, monkeypatch
):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "write-failure"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("injected staged write failure")

    monkeypatch.setattr(attachments.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="injected staged write failure"):
        _store_blob(conn, attach_dir)

    assert conn.rollback_calls == 1
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert list(attach_dir.iterdir()) == []
    conn.close()


def test_store_commit_failure_rolls_back_row_and_removes_published_blob(tmp_path):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "commit-failure"
    conn.fail_commit = True

    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        _store_blob(conn, attach_dir)

    assert conn.rollback_calls == 1
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert list(attach_dir.iterdir()) == []
    conn.close()


def test_delete_unlink_failure_is_raised_and_leftover_is_reconcilable(
    tmp_path, monkeypatch
):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "delete-failure"
    stored = _store_blob(conn, attach_dir)
    blob = _blob_path(conn, attach_dir, stored["id"])
    real_unlink = Path.unlink

    def fail_blob_unlink(path: Path, *args, **kwargs) -> None:
        if path == blob:
            raise PermissionError("injected unlink failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_blob_unlink)
    with pytest.raises(PermissionError, match="injected unlink failure"):
        attachments.delete(conn, stored["id"], attach_dir)

    assert attachments.get(conn, stored["id"]) is None
    assert blob.read_bytes() == b"hello"
    report = attachments.reconcile_storage(conn, attach_dir)
    assert report.orphan_files == (blob.name,)
    assert report.ok is False
    conn.close()


def test_bulk_unlink_attempts_every_blob_and_reports_failures(tmp_path, monkeypatch):
    attach_dir = tmp_path / "bulk-unlink"
    attach_dir.mkdir()
    first = "0" * 32 + ".bin"
    second = "1" * 32 + ".bin"
    first_path = attachments.disk_path(attach_dir, first)
    second_path = attachments.disk_path(attach_dir, second)
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    real_unlink = Path.unlink

    def fail_first(path: Path, *args, **kwargs) -> None:
        if path == first_path:
            raise PermissionError("injected bulk unlink failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first)
    with pytest.raises(attachments.BlobCleanupError) as raised:
        attachments.unlink_blobs(attach_dir, [first, second])

    assert [failure.stored_name for failure in raised.value.failures] == [first]
    assert first_path.exists()
    assert not second_path.exists()


def test_reconcile_storage_detects_missing_tampered_size_orphan_and_non_regular(
    tmp_path,
):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "integrity"
    healthy = _store_blob(conn, attach_dir, data=b"healthy", filename="healthy.bin")
    missing = _store_blob(conn, attach_dir, data=b"missing", filename="missing.bin")
    tampered = _store_blob(conn, attach_dir, data=b"same-size", filename="hash.bin")
    wrong_size = _store_blob(conn, attach_dir, data=b"short", filename="size.bin")
    linked = _store_blob(conn, attach_dir, data=b"linked", filename="link.bin")
    fifo = _store_blob(conn, attach_dir, data=b"fifo", filename="fifo.bin")

    _blob_path(conn, attach_dir, missing["id"]).unlink()
    _blob_path(conn, attach_dir, tampered["id"]).write_bytes(b"different")
    _blob_path(conn, attach_dir, wrong_size["id"]).write_bytes(b"much longer")

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must not be read or changed")
    linked_path = _blob_path(conn, attach_dir, linked["id"])
    linked_path.unlink()
    linked_path.symlink_to(outside)

    fifo_path = _blob_path(conn, attach_dir, fifo["id"])
    fifo_path.unlink()
    os.mkfifo(fifo_path)

    (attach_dir / "z-orphan.bin").write_bytes(b"z")
    (attach_dir / "a-orphan.bin").write_bytes(b"a")

    report = attachments.reconcile_storage(conn, attach_dir)

    assert report == attachments.reconcile_storage(conn, attach_dir)
    assert report.ok is False
    assert [finding.attachment_id for finding in report.missing] == [missing["id"]]
    assert [finding.attachment_id for finding in report.tampered] == [
        tampered["id"],
        wrong_size["id"],
    ]
    assert [finding.attachment_id for finding in report.size_mismatched] == [
        wrong_size["id"]
    ]
    assert [
        (finding.attachment_id, finding.file_type) for finding in report.non_regular
    ] == [(linked["id"], "symlink"), (fifo["id"], "fifo")]
    assert report.unreadable == ()
    assert report.orphan_files == ("a-orphan.bin", "z-orphan.bin")
    assert report.storage_root_problem is None
    assert _blob_path(conn, attach_dir, healthy["id"]).read_bytes() == b"healthy"
    assert outside.read_bytes() == b"must not be read or changed"
    conn.close()


def test_issue_attachment_roundtrip(tmp_path):
    app, _ = _app(tmp_path, "rt.db")
    with TestClient(app) as client:
        _admin(client)
        issue = client.post("/issues", json={"title": "ship"}, headers=H1).json()
        up = client.post(
            f"/issues/{issue['id']}/attachments", files=_file(), headers=H1
        )
        assert up.status_code == 201
        att = up.json()
        assert att["filename"] == "notes.txt"
        assert att["byte_size"] == len(b"hello world")
        assert att["content_type"] == "text/plain"
        assert len(att["sha256"]) == 64
        assert "stored_name" not in att  # internal disk detail never exposed

        listed = client.get(f"/issues/{issue['id']}/attachments", headers=H1).json()
        assert [a["id"] for a in listed] == [att["id"]]

        got = client.get(f"/attachments/{att['id']}")
        assert got.status_code == 200
        assert got.content == b"hello world"
        # Served as a download, never inline (so an uploaded .html can't execute).
        assert "attachment" in got.headers.get("content-disposition", "")


def test_empty_upload_is_rejected(tmp_path):
    app, _ = _app(tmp_path, "empty.db")
    with TestClient(app) as client:
        _admin(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        r = client.post(
            f"/issues/{issue['id']}/attachments",
            files=_file(data=b""),
            headers=H1,
        )
        assert r.status_code == 422


def test_oversize_upload_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ATTACH_MAX_BYTES", 8)
    app, _ = _app(tmp_path, "big.db")
    with TestClient(app) as client:
        _admin(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        r = client.post(
            f"/issues/{issue['id']}/attachments",
            files=_file(data=b"this is more than eight bytes"),
            headers=H1,
        )
        assert r.status_code == 413


def test_path_traversal_filename_is_neutralized(tmp_path):
    app, _ = _app(tmp_path, "trav.db")
    with TestClient(app) as client:
        _admin(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        att = client.post(
            f"/issues/{issue['id']}/attachments",
            files=_file(name="../../etc/passwd", data=b"x"),
            headers=H1,
        ).json()
        # The display name is just the basename — no path survives.
        assert att["filename"] == "passwd"
        # On disk: one blob, under a random name, never a nested traversal path.
        stored = list(config.ATTACH_DIR.iterdir())
        assert len(stored) == 1
        assert stored[0].name != "passwd"
        assert "etc" not in stored[0].name
        # And it is still retrievable by id.
        assert client.get(f"/attachments/{att['id']}").content == b"x"


def test_page_attachment_roundtrip(tmp_path):
    app, _ = _app(tmp_path, "page.db")
    with TestClient(app) as client:
        _admin(client)
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
        ).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Doc"}, headers=H1
        ).json()
        att = client.post(
            f"/pages/{pg['id']}/attachments",
            files=_file(name="diagram.png", data=b"PNGDATA", ctype="image/png"),
            headers=H1,
        ).json()
        assert att["target_kind"] == "page"
        listed = client.get(f"/pages/{pg['id']}/attachments", headers=H1).json()
        assert listed[0]["filename"] == "diagram.png"
        assert client.get(f"/attachments/{att['id']}").content == b"PNGDATA"


def test_delete_is_uploader_only_and_audited(tmp_path):
    app, _ = _app(tmp_path, "del.db")
    with TestClient(app) as client:
        _admin(client)
        # A second user (member) who is NOT the uploader.
        client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        att = client.post(
            f"/issues/{issue['id']}/attachments", files=_file(), headers=H1
        ).json()

        # User 2 cannot delete user 1's attachment.
        assert (
            client.delete(
                f"/attachments/{att['id']}", headers={"X-Athena-Actor": "2"}
            ).status_code
            == 403
        )
        # The uploader can; afterwards it's gone.
        assert client.delete(f"/attachments/{att['id']}", headers=H1).status_code == 204
        assert client.get(f"/attachments/{att['id']}").status_code == 404

        verbs = [e["verb"] for e in client.get("/activity", headers=H1).json()]
        assert "added_attachment" in verbs and "removed_attachment" in verbs


def test_cross_actor_run_binding_upload_rolls_back_row_and_blob(tmp_path):
    app, _ = _app(tmp_path, "upload-run-binding.db")
    with TestClient(app) as client:
        _admin(client)
        client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)
        foreign_run = {"X-Athena-Actor": "2", "X-Athena-Run": "foreign-run"}
        issue = client.post(
            "/issues", json={"title": "owned by B"}, headers=foreign_run
        ).json()

        refused = client.post(
            f"/issues/{issue['id']}/attachments",
            files=_file(data=b"must roll back"),
            headers={**H1, "X-Athena-Run": "foreign-run"},
        )

        assert refused.status_code == 403
        assert refused.json()["detail"] == "run 'foreign-run' is bound to another actor"
        assert client.get(f"/issues/{issue['id']}/attachments", headers=H1).json() == []
        assert config.ATTACH_DIR.is_dir()
        assert list(config.ATTACH_DIR.iterdir()) == []
        events = client.get("/activity?run_id=foreign-run", headers=H1).json()
        assert [event["actor_id"] for event in events] == [2]
        assert all(event["verb"] != "added_attachment" for event in events)


def test_cross_actor_run_binding_delete_rolls_back_row_blob_and_audit(tmp_path):
    app, db_file = _app(tmp_path, "delete-run-binding.db")
    with TestClient(app) as client:
        _admin(client)
        client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)
        issue = client.post("/issues", json={"title": "kept"}, headers=H1).json()
        attachment = client.post(
            f"/issues/{issue['id']}/attachments", files=_file(), headers=H1
        ).json()
        conn = db.connect(db_file)
        blob = _blob_path(conn, config.ATTACH_DIR, attachment["id"])
        conn.close()
        foreign_run = {"X-Athena-Actor": "2", "X-Athena-Run": "foreign-run"}
        client.post("/issues", json={"title": "binder"}, headers=foreign_run)

        refused = client.delete(
            f"/attachments/{attachment['id']}",
            headers={**H1, "X-Athena-Run": "foreign-run"},
        )

        assert refused.status_code == 403
        assert refused.json()["detail"] == "run 'foreign-run' is bound to another actor"
        assert client.get(f"/attachments/{attachment['id']}").content == b"hello world"
        assert blob.read_bytes() == b"hello world"
        listed = client.get(f"/issues/{issue['id']}/attachments", headers=H1).json()
        assert [item["id"] for item in listed] == [attachment["id"]]
        events = client.get("/activity?run_id=foreign-run", headers=H1).json()
        assert [event["actor_id"] for event in events] == [2]
        assert all(event["verb"] != "removed_attachment" for event in events)


def test_command_commit_failure_rolls_back_row_and_published_blob(tmp_path):
    conn = _storage_conn(tmp_path)
    space = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    page = pages.create_page(
        conn, space_id=space["id"], title="Doc", body="", created_by=1
    )
    attach_dir = tmp_path / "command-commit-failure"
    conn.fail_commit = True

    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        attachment_commands.create_attachment(
            conn,
            actor={"id": 1, "role": "admin"},
            target_kind="page",
            target_id=page["id"],
            filename="failed.bin",
            content_type="application/octet-stream",
            data=b"must not survive",
            attach_dir=attach_dir,
        )

    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert list(attach_dir.iterdir()) == []
    conn.close()


def test_delete_unlink_failure_keeps_committed_audit(tmp_path, monkeypatch):
    app, db_file = _app(tmp_path, "delete-unlink-audit.db")
    with TestClient(app, raise_server_exceptions=False) as client:
        _admin(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        attachment = client.post(
            f"/issues/{issue['id']}/attachments", files=_file(), headers=H1
        ).json()
        conn = db.connect(db_file)
        blob = _blob_path(conn, config.ATTACH_DIR, attachment["id"])
        conn.close()
        real_unlink = Path.unlink

        def fail_blob_unlink(path: Path, *args, **kwargs) -> None:
            if path == blob:
                raise PermissionError("injected unlink failure")
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_blob_unlink)
        response = client.delete(f"/attachments/{attachment['id']}", headers=H1)

        assert response.status_code == 500
        assert client.get(f"/attachments/{attachment['id']}").status_code == 404
        assert client.get(f"/issues/{issue['id']}/attachments", headers=H1).json() == []
        assert blob.read_bytes() == b"hello world"
        removed = [
            event
            for event in client.get("/activity", headers=H1).json()
            if event["verb"] == "removed_attachment"
        ]
        assert len(removed) == 1
        assert removed[0]["actor_id"] == 1
        assert removed[0]["target_kind"] == "issue"
        assert removed[0]["target_id"] == issue["id"]
        assert removed[0]["detail"] == "notes.txt"


def test_download_rejects_symlinked_blob(tmp_path):
    app, db_file = _app(tmp_path, "symlink-download.db")
    with TestClient(app) as client:
        _admin(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        attachment = client.post(
            f"/issues/{issue['id']}/attachments", files=_file(), headers=H1
        ).json()
        conn = db.connect(db_file)
        blob = _blob_path(conn, config.ATTACH_DIR, attachment["id"])
        conn.close()
        outside = tmp_path / "outside-secret"
        outside.write_bytes(b"do not disclose")
        blob.unlink()
        blob.symlink_to(outside)

        response = client.get(f"/attachments/{attachment['id']}")

        assert response.status_code == 404
        assert b"do not disclose" not in response.content
        assert outside.read_bytes() == b"do not disclose"


def test_attachment_commands_reject_unsafe_context_and_invisible_targets(tmp_path):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "command-rejections"
    actor = {"id": 1, "role": "admin"}

    conn.execute("BEGIN")
    with pytest.raises(RuntimeError, match="require a fresh connection"):
        attachment_commands.create_attachment(
            conn,
            actor=actor,
            target_kind="issue",
            target_id=999,
            filename="nested.bin",
            content_type="application/octet-stream",
            data=b"rejected",
            attach_dir=attach_dir,
        )
    conn.rollback()

    with pytest.raises(attachment_commands.AttachmentCommandError) as invalid:
        attachment_commands.create_attachment(
            conn,
            actor=actor,
            target_kind="unknown",
            target_id=1,
            filename="invalid.bin",
            content_type="application/octet-stream",
            data=b"rejected",
            attach_dir=attach_dir,
        )
    assert invalid.value.status_code == 422

    with pytest.raises(attachment_commands.AttachmentCommandError) as hidden:
        attachment_commands.create_attachment(
            conn,
            actor=actor,
            target_kind="issue",
            target_id=999,
            filename="hidden.bin",
            content_type="application/octet-stream",
            data=b"rejected",
            attach_dir=attach_dir,
        )
    assert hidden.value.status_code == 404

    with pytest.raises(attachment_commands.AttachmentCommandError) as missing:
        attachment_commands.remove_attachment(
            conn,
            actor=actor,
            attachment_id=999,
            attach_dir=attach_dir,
        )
    assert missing.value.status_code == 404

    issue_id = conn.execute(
        "INSERT INTO issues (title, created_by) VALUES ('visible', 1)"
    ).lastrowid
    assert issue_id is not None
    conn.execute("INSERT INTO users (email, name) VALUES ('other@e.com', 'Other')")
    conn.commit()
    attachment = attachments.store(
        conn,
        target_kind="issue",
        target_id=issue_id,
        filename="owned.bin",
        content_type="application/octet-stream",
        data=b"owned",
        uploaded_by=1,
        attach_dir=attach_dir,
    )

    with pytest.raises(attachment_commands.AttachmentCommandError) as forbidden:
        attachment_commands.remove_attachment(
            conn,
            actor={"id": 2, "role": "member"},
            attachment_id=attachment["id"],
            attach_dir=attach_dir,
        )
    assert forbidden.value.status_code == 403
    assert attachments.get(conn, attachment["id"]) is not None
    conn.close()


def test_attachment_command_reports_audit_and_blob_rollback_failures(
    tmp_path, monkeypatch
):
    conn = _storage_conn(tmp_path)
    issue_id = conn.execute(
        "INSERT INTO issues (title, created_by) VALUES ('target', 1)"
    ).lastrowid
    assert issue_id is not None
    conn.commit()
    attach_dir = tmp_path / "double-failure"

    def fail_audit(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    def fail_cleanup(*args, **kwargs):
        raise OSError("injected blob rollback failure")

    monkeypatch.setattr(attachment_commands.activity, "record", fail_audit)
    monkeypatch.setattr(attachments, "unlink_blobs", fail_cleanup)

    with pytest.raises(BaseExceptionGroup) as raised:
        attachment_commands.create_attachment(
            conn,
            actor={"id": 1, "role": "admin"},
            target_kind="issue",
            target_id=issue_id,
            filename="evidence.bin",
            content_type="application/octet-stream",
            data=b"evidence",
            attach_dir=attach_dir,
        )

    assert [str(error) for error in raised.value.exceptions] == [
        "injected audit failure",
        "injected blob rollback failure",
    ]
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert len(list(attach_dir.iterdir())) == 1
    conn.close()


def test_insert_and_publish_removes_blob_when_post_publish_lookup_fails(
    tmp_path, monkeypatch
):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "post-publish-failure"
    monkeypatch.setattr(attachments, "get", lambda *_args, **_kwargs: None)

    with pytest.raises(AssertionError):
        _store_blob(conn, attach_dir, data=b"must not survive")

    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert list(attach_dir.iterdir()) == []
    conn.close()


def test_delete_handles_missing_rows_commit_failure_and_idempotent_unlink(tmp_path):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "delete-boundaries"
    assert attachments.delete(conn, 999, attach_dir) is False

    stored = _store_blob(conn, attach_dir)
    stored_name = attachments.get_stored_name(conn, stored["id"])
    assert stored_name is not None
    blob = attachments.disk_path(attach_dir, stored_name)
    conn.fail_commit = True
    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        attachments.delete(conn, stored["id"], attach_dir)

    conn.fail_commit = False
    assert attachments.get(conn, stored["id"]) is not None
    assert blob.read_bytes() == b"hello"
    assert attachments.delete(conn, stored["id"], attach_dir) is True
    assert attachments.get(conn, stored["id"]) is None
    assert not blob.exists()
    attachments.unlink_blobs(attach_dir, [stored_name])
    conn.close()


def test_open_blob_rejects_invalid_size_fifo_and_unsupported_platform(
    tmp_path, monkeypatch
):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "descriptor-open"
    stored = _store_blob(conn, attach_dir, data=b"regular")
    stored_name = attachments.get_stored_name(conn, stored["id"])
    assert stored_name is not None
    blob = attachments.disk_path(attach_dir, stored_name)

    with pytest.raises(ValueError, match="invalid attachment stored name"):
        attachments.open_blob(attach_dir, "../escape", expected_size=1)
    with pytest.raises(OSError, match="size does not match metadata"):
        attachments.open_blob(attach_dir, stored_name, expected_size=999)

    blob.unlink()
    os.mkfifo(blob)
    with pytest.raises(OSError, match="not a regular file"):
        attachments.open_blob(attach_dir, stored_name, expected_size=0)
    blob.unlink()

    monkeypatch.delattr(attachments.os, "O_NOFOLLOW")
    with pytest.raises(OSError, match="cannot open attachment blobs without symlinks"):
        attachments.open_blob(attach_dir, stored_name, expected_size=0)
    conn.close()


def test_download_uses_rfc5987_for_non_ascii_filename(tmp_path):
    app, _ = _app(tmp_path, "unicode-download.db")
    with TestClient(app) as client:
        _admin(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        attachment = client.post(
            f"/issues/{issue['id']}/attachments",
            files=_file(name="résumé 2026.txt", data=b"cv"),
            headers=H1,
        ).json()

        response = client.get(f"/attachments/{attachment['id']}")

        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            "attachment; filename*=utf-8''r%C3%A9sum%C3%A9%202026.txt"
        )


def test_reconcile_storage_reports_corrupt_rows_and_root_types(tmp_path):
    conn = _storage_conn(tmp_path)
    conn.execute(
        "INSERT INTO attachments "
        "(target_kind, target_id, filename, content_type, byte_size, sha256, "
        "stored_name, uploaded_by) VALUES ('issue', 1, 'bad', 'text/plain', 0, "
        "?, '../invalid', 1)",
        ("0" * 64,),
    )
    conn.commit()
    attach_dir = tmp_path / "missing-root"

    missing_root = attachments.reconcile_storage(conn, attach_dir)
    assert missing_root.missing == ()
    assert [(item.stored_name, item.reason) for item in missing_root.unreadable] == [
        ("../invalid", "invalid_stored_name")
    ]
    assert missing_root.storage_root_problem is None

    attach_dir.write_bytes(b"not a directory")
    wrong_type = attachments.reconcile_storage(conn, attach_dir)
    assert wrong_type.storage_root_problem == "non_regular"
    conn.close()


def test_reconcile_storage_reports_root_io_and_hash_races(tmp_path, monkeypatch):
    conn = _storage_conn(tmp_path)
    attach_dir = tmp_path / "reconcile-races"
    stored = _store_blob(conn, attach_dir, data=b"stable")
    stored_name = attachments.get_stored_name(conn, stored["id"])
    assert stored_name is not None

    real_open = attachments.os.open

    def fail_root_open(path, flags, *args, **kwargs):
        if Path(path) == attach_dir:
            raise PermissionError(13, "injected root open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(attachments.os, "open", fail_root_open)
    root_failure = attachments.reconcile_storage(conn, attach_dir)
    assert root_failure.storage_root_problem == "os_error:13"
    monkeypatch.setattr(attachments.os, "open", real_open)

    real_scandir = attachments.os.scandir

    def fail_scandir(*args, **kwargs):
        raise PermissionError(13, "injected scandir failure")

    monkeypatch.setattr(attachments.os, "scandir", fail_scandir)
    scan_failure = attachments.reconcile_storage(conn, attach_dir)
    assert scan_failure.storage_root_problem == "os_error:13"
    monkeypatch.setattr(attachments.os, "scandir", real_scandir)

    def vanished(*args, **kwargs):
        raise FileNotFoundError("injected hash race")

    monkeypatch.setattr(attachments, "_hash_regular_entry", vanished)
    vanished_report = attachments.reconcile_storage(conn, attach_dir)
    assert [item.attachment_id for item in vanished_report.missing] == [stored["id"]]

    def unreadable(*args, **kwargs):
        raise PermissionError(13, "injected hash read failure")

    monkeypatch.setattr(attachments, "_hash_regular_entry", unreadable)
    unreadable_report = attachments.reconcile_storage(conn, attach_dir)
    assert [item.reason for item in unreadable_report.unreadable] == ["os_error:13"]

    monkeypatch.setattr(
        attachments,
        "_hash_regular_entry",
        lambda *_args, **_kwargs: attachments.AttachmentNonRegularEntry(
            attachment_id=None,
            stored_name=stored_name,
            file_type="fifo",
        ),
    )
    replaced_report = attachments.reconcile_storage(conn, attach_dir)
    assert [
        (item.attachment_id, item.stored_name, item.file_type)
        for item in replaced_report.non_regular
    ] == [(stored["id"], stored_name, "fifo")]
    conn.close()


def test_viewer_cannot_upload(tmp_path):
    app, _ = _app(tmp_path, "viewer.db")
    with TestClient(app) as client:
        _admin(client)
        # Create a viewer (read-only) user.
        client.post(
            "/users",
            json={"email": "v@e.com", "name": "V", "role": "viewer"},
            headers=H1,
        )
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        r = client.post(
            f"/issues/{issue['id']}/attachments",
            files=_file(),
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 403


def _login(client):
    """Bootstrap + browser login; return the session's CSRF token (readable cookie),
    which the web write routes require back as an X-CSRF-Token header."""
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    client.post("/login", data={"email": "a@e.com", "password": "pw"})
    return client.cookies.get("athena_csrf")


def test_web_issue_attachment_upload_renders_and_downloads(tmp_path):
    app, _ = _app(tmp_path, "webiss.db")
    with TestClient(app) as client:
        csrf = _login(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        # Upload through the browser path (session cookie + CSRF header).
        done = client.post(
            f"/aegis/issues/{issue['id']}/attachments",
            files=_file(name="spec.txt", data=b"abc"),
            headers={"X-CSRF-Token": csrf},
        )
        # TestClient follows the 303 redirect to the issue page.
        assert done.status_code == 200
        assert "spec.txt" in done.text
        att = client.get(f"/issues/{issue['id']}/attachments", headers=H1).json()[0]
        assert f"/attachments/{att['id']}" in done.text
        assert client.get(f"/attachments/{att['id']}").content == b"abc"


def test_web_page_attachment_upload_renders(tmp_path):
    app, _ = _app(tmp_path, "webpg.db")
    with TestClient(app) as client:
        csrf = _login(client)
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
        ).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Doc"}, headers=H1
        ).json()
        done = client.post(
            f"/mentor/pages/{pg['id']}/attachments",
            files=_file(name="plan.md", data=b"# plan"),
            headers={"X-CSRF-Token": csrf},
        )
        assert done.status_code == 200
        assert "plan.md" in done.text


def test_upload_to_missing_target_404(tmp_path):
    app, _ = _app(tmp_path, "404.db")
    with TestClient(app) as client:
        _admin(client)
        assert (
            client.post(
                "/issues/999/attachments", files=_file(), headers=H1
            ).status_code
            == 404
        )
        assert (
            client.post("/pages/999/attachments", files=_file(), headers=H1).status_code
            == 404
        )
