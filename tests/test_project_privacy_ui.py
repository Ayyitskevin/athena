"""The project privacy web UI: the access page (visibility toggle + member roster).

The REST endpoints already drive privacy; this is the browser surface — a dedicated
/aegis/projects/{id}/access page (creator-OR-admin), a Private badge on the projects
list, and the add/remove member controls. Managing access is wider than edit/delete
(creator-only): an admin can manage any project, a plain member can't manage at all.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}


def _bootstrap(client):
    client.post(
        "/users",
        json={"email": "admin@e.com", "name": "Admin", "password": "pw"},
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "c@e.com",
            "name": "Creator",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "o@e.com",
            "name": "Outsider",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )


def _login(client, email):
    r = client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _logout(client):
    client.post("/logout", follow_redirects=False)
    client.headers.pop("X-CSRF-Token", None)


def test_toggle_visibility_and_manage_members_via_web(tmp_path):
    db_file = tmp_path / "ui.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]

        _login(client, "c@e.com")
        # The access page renders for the creator while the project is still public.
        page = client.get(f"/aegis/projects/{pid}/access")
        assert page.status_code == 200 and "Make private" in page.text

        # Flip it private from the web; the toggle redirects back to the access page.
        r = client.post(
            f"/aegis/projects/{pid}/visibility",
            data={"visibility": "private"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # The project is now private and the creator was auto-added to the roster.
        conn = db.connect(db_file)
        assert (
            conn.execute(
                "SELECT visibility FROM projects WHERE id=?", (pid,)
            ).fetchone()["visibility"]
            == "private"
        )
        access_page = client.get(f"/aegis/projects/{pid}/access").text
        assert "Make public" in access_page and "Creator" in access_page

        # Grant the outsider access, then revoke it. The roster's remove-form for a user
        # renders only while they're a member, so it's an unambiguous signal (their name
        # also appears in the add dropdown, so a bare name check wouldn't distinguish).
        remove_form = f"/aegis/projects/{pid}/members/3/delete"
        client.post(
            f"/aegis/projects/{pid}/members",
            data={"user_id": "3"},
            follow_redirects=False,
        )
        assert remove_form in client.get(f"/aegis/projects/{pid}/access").text
        client.post(remove_form, follow_redirects=False)
        assert remove_form not in client.get(f"/aegis/projects/{pid}/access").text

        # The projects list shows the lock badge to the creator.
        assert "badge-private" in client.get("/aegis/projects").text


def test_access_page_authz(tmp_path):
    db_file = tmp_path / "az.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pid,))
        conn.commit()
        url = f"/aegis/projects/{pid}/access"

        # Anonymous → 401.
        assert client.get(url).status_code == 401
        # An outsider who can't see the private project → 404 (no existence leak).
        _login(client, "o@e.com")
        assert client.get(url).status_code == 404
        assert (
            client.post(
                f"/aegis/projects/{pid}/visibility",
                data={"visibility": "public"},
                follow_redirects=False,
            ).status_code
            == 404
        )
        # An admin (not the creator) may manage access on any project → 200.
        _logout(client)
        _login(client, "admin@e.com")
        assert client.get(url).status_code == 200
        # A plain member can SEE the project but not manage its access → 403.
        from athena.core import access

        access.add_project_member(conn, pid, 3, added_by=2)
        _logout(client)
        _login(client, "o@e.com")
        assert client.get(url).status_code == 403
