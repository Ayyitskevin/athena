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


def test_children_list_hides_a_private_child_under_a_public_parent(tmp_path):
    # WHY: parenting has NO same-project rule, so a private-project child can legitimately
    # sit under a public parent. The parent is visible, so /children is 200 — but each
    # child must still be visibility-gated, or the list leaks the hidden child's
    # title/body/key/status to anyone who can see the public parent (and identically
    # through the web detail page and the MCP list_subtasks tool).
    db_file = tmp_path / "child_leak.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        # Public parent in the backlog (visible to everyone, signed in or not).
        v = client.post("/issues", json={"title": "Public parent"}, headers=H_CREATOR).json()["id"]
        # Confidential child in a (soon-to-be) private project.
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        c = client.post(
            "/issues", json={"title": "Acquire FooCorp", "project_id": pp}, headers=H_CREATOR
        ).json()["id"]
        # Nest C under V — allowed: creator owns C, V is public, no cycle.
        assert client.put(
            f"/issues/{c}/parent", json={"parent_id": v}, headers=H_CREATOR
        ).status_code == 200
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        url = f"/issues/{v}/children"
        # The public parent is visible → 200, but the hidden child is filtered out (no leak).
        outsider = client.get(url, headers=H_OUTSIDER)
        assert outsider.status_code == 200 and outsider.json() == []
        assert client.get(url).json() == []  # signed out, same
        # The creator and admin (who can see P) get the child.
        assert [row["id"] for row in client.get(url, headers=H_CREATOR).json()] == [c]
        assert [row["id"] for row in client.get(url, headers=H_ADMIN).json()] == [c]
        # Membership reveals it to the outsider.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert [row["id"] for row in client.get(url, headers=H_OUTSIDER).json()] == [c]
        conn.close()


def test_backlog_issue_subresources_stay_open(tmp_path):
    db_file = tmp_path / "backlog.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        iid = client.post("/issues", json={"title": "Loose"}, headers=H_CREATOR).json()["id"]  # backlog
        # A projectless issue reads like a public one — every sub-resource is open.
        for sub in SUBRESOURCES:
            assert client.get(f"/issues/{iid}/{sub}").status_code == 200
