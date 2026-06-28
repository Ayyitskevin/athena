"""Mentor reads respect space visibility (slice 3 of access control).

A private space's pages and the space itself must vanish from every mentor read for
someone who can't see the space — the space list, the space detail, the page list, and
the page detail, REST and web — while admins, the creator, and members keep full sight.
Pages always live in a space (no backlog), so the space gate covers the pages too.
Nothing marks a space private through the app yet; these flip the visibility column on
a side connection.
"""
from fastapi.testclient import TestClient

from athena.core import access, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


def _scenario(client, db_file):
    """Private space SEC (with a page), public space ENG (with a page). Returns
    (db_conn, priv_space_id, priv_page_id, pub_page_id)."""
    priv = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
    pub = client.post("/spaces", json={"key": "ENG", "name": "Engineering"}, headers=H_CREATOR).json()["id"]
    priv_page = client.post(f"/spaces/{priv}/pages", json={"title": "Hidden runbook"}, headers=H_CREATOR).json()["id"]
    pub_page = client.post(f"/spaces/{pub}/pages", json={"title": "Public doc"}, headers=H_CREATOR).json()["id"]
    conn = db.connect(db_file)
    conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (priv,))
    conn.commit()
    return conn, priv, priv_page, pub_page


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- REST -------------------------------------------------------------------

def test_rest_space_list_hides_private(tmp_path):
    db_file = tmp_path / "list.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, _pp, _pub = _scenario(client, db_file)

        def keys(headers=None):
            return {s["key"] for s in client.get("/spaces", headers=headers or {}).json()}

        for h in (H_OUTSIDER, None):
            assert keys(h) == {"ENG"}
        for h in (H_CREATOR, H_ADMIN):
            assert keys(h) == {"SEC", "ENG"}

        access.add_space_member(conn, priv, 3, added_by=2)
        assert keys(H_OUTSIDER) == {"SEC", "ENG"}


def test_rest_space_and_pages_404_for_outsider(tmp_path):
    db_file = tmp_path / "detail.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, priv_page, pub_page = _scenario(client, db_file)

        # The private space, its page list, and its page are all 404 for the outsider
        # and anonymous; the creator and admin get 200.
        assert client.get(f"/spaces/{priv}", headers=H_OUTSIDER).status_code == 404
        assert client.get(f"/spaces/{priv}/pages", headers=H_OUTSIDER).status_code == 404
        assert client.get(f"/pages/{priv_page}", headers=H_OUTSIDER).status_code == 404
        assert client.get(f"/pages/{priv_page}").status_code == 404
        for h in (H_CREATOR, H_ADMIN):
            assert client.get(f"/spaces/{priv}", headers=h).status_code == 200
            assert client.get(f"/pages/{priv_page}", headers=h).status_code == 200
        # The public page is open to everyone.
        assert client.get(f"/pages/{pub_page}").status_code == 200

        # Membership opens it up.
        access.add_space_member(conn, priv, 3, added_by=2)
        assert client.get(f"/pages/{priv_page}", headers=H_OUTSIDER).status_code == 200


# --- Web --------------------------------------------------------------------

def test_web_mentor_surfaces_hide_private_from_anonymous(tmp_path):
    db_file = tmp_path / "web.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, priv_page, pub_page = _scenario(client, db_file)

        # The space index never names the private space; it does name the public one.
        index = client.get("/mentor").text
        assert "Secret" not in index and "SEC" not in index
        assert "Engineering" in index
        # The private space detail and its page are 404; the public page renders.
        assert client.get(f"/mentor/spaces/{priv}").status_code == 404
        assert client.get(f"/mentor/pages/{priv_page}").status_code == 404
        assert "Hidden runbook" not in client.get(f"/mentor/pages/{priv_page}").text
        assert client.get(f"/mentor/pages/{pub_page}").status_code == 200


def test_web_page_visible_to_logged_in_creator(tmp_path):
    db_file = tmp_path / "web_creator.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, priv_page, pub_page = _scenario(client, db_file)

        assert client.get(f"/mentor/pages/{priv_page}").status_code == 404
        _login(client, "c@e.com")
        page = client.get(f"/mentor/pages/{priv_page}")
        assert page.status_code == 200 and "Hidden runbook" in page.text
        assert "Secret" in client.get("/mentor").text
