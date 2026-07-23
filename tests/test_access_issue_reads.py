"""Issue reads respect project visibility (slice 2 of access control).

A private project's issues must vanish from every issue read for someone who can't
see the project — the list and the detail, REST and web, signed in or not — while
admins, the creator, and members keep full sight, and the backlog (issues with no
project) stays visible to everyone. Nothing marks a project private through the app
yet (that's a later slice), so these flip the `visibility` column on a side
connection to the same DB and exercise the gate.
"""

from fastapi.testclient import TestClient

from athena.core import access, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}  # the bootstrap admin (sees everything)
H_CREATOR = {"X-Athena-Actor": "2"}  # member; owns the private project
H_OUTSIDER = {"X-Athena-Actor": "3"}  # member; not a member of the project


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


def _make_private(db_file, project_id):
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE projects SET visibility = 'private' WHERE id = ?", (project_id,)
    )
    conn.commit()
    return conn


def _scenario(client, db_file):
    """A private project (with one issue), a public project (with one issue), and a
    backlog issue. Returns the three issue ids; the private project is flipped private.
    Returns (private_iid, public_iid, backlog_iid, private_pid, side_conn)."""
    priv_pid = client.post(
        "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
    ).json()["id"]
    pub_pid = client.post(
        "/projects", json={"name": "Open", "key": "OPN"}, headers=H_CREATOR
    ).json()["id"]
    priv = client.post(
        "/issues",
        json={"title": "Hidden bug", "project_id": priv_pid},
        headers=H_CREATOR,
    ).json()["id"]
    pub = client.post(
        "/issues", json={"title": "Open bug", "project_id": pub_pid}, headers=H_CREATOR
    ).json()["id"]
    back = client.post(
        "/issues", json={"title": "Loose bug"}, headers=H_CREATOR
    ).json()["id"]
    conn = _make_private(db_file, priv_pid)
    return priv, pub, back, priv_pid, conn


def _login(client, email):
    r = client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- REST: GET /issues -------------------------------------------------------


def test_rest_list_hides_private_from_outsider_and_anonymous(tmp_path):
    db_file = tmp_path / "rest_list.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        priv, pub, back, _pid, _conn = _scenario(client, db_file)

        def titles(headers=None):
            return {
                i["title"] for i in client.get("/issues", headers=headers or {}).json()
            }

        # The outsider and the signed-out caller see the public + backlog issues, never
        # the private one.
        for h in (H_OUTSIDER, None):
            t = titles(h)
            assert "Open bug" in t and "Loose bug" in t
            assert "Hidden bug" not in t

        # The creator and the admin see all three.
        for h in (H_CREATOR, H_ADMIN):
            assert "Hidden bug" in titles(h)


def test_rest_list_opens_to_a_granted_member(tmp_path):
    db_file = tmp_path / "rest_member.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        priv, pub, back, pid, conn = _scenario(client, db_file)
        assert "Hidden bug" not in {
            i["title"] for i in client.get("/issues", headers=H_OUTSIDER).json()
        }

        access.add_project_member(conn, pid, 3, added_by=2)
        assert "Hidden bug" in {
            i["title"] for i in client.get("/issues", headers=H_OUTSIDER).json()
        }


# --- REST: GET /issues/{ref} -------------------------------------------------


def test_rest_detail_404s_private_for_outsider(tmp_path):
    db_file = tmp_path / "rest_detail.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        priv, pub, back, pid, conn = _scenario(client, db_file)

        # Hidden issue: 404 (indistinguishable from missing) for outsider + anonymous.
        assert client.get(f"/issues/{priv}", headers=H_OUTSIDER).status_code == 404
        assert client.get(f"/issues/{priv}").status_code == 404
        # ...but visible to the creator and admin, and the backlog issue is open to all.
        assert client.get(f"/issues/{priv}", headers=H_CREATOR).status_code == 200
        assert client.get(f"/issues/{priv}", headers=H_ADMIN).status_code == 200
        assert client.get(f"/issues/{back}").status_code == 200

        # Granting membership turns the 404 into a 200.
        access.add_project_member(conn, pid, 3, added_by=2)
        assert client.get(f"/issues/{priv}", headers=H_OUTSIDER).status_code == 200


# --- Web: anonymous viewer ---------------------------------------------------


def test_web_surfaces_hide_private_from_anonymous(tmp_path):
    db_file = tmp_path / "web_anon.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        priv, pub, back, pid, conn = _scenario(client, db_file)

        # The list, the board, and the Aegis landing never name the private issue...
        assert "Hidden bug" not in client.get("/aegis/issues").text
        assert "Hidden bug" not in client.get("/aegis/boards").text
        assert "Hidden bug" not in client.get("/aegis").text
        # ...but do show the public one.
        assert "Open bug" in client.get("/aegis/issues").text
        # The detail page is a 404 for the hidden issue, a 200 for the public one.
        assert client.get(f"/aegis/issues/{priv}").status_code == 404
        assert client.get(f"/aegis/issues/{pub}").status_code == 200


def test_web_detail_visible_to_logged_in_creator(tmp_path):
    db_file = tmp_path / "web_creator.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        priv, pub, back, pid, conn = _scenario(client, db_file)

        # Signed out, the creator's own private issue is a 404; signed in, it renders.
        assert client.get(f"/aegis/issues/{priv}").status_code == 404
        _login(client, "c@e.com")
        page = client.get(f"/aegis/issues/{priv}")
        assert page.status_code == 200 and "Hidden bug" in page.text
        assert "Hidden bug" in client.get("/aegis/issues").text
