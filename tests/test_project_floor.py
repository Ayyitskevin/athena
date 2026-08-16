"""Project floor: many chairs, one seat each."""

from fastapi.testclient import TestClient

from athena.aegis import office
from athena.main import create_app


def test_floor_lists_empty_and_occupied_chairs(tmp_path):
    app = create_app(tmp_path / "floor.db")
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "admin@e.com", "name": "Admin", "password": "secret"},
        )
        project = client.post(
            "/projects",
            json={"key": "SCR", "name": "Scranton"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        empty = client.post(
            "/issues",
            json={"title": "empty chair", "project_id": project["id"]},
            headers={"X-Athena-Actor": "1"},
        ).json()
        seated = client.post(
            "/issues",
            json={"title": "taken chair", "project_id": project["id"]},
            headers={"X-Athena-Actor": "1"},
        ).json()
        grok = client.post(
            "/users/onboard_agent",
            json={
                "name": "Grok",
                "email": "grok@agents.local",
                "scopes": ["read", "issue:write"],
            },
            headers={"X-Athena-Actor": "1"},
        ).json()
        client.put(
            f"/issues/{seated['id']}/assignee",
            json={"assignee_id": grok["user"]["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        client.post(
            f"/issues/{seated['id']}/delegate",
            json={"user_id": grok["user"]["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        etag = client.get(
            f"/issues/{seated['id']}",
            headers={"Authorization": f"Bearer {grok['token']['token']}"},
        ).headers["etag"]
        claimed = client.post(
            f"/issues/{seated['id']}/claim",
            json={"paths": ["src/athena/aegis/office.py"]},
            headers={
                "Authorization": f"Bearer {grok['token']['token']}",
                "If-Match": etag,
            },
        )
        assert claimed.status_code == 201, claimed.text

        page = client.get(f"/aegis/projects/{project['id']}/floor")
        assert page.status_code == 200, page.text
        assert "Nobody shares a seat" in page.text
        assert "empty chair" in page.text
        assert "taken chair" in page.text

        payload = client.get(f"/projects/{project['id']}/floor").json()
        assert payload["schema"] == office.FLOOR_SCHEMA
        assert payload["chair_count"] == 2
        assert payload["occupied_count"] == 1
        by_title = {c["issue_title"]: c for c in payload["chairs"]}
        assert by_title["empty chair"]["occupied"] is False
        assert by_title["taken chair"]["occupied"] is True
        assert by_title["taken chair"]["occupant"]["seat_slug"] == "grok"
        assert empty["id"]  # used
        missing = client.get("/projects/999/floor")
        assert missing.status_code == 404


def test_open_blockers_by_issue_matches_single_read(tmp_path):
    from athena.aegis import dependencies, issues
    from athena.core import db, users

    conn = db.connect(tmp_path / "blockers.db")
    db.migrate(conn)
    admin = users.create_user(
        conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE
    )
    a = issues.create_issue(conn, title="blocker", body="", created_by=admin["id"])
    b = issues.create_issue(conn, title="blocked", body="", created_by=admin["id"])
    dependencies.add_link(
        conn,
        from_id=a["id"],
        to_id=b["id"],
        relation="blocks",
        created_by=admin["id"],
    )
    single = dependencies.open_blockers(conn, b["id"], actor=admin)
    batched = dependencies.open_blockers_by_issue(conn, [a["id"], b["id"]], actor=admin)
    assert batched[a["id"]] == []
    assert [row["id"] for row in batched[b["id"]]] == [row["id"] for row in single]
    conn.close()


def test_floor_assign_sits_an_empty_chair(tmp_path):
    app = create_app(tmp_path / "floor-sit.db")
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "admin@e.com", "name": "Admin", "password": "secret"},
        )
        project = client.post(
            "/projects",
            json={"key": "SCR", "name": "Scranton"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        issue = client.post(
            "/issues",
            json={"title": "open chair", "project_id": project["id"]},
            headers={"X-Athena-Actor": "1"},
        ).json()
        client.post(
            "/users/onboard_agent",
            json={
                "name": "Grok",
                "email": "grok@agents.local",
                "scopes": ["read", "issue:write"],
            },
            headers={"X-Athena-Actor": "1"},
        )
        client.post(
            "/login",
            data={"email": "admin@e.com", "password": "secret"},
            follow_redirects=False,
        )
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        page = client.get(f"/aegis/projects/{project['id']}/floor")
        assert "Sit them down" in page.text
        posted = client.post(
            f"/aegis/projects/{project['id']}/floor/assign",
            data={"issue_id": issue["id"], "seat_slug": "grok"},
            follow_redirects=False,
        )
        assert posted.status_code == 303, posted.text
        assert "assigned=" in posted.headers["location"]
        assert "seat=grok" in posted.headers["location"]
        fresh = client.get(
            f"/issues/{issue['id']}", headers={"X-Athena-Actor": "1"}
        ).json()
        assert fresh["assignee_name"] == "Grok"


def test_chair_packet_names_project_and_blockers():
    from athena.aegis.office import PROTOCOL

    assert PROTOCOL["claim_one_issue"] is True
