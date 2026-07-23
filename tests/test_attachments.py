"""File attachments on issues and pages.

These encode the contract that matters: a file round-trips (upload -> list ->
download with the right bytes), the client's filename can never become a path
(traversal is neutralized) and the blob lands under a random name, downloads are
forced to be saved (not rendered inline), size/emptiness are enforced, only the
uploader can delete, viewers can't upload, and every change is audited.
"""

from athena import config
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
