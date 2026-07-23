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
from athena.core import attachments, db
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
