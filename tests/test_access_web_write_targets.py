"""Web write/target gaps found by the access-control audit (group 2 of the fixes).

The REST write endpoints were gated against hidden TARGETS in an earlier slice, but the
web (HTMX) twins gated only the source: you could create/move an issue INTO a private
project, link/nest under a hidden issue, or keep moving a board card on an issue whose
project went private — all writes into / oracles for containers you can't see. Each now
gates the target the same way, collapsing hidden into the same response as missing. (The
REST set_parent had the same target gap and is fixed too.)
"""
from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _private_project(client, db_file):
    pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
    conn.commit()
    return pp, conn


def test_web_create_issue_into_private_project_blocked(tmp_path):
    db_file = tmp_path / "ci.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp, conn = _private_project(client, db_file)
        _login(client, "o@e.com")
        r = client.post(
            "/aegis/issues",
            data={"title": "sneak", "body": "", "priority": "medium", "project_id": str(pp)},
            follow_redirects=False,
        )
        assert r.status_code == 400  # same "No such project." as a nonexistent id
        assert conn.execute("SELECT COUNT(*) AS n FROM issues WHERE project_id=?", (pp,)).fetchone()["n"] == 0


def test_web_move_issue_into_private_project_blocked(tmp_path):
    db_file = tmp_path / "mp.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp, conn = _private_project(client, db_file)
        biid = client.post("/issues", json={"title": "mine"}, headers=H_OUTSIDER).json()["id"]  # backlog, outsider's
        _login(client, "o@e.com")
        r = client.post(f"/aegis/issues/{biid}/project", data={"project_id": str(pp)}, follow_redirects=False)
        assert r.status_code == 400
        assert conn.execute("SELECT project_id FROM issues WHERE id=?", (biid,)).fetchone()["project_id"] is None


def test_web_link_and_parent_to_hidden_issue_blocked(tmp_path):
    db_file = tmp_path / "lp.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp, conn = _private_project(client, db_file)
        hidden = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        src = client.post("/issues", json={"title": "Mine"}, headers=H_OUTSIDER).json()["id"]  # backlog, outsider's
        _login(client, "o@e.com")
        # Link to the hidden issue → same 400 as a missing target (no oracle, no write).
        r = client.post(f"/aegis/issues/{src}/links", data={"relation": "blocks", "target_ref": str(hidden)}, follow_redirects=False)
        assert r.status_code == 400
        # Nest under the hidden issue → same 400.
        r = client.post(f"/aegis/issues/{src}/parent", data={"parent_ref": str(hidden)}, follow_redirects=False)
        assert r.status_code == 400
        # Neither write landed.
        assert conn.execute("SELECT COUNT(*) AS n FROM issue_links").fetchone()["n"] == 0
        assert conn.execute("SELECT parent_id FROM issues WHERE id=?", (src,)).fetchone()["parent_id"] is None


def test_web_board_move_blocked_when_issue_hidden(tmp_path):
    """The board card-move twin of the detail-page status gate: an issue creator/assignee
    who lost access when the project went private can't keep moving the card."""
    db_file = tmp_path / "bm.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        # Admin owns the project; the outsider creates (and is assigned) an issue in it.
        pp = client.post("/projects", json={"name": "P", "key": "P"}, headers=H_ADMIN).json()["id"]
        iid = client.post("/issues", json={"title": "card", "project_id": pp}, headers=H_OUTSIDER).json()["id"]
        before = client.get(f"/issues/{iid}", headers=H_OUTSIDER).json()["status"]
        # Admin flips the project private without adding the outsider.
        client.put(f"/projects/{pp}/visibility", json={"visibility": "private"}, headers=H_ADMIN)

        _login(client, "o@e.com")
        # The outsider is the issue's creator/assignee, but can no longer see it — the
        # board move snaps back (re-render) and applies no status change, no activity.
        r = client.post(f"/aegis/boards/move/{iid}", data={"new_status": "in_progress"}, follow_redirects=False)
        assert r.status_code in (200, 303)
        conn = db.connect(db_file)
        assert conn.execute("SELECT status FROM issues WHERE id=?", (iid,)).fetchone()["status"] == before


def test_rest_set_parent_to_hidden_issue_blocked(tmp_path):
    db_file = tmp_path / "sp.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp, conn = _private_project(client, db_file)
        hidden = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        child = client.post("/issues", json={"title": "Mine"}, headers=H_OUTSIDER).json()["id"]
        # REST PUT parent to a hidden issue → same 422 as a nonexistent parent.
        r = client.put(f"/issues/{child}/parent", json={"parent_id": hidden}, headers=H_OUTSIDER)
        assert r.status_code == 422
        assert issues.get_issue(conn, child)["parent_id"] is None
