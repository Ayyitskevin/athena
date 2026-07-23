"""The label hub respects visibility (slice 4 — cross-entity surfaces).

The label hub gathers, for one label, every issue AND every page that carries it — a
browsable cross-entity list, so a direct content leak if ungated. A private project's
tagged issues and a private space's tagged pages must drop out for someone who can't
see them, while admins, the creator, and members get the full set. (The index counts
are a separate, lesser follow-up.)
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
    """Label "shared" on a hidden issue, a public issue, a hidden page, a public page.
    Returns (db_conn, priv_project_id, priv_space_id)."""
    lid = client.post(
        "/labels", json={"name": "shared", "color": "#ff0000"}, headers=H_CREATOR
    ).json()["id"]
    pp = client.post(
        "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
    ).json()["id"]
    op = client.post(
        "/projects", json={"name": "Open", "key": "OPN"}, headers=H_CREATOR
    ).json()["id"]
    hi = client.post(
        "/issues", json={"title": "Hidden alpha", "project_id": pp}, headers=H_CREATOR
    ).json()["id"]
    pi = client.post(
        "/issues", json={"title": "Public beta", "project_id": op}, headers=H_CREATOR
    ).json()["id"]
    ps = client.post(
        "/spaces", json={"key": "SSP", "name": "SecretSpace"}, headers=H_CREATOR
    ).json()["id"]
    os_ = client.post(
        "/spaces", json={"key": "OSP", "name": "OpenSpace"}, headers=H_CREATOR
    ).json()["id"]
    hp = client.post(
        f"/spaces/{ps}/pages", json={"title": "Hidden gamma"}, headers=H_CREATOR
    ).json()["id"]
    pp_ = client.post(
        f"/spaces/{os_}/pages", json={"title": "Public delta"}, headers=H_CREATOR
    ).json()["id"]
    for iid in (hi, pi):
        client.post(f"/issues/{iid}/labels", json={"label_id": lid}, headers=H_CREATOR)
    for pid in (hp, pp_):
        client.post(f"/pages/{pid}/labels", json={"label_id": lid}, headers=H_CREATOR)

    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
    conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
    conn.commit()
    return conn, pp, ps


def _login(client, email):
    r = client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_hub_hides_private_issue_and_page_from_anonymous(tmp_path):
    db_file = tmp_path / "hub.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _scenario(client, db_file)

        text = client.get("/aegis/labels/shared").text
        # The hidden issue and hidden page are gone; the public ones remain.
        assert "Hidden alpha" not in text and "Hidden gamma" not in text
        assert "Public beta" in text and "Public delta" in text


def test_hub_shows_everything_to_logged_in_creator(tmp_path):
    db_file = tmp_path / "hub_creator.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _scenario(client, db_file)

        _login(client, "c@e.com")
        text = client.get("/aegis/labels/shared").text
        assert "Hidden alpha" in text and "Hidden gamma" in text
        assert "Public beta" in text and "Public delta" in text


def test_hub_opens_to_a_granted_member(tmp_path):
    db_file = tmp_path / "hub_member.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, pp, ps = _scenario(client, db_file)

        _login(client, "o@e.com")
        assert "Hidden alpha" not in client.get("/aegis/labels/shared").text

        access.add_project_member(conn, pp, 3, added_by=2)
        access.add_space_member(conn, ps, 3, added_by=2)
        text = client.get("/aegis/labels/shared").text
        assert "Hidden alpha" in text and "Hidden gamma" in text
