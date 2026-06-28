"""Project dropdowns and label index counts respect visibility (slice 4 follow-ups).

The project-picker dropdowns on the issue list/forms must not enumerate a private
project's name to someone who can't see it, and the label index's per-label counts must
tally only the issues/pages the viewer may see (a count never reveals how many hidden
items carry a label). Labels themselves are shared vocabulary, so a label still appears
even when all its usages are hidden — just at zero.
"""
from fastapi.testclient import TestClient

from athena.core import access, db, labels, users
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


def test_issue_list_project_dropdown_hides_private_project(tmp_path):
    db_file = tmp_path / "dd.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        client.post("/projects", json={"name": "Secretproj", "key": "SEC"}, headers=H_CREATOR)
        client.post("/projects", json={"name": "Openproj", "key": "OPN"}, headers=H_CREATOR)
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE key = 'SEC'")
        conn.commit()

        # Anonymous: the private project's name is absent from the filter dropdown;
        # the public one is present.
        anon = client.get("/aegis/issues").text
        assert "Secretproj" not in anon and "Openproj" in anon
        # The creator, signed in, sees their private project in the dropdown.
        _login(client, "c@e.com")
        assert "Secretproj" in client.get("/aegis/issues").text


def test_label_usage_counts_respect_visibility(tmp_path):
    db_file = tmp_path / "lu.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        lid = client.post("/labels", json={"name": "x", "color": "#ff0000"}, headers=H_CREATOR).json()["id"]
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        hi = client.post("/issues", json={"title": "h", "project_id": pp}, headers=H_CREATOR).json()["id"]
        pi = client.post("/issues", json={"title": "p"}, headers=H_CREATOR).json()["id"]
        ps = client.post("/spaces", json={"key": "SSP", "name": "S"}, headers=H_CREATOR).json()["id"]
        os_ = client.post("/spaces", json={"key": "OSP", "name": "O"}, headers=H_CREATOR).json()["id"]
        hp = client.post(f"/spaces/{ps}/pages", json={"title": "hp"}, headers=H_CREATOR).json()["id"]
        op = client.post(f"/spaces/{os_}/pages", json={"title": "op"}, headers=H_CREATOR).json()["id"]
        for iid in (hi, pi):
            client.post(f"/issues/{iid}/labels", json={"label_id": lid}, headers=H_CREATOR)
        for pid in (hp, op):
            client.post(f"/pages/{pid}/labels", json={"label_id": lid}, headers=H_CREATOR)
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
        conn.commit()

        def usage_for(user_id):
            actor = users.get_user(conn, user_id) if user_id else None
            rows = labels.label_usage(
                conn,
                visible_project_ids=access.visible_project_filter(conn, actor),
                visible_space_ids=access.visible_space_filter(conn, actor),
            )
            return next(r for r in rows if r["name"] == "x")

        # Admin (user 1) tallies everything; the outsider only the public issue + page.
        admin = usage_for(1)
        assert admin["issue_count"] == 2 and admin["page_count"] == 2
        out = usage_for(3)
        assert out["issue_count"] == 1 and out["page_count"] == 1

        # Membership brings the hidden tallies back.
        access.add_project_member(conn, pp, 3, added_by=2)
        access.add_space_member(conn, ps, 3, added_by=2)
        revealed = usage_for(3)
        assert revealed["issue_count"] == 2 and revealed["page_count"] == 2
