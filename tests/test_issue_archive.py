"""Issue archival (soft-delete): hide closed/obsolete issues without destroying them.

Issues are never deleted (audit integrity), so archived_at is the soft-delete: a
non-NULL timestamp hides the issue from the default lists/boards while the row and
its whole history survive, and it can be restored. These pin the REST archive/
unarchive (gated, recorded), the default-hide + include_archived list behaviour,
and the web button + "Show archived" toggle.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # bootstrap admin
H2 = {"X-Athena-Actor": "2"}  # a second member


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "B", "role": "member"}, headers=H1
    )


def _issue(client, title="x", headers=H1):
    return client.post("/issues", json={"title": title}, headers=headers).json()["id"]


# --- REST API --------------------------------------------------------------


def test_archive_hides_from_default_list_and_unarchive_restores(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        _bootstrap(client)
        keep, gone = _issue(client, "keep"), _issue(client, "gone")

        archived = client.post(f"/issues/{gone}/archive", headers=H1)
        assert (
            archived.status_code == 200 and archived.json()["archived_at"] is not None
        )

        # Default list/feed hides archived; include_archived reveals it; the single
        # read still resolves it (archival is not deletion).
        assert [i["id"] for i in client.get("/issues", headers=H1).json()] == [keep]
        both = sorted(
            i["id"]
            for i in client.get("/issues?include_archived=true", headers=H1).json()
        )
        assert both == sorted([keep, gone])
        assert client.get(f"/issues/{gone}", headers=H1).status_code == 200

        restored = client.post(f"/issues/{gone}/unarchive", headers=H1)
        assert restored.json()["archived_at"] is None
        assert sorted(
            i["id"] for i in client.get("/issues", headers=H1).json()
        ) == sorted([keep, gone])


def test_archive_is_write_gated(tmp_path):
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        _bootstrap(client)
        mine = _issue(client, "mine")  # created by user 1
        # A non-author member can't archive it.
        assert client.post(f"/issues/{mine}/archive", headers=H2).status_code == 403
        # Unknown issue → 404.
        assert client.post("/issues/9999/archive", headers=H1).status_code == 404
        assert client.get(f"/issues/{mine}", headers=H1).json()["archived_at"] is None


def test_archive_records_activity_once(tmp_path):
    db_file = tmp_path / "trail.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        iid = _issue(client, "audit")
        client.post(f"/issues/{iid}/archive", headers=H1)
        client.post(f"/issues/{iid}/archive", headers=H1)  # idempotent re-archive
        client.post(f"/issues/{iid}/unarchive", headers=H1)
    conn = db.connect(db_file)
    verbs = [
        r["verb"]
        for r in conn.execute(
            "SELECT verb FROM activity WHERE target_kind='issue' AND target_id=? ORDER BY id",
            (iid,),
        ).fetchall()
    ]
    conn.close()
    # archived once (the second archive was a no-op change), then unarchived.
    assert verbs.count("archived") == 1 and verbs.count("unarchived") == 1


# --- Web -------------------------------------------------------------------


def _login(client, email="a@e.com", password="pw"):
    client.post("/users", json={"email": email, "name": "A", "password": password})
    r = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_archive_restore_and_list_toggle(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        _login(client)  # first user → admin
        iid = _issue(client, "archive me")

        # Detail offers Archive; archiving hides it from the default list.
        assert "Archive" in client.get(f"/aegis/issues/{iid}").text
        assert (
            client.post(
                f"/aegis/issues/{iid}/archive", follow_redirects=False
            ).status_code
            == 303
        )
        assert "archive me" not in client.get("/aegis/issues").text
        # The "Show archived" toggle brings it back into view.
        assert "archive me" in client.get("/aegis/issues?archived=1").text
        # Detail now shows the archived badge and a Restore button.
        detail = client.get(f"/aegis/issues/{iid}").text
        assert "archived-badge" in detail and "Restore" in detail

        assert (
            client.post(
                f"/aegis/issues/{iid}/unarchive", follow_redirects=False
            ).status_code
            == 303
        )
        assert "archive me" in client.get("/aegis/issues").text


def test_web_archive_requires_login(tmp_path):
    with TestClient(create_app(tmp_path / "anon.db")) as client:
        _bootstrap(client)
        iid = _issue(client, "x")
        anon = TestClient(client.app)
        # No session → 401 (CSRF dep allows the request through to the auth check;
        # an anonymous POST without a token is rejected before any write).
        assert anon.post(
            f"/aegis/issues/{iid}/archive", follow_redirects=False
        ).status_code in (401, 403)
        assert client.get(f"/issues/{iid}", headers=H1).json()["archived_at"] is None
