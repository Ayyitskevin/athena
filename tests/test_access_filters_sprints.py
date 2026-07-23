"""Saved-filter runs and sprint reads respect visibility (slice 2b follow-up).

A saved filter that matches a private project's issue must not surface it to a runner
who can't see that project, and a private project's sprints must 404 for outsiders —
while admins, the creator, and members keep full sight.
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


def _private_project(client, db_file):
    pp = client.post(
        "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
    ).json()["id"]
    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
    conn.commit()
    return conn, pp


def test_saved_filter_run_excludes_hidden_issues(tmp_path):
    db_file = tmp_path / "filt.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, pp = _private_project(client, db_file)
        client.post(
            "/issues", json={"title": "Hidden bug", "project_id": pp}, headers=H_CREATOR
        )
        client.post(
            "/issues", json={"title": "Public bug"}, headers=H_CREATOR
        )  # backlog
        # The outsider owns a match-everything filter.
        fid = client.post("/filters", json={"name": "all"}, headers=H_OUTSIDER).json()[
            "id"
        ]

        def titles():
            return {
                i["title"]
                for i in client.get(f"/filters/{fid}/issues", headers=H_OUTSIDER).json()
            }

        # Running it never surfaces the private project's issue...
        assert "Public bug" in titles() and "Hidden bug" not in titles()
        # ...until the outsider is granted membership.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert "Hidden bug" in titles()


def test_sprint_reads_404_for_outsider(tmp_path):
    db_file = tmp_path / "spr.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, pp = _private_project(client, db_file)
        sid = client.post(
            f"/projects/{pp}/sprints", json={"name": "S1"}, headers=H_CREATOR
        ).json()["id"]

        # The project's sprint list and the sprint itself are 404 for outsider + anon.
        assert (
            client.get(f"/projects/{pp}/sprints", headers=H_OUTSIDER).status_code == 404
        )
        assert client.get(f"/sprints/{sid}", headers=H_OUTSIDER).status_code == 404
        assert client.get(f"/sprints/{sid}").status_code == 404
        # ...but open to the creator and admin.
        for h in (H_CREATOR, H_ADMIN):
            assert client.get(f"/projects/{pp}/sprints", headers=h).status_code == 200
            assert client.get(f"/sprints/{sid}", headers=h).status_code == 200

        # Membership opens them up.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert (
            client.get(f"/projects/{pp}/sprints", headers=H_OUTSIDER).status_code == 200
        )
        assert client.get(f"/sprints/{sid}", headers=H_OUTSIDER).status_code == 200


def test_issue_sprint_filter_does_not_become_a_private_sprint_oracle(tmp_path):
    # WHY: MCP now exposes the existing issue sprint filter. Filtering by a hidden
    # sprint must look exactly like filtering by an unknown id to an outsider.
    db_file = tmp_path / "sprint_filter.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _, pp = _private_project(client, db_file)
        sid = client.post(
            f"/projects/{pp}/sprints",
            json={"name": "Secret Sprint"},
            headers=H_CREATOR,
        ).json()["id"]
        issue = client.post(
            "/issues",
            json={"title": "Hidden sprint issue", "project_id": pp},
            headers=H_CREATOR,
        ).json()
        client.put(
            f"/issues/{issue['id']}/sprint",
            json={"sprint_id": sid},
            headers=H_CREATOR,
        )

        outsider = client.get("/issues", params={"sprint": sid}, headers=H_OUTSIDER)
        unknown = client.get("/issues", params={"sprint": 999999}, headers=H_OUTSIDER)
        anonymous = client.get("/issues", params={"sprint": sid})
        assert (
            outsider.status_code == unknown.status_code == anonymous.status_code == 200
        )
        assert outsider.json() == unknown.json() == anonymous.json() == []
        assert [
            row["id"]
            for row in client.get(
                "/issues", params={"sprint": sid}, headers=H_CREATOR
            ).json()
        ] == [issue["id"]]


def test_web_sprint_page_404s_for_anonymous(tmp_path):
    db_file = tmp_path / "spr_web.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, pp = _private_project(client, db_file)
        client.post(f"/projects/{pp}/sprints", json={"name": "S1"}, headers=H_CREATOR)
        assert client.get(f"/aegis/projects/{pp}/sprints").status_code == 404
