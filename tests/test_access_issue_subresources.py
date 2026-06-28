"""REST issue sub-resources respect visibility (slice 2b follow-up).

The per-issue read endpoints (comments, children, links, contributors, attachments)
each 404 when the parent issue is in a private project the caller can't see — the same
gate the issue detail uses — so a sub-resource never leaks for a hidden issue. Admins,
the creator, and members keep access; the backlog (no project) reads like a public one.
"""
import pytest
from fastapi.testclient import TestClient

from athena.core import access, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}

SUBRESOURCES = ["comments", "children", "links", "contributors", "attachments"]


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


@pytest.mark.parametrize("sub", SUBRESOURCES)
def test_subresource_404s_for_outsider_on_private_issue(tmp_path, sub):
    db_file = tmp_path / f"sub_{sub}.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        iid = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        # Seed a comment so the comments endpoint would return content if it weren't gated.
        client.post(f"/issues/{iid}/comments", json={"body": "secret note"}, headers=H_CREATOR)
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        url = f"/issues/{iid}/{sub}"
        # Hidden from the outsider and the signed-out caller...
        assert client.get(url, headers=H_OUTSIDER).status_code == 404
        assert client.get(url).status_code == 404
        # ...open to the creator and admin.
        assert client.get(url, headers=H_CREATOR).status_code == 200
        assert client.get(url, headers=H_ADMIN).status_code == 200

        # Membership opens it up.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert client.get(url, headers=H_OUTSIDER).status_code == 200


def test_backlog_issue_subresources_stay_open(tmp_path):
    db_file = tmp_path / "backlog.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        iid = client.post("/issues", json={"title": "Loose"}, headers=H_CREATOR).json()["id"]  # backlog
        # A projectless issue reads like a public one — every sub-resource is open.
        for sub in SUBRESOURCES:
            assert client.get(f"/issues/{iid}/{sub}").status_code == 200
