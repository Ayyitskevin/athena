"""The space privacy web UI: the access page (visibility toggle + member roster).

The Mentor twin of the project access UI — a dedicated /mentor/spaces/{id}/access page
(creator-OR-admin), a Private badge on the spaces list, and add/remove member controls.
Managing access is wider than delete (creator-only): an admin manages any space, a plain
member can't manage at all.
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


def test_toggle_visibility_and_manage_members_via_web(tmp_path):
    db_file = tmp_path / "sui.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]

        _login(client, "c@e.com")
        page = client.get(f"/mentor/spaces/{sid}/access")
        assert page.status_code == 200 and "Make private" in page.text

        # Flip private from the web; redirects back to the access page.
        r = client.post(f"/mentor/spaces/{sid}/visibility", data={"visibility": "private"}, follow_redirects=False)
        assert r.status_code == 303
        conn = db.connect(db_file)
        assert conn.execute("SELECT visibility FROM spaces WHERE id=?", (sid,)).fetchone()["visibility"] == "private"
        access_page = client.get(f"/mentor/spaces/{sid}/access").text
        assert "Make public" in access_page and "Creator" in access_page

        # Grant then revoke the outsider; the remove-form renders only for real members.
        remove_form = f"/mentor/spaces/{sid}/members/3/delete"
        client.post(f"/mentor/spaces/{sid}/members", data={"user_id": "3"}, follow_redirects=False)
        assert remove_form in client.get(f"/mentor/spaces/{sid}/access").text
        client.post(remove_form, follow_redirects=False)
        assert remove_form not in client.get(f"/mentor/spaces/{sid}/access").text

        # The spaces list shows the lock badge to the creator.
        assert "badge-private" in client.get("/mentor").text


def test_space_access_page_authz(tmp_path):
    db_file = tmp_path / "saz.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility='private' WHERE id=?", (sid,))
        conn.commit()
        url = f"/mentor/spaces/{sid}/access"

        assert client.get(url).status_code == 401  # anonymous
        # Outsider who can't see the private space → 404 (no existence leak).
        _login(client, "o@e.com")
        assert client.get(url).status_code == 404
        assert client.post(f"/mentor/spaces/{sid}/visibility", data={"visibility": "public"}, follow_redirects=False).status_code == 404
        # Admin (not creator) may manage any space → 200.
        _logout(client)
        _login(client, "admin@e.com")
        assert client.get(url).status_code == 200
        # A plain member can see the space but not manage its access → 403.
        from athena.core import access
        access.add_space_member(conn, sid, 3, added_by=2)
        _logout(client)
        _login(client, "o@e.com")
        assert client.get(url).status_code == 403
