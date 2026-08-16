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


def test_chair_packet_names_project_and_blockers():
    from athena.aegis.office import PROTOCOL

    assert PROTOCOL["claim_one_issue"] is True
