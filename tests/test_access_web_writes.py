"""Web (HTMX) write routes require visibility — the browser twin of the REST gate.

The detail pages are already read-gated (a private issue/page 404s), but the action
routes that POST from them — edit, status, comment, attachment, watch, label, page
move/delete, space edit — were gated only by can_modify / existence. A logged-in
outsider who knew an id could craft the POST. Now every web write funnels through a
visibility check (_issue_visible_or_404 / _authorize_issue_write for issues,
_page_visible_or_response / _authorize_project_write for pages & projects) and 404s for
anyone who can't see the container — no existence leak, no blind write.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _logout(client):
    client.post("/logout", follow_redirects=False)
    client.headers.pop("X-CSRF-Token", None)


def test_issue_web_writes_require_visibility(tmp_path):
    db_file = tmp_path / "iww.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        iid = client.post("/issues", json={"title": "Hidden", "project_id": pid}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pid,))
        conn.commit()

        # A logged-in outsider who can't see the issue gets 404 on every write path —
        # the modify-gated edit, the visibility-only comment, and the personal watch.
        _login(client, "o@e.com")
        assert client.post(f"/aegis/issues/{iid}/edit", data={"title": "x", "body": ""}, follow_redirects=False).status_code == 404
        assert client.post(f"/aegis/issues/{iid}/comments", data={"body": "x"}, follow_redirects=False).status_code == 404
        assert client.post(f"/aegis/issues/{iid}/watch", follow_redirects=False).status_code == 404

        # The creator (who can see it AND owns it) edits and comments — 303 back.
        _logout(client)
        _login(client, "c@e.com")
        assert client.post(f"/aegis/issues/{iid}/edit", data={"title": "ok", "body": ""}, follow_redirects=False).status_code == 303
        assert client.post(f"/aegis/issues/{iid}/comments", data={"body": "hi"}, follow_redirects=False).status_code == 303


def test_viewer_issue_write_probe_checks_visibility_before_role(tmp_path):
    db_file = tmp_path / "viewer-oracle.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        public_id = client.post(
            "/issues", json={"title": "Visible"}, headers=H_CREATOR
        ).json()["id"]
        project_id = client.post(
            "/projects",
            json={"name": "Secret", "key": "SEC"},
            headers=H_CREATOR,
        ).json()["id"]
        hidden_id = client.post(
            "/issues",
            json={"title": "Hidden", "project_id": project_id},
            headers=H_CREATOR,
        ).json()["id"]

        conn = db.connect(db_file)
        conn.execute(
            "UPDATE projects SET visibility = 'private' WHERE id = ?",
            (project_id,),
        )
        conn.execute("UPDATE users SET role = 'viewer' WHERE email = 'o@e.com'")
        conn.commit()
        conn.close()

        _login(client, "o@e.com")
        for issue_id in (hidden_id, 999999):
            assert client.get(f"/aegis/issues/{issue_id}/edit").status_code == 404
            assert (
                client.post(
                    f"/aegis/issues/{issue_id}/edit",
                    data={"title": "probe", "body": ""},
                    follow_redirects=False,
                ).status_code
                == 404
            )

        visible = client.get(f"/aegis/issues/{public_id}/edit")
        assert visible.status_code == 403
        assert "Viewer role is read-only" in visible.text


def test_project_edit_form_404s_for_outsider_no_leak(tmp_path):
    db_file = tmp_path / "pe.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pid,))
        conn.commit()
        # The edit form (and save) is creator-only; for a hidden project an outsider gets
        # 404 (not the 403 that would reveal the project exists).
        _login(client, "o@e.com")
        assert client.get(f"/aegis/projects/{pid}/edit").status_code == 404
        assert client.post(f"/aegis/projects/{pid}/edit", data={"name": "x", "key": "SEC", "description": ""}, follow_redirects=False).status_code == 404


def test_page_and_space_web_writes_require_visibility(tmp_path):
    db_file = tmp_path / "pww.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
        pgid = client.post(f"/spaces/{sid}/pages", json={"title": "P", "body": "v1"}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (sid,))
        conn.commit()

        # Logged-in outsider: every page/space write 404s.
        _login(client, "o@e.com")
        assert client.post(f"/mentor/pages/{pgid}/edit", data={"title": "x", "body": "y"}, follow_redirects=False).status_code == 404
        assert client.post(f"/mentor/pages/{pgid}/comments", data={"body": "x"}, follow_redirects=False).status_code == 404
        assert client.post(f"/mentor/pages/{pgid}/watch", follow_redirects=False).status_code == 404
        assert client.post(f"/mentor/spaces/{sid}/edit", data={"key": "SEC", "name": "x", "description": ""}, follow_redirects=False).status_code == 404
        assert client.post(f"/mentor/spaces/{sid}/pages", data={"title": "new", "body": "", "parent_id": ""}, follow_redirects=False).status_code == 404

        # Mentor is a shared wiki: a space MEMBER (who can see it) may edit a page — so
        # once the outsider is granted membership, the same edit 303s.
        from athena.core import access
        access.add_space_member(conn, sid, 3, added_by=2)
        assert client.post(f"/mentor/pages/{pgid}/edit", data={"title": "member", "body": "z"}, follow_redirects=False).status_code == 303
