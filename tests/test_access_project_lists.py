"""Project lists respect visibility (access control).

The dedicated project listings — REST GET /projects and the web /aegis/projects page
— must not enumerate a private project to someone who can't see it, while admins, the
creator, and members get the full list. (Project picker dropdowns on forms/filters are
a separate follow-up.)
"""

from fastapi.testclient import TestClient

from athena.core import access, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


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


def _scenario(client, db_file):
    priv = client.post(
        "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
    ).json()["id"]
    client.post("/projects", json={"name": "Open", "key": "OPN"}, headers=H_CREATOR)
    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (priv,))
    conn.commit()
    return conn, priv


def _login(client, email):
    r = client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_rest_project_list_hides_private(tmp_path):
    db_file = tmp_path / "rest.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv = _scenario(client, db_file)

        def keys(headers=None):
            return {
                p["key"] for p in client.get("/projects", headers=headers or {}).json()
            }

        # Outsider + anonymous: only the public project.
        for h in (H_OUTSIDER, None):
            assert keys(h) == {"OPN"}
        # Creator + admin: both.
        for h in (H_CREATOR, H_ADMIN):
            assert keys(h) == {"SEC", "OPN"}

        # Granting membership reveals it to the outsider.
        access.add_project_member(conn, priv, 3, added_by=2)
        assert keys(H_OUTSIDER) == {"SEC", "OPN"}


def test_web_projects_page_hides_private_from_anonymous(tmp_path):
    db_file = tmp_path / "web.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _scenario(client, db_file)

        text = client.get("/aegis/projects").text
        assert "Secret" not in text and "SEC" not in text
        assert "Open" in text


def test_web_projects_page_shows_private_to_logged_in_creator(tmp_path):
    db_file = tmp_path / "web_creator.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _scenario(client, db_file)

        assert "Secret" not in client.get("/aegis/projects").text
        _login(client, "c@e.com")
        assert "Secret" in client.get("/aegis/projects").text
