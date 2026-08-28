"""The browser issue time-travel view (/aegis/issues/{ref}/history).

A thin client over issue_history.project_issue_state — the same projection the JSON
GET /issues/{id}/state serves. These encode that the page shows the reconstructed state
beside a clickable checkpoint timeline, that picking a past checkpoint (?as_of=) shows
the state THEN (not now), that the detail page links to it, and that a hidden/missing
issue is a 404. Reads are open, like the issue detail page.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _evolved_issue(client):
    """An issue that moves open→in_progress→done. Returns (id, created_event_id)."""
    iid = client.post("/issues", json={"title": "x"}, headers=H_ADMIN).json()["id"]
    client.patch(f"/issues/{iid}", json={"status": "in_progress"}, headers=H_ADMIN)
    client.patch(f"/issues/{iid}", json={"status": "done"}, headers=H_ADMIN)
    return iid


def test_history_page_shows_current_state_and_checkpoints(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        iid = _evolved_issue(client)

        page = client.get(f"/aegis/issues/{iid}/history")
        assert page.status_code == 200
        assert "Current state" in page.text
        assert "done" in page.text  # the latest status
        # The timeline offers checkpoints to time-travel to (the 'Now' anchor + events).
        assert "Now (latest)" in page.text
        assert f"/aegis/issues/{iid}/history?as_of=" in page.text


def test_history_page_enriches_one_timeline_with_source_actions(tmp_path):
    with TestClient(create_app(tmp_path / "narrative-web.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        created = client.post("/issues", json={"title": "narrated"}, headers=H_ADMIN)
        issue = created.json()
        claim = client.post(
            f"/issues/{issue['id']}/claim",
            headers={
                **H_ADMIN,
                "If-Match": created.headers["ETag"],
                "X-Athena-Run": "narrative-web-run",
            },
        )
        assert claim.status_code == 201, claim.text

        page = client.get(f"/aegis/issues/{issue['id']}/history", headers=H_ADMIN)

        assert page.status_code == 200
        assert page.text.count('<ul class="tt-events">') == 1
        assert "narrative-items" not in page.text
        assert "activity #" in page.text
        assert f"/aegis/issues/{issue['id']}/history?as_of=" in page.text


def test_history_page_time_travels_to_a_past_checkpoint(tmp_path):
    with TestClient(create_app(tmp_path / "past.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        iid = _evolved_issue(client)
        conn = db.connect(tmp_path / "past.db")
        created_id = min(
            e["id"]
            for e in activity.list_activity(conn, target_kind="issue", target_id=iid)
        )

        page = client.get(f"/aegis/issues/{iid}/history?as_of={created_id}")
        assert page.status_code == 200
        # At creation the status was 'open', not the current 'done'.
        assert "State as of" in page.text
        assert "open" in page.text
        assert "Jump to now" in page.text


def test_detail_page_links_to_history(tmp_path):
    with TestClient(create_app(tmp_path / "link.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        iid = _evolved_issue(client)
        detail = client.get(f"/aegis/issues/{iid}")
        assert detail.status_code == 200
        assert f"/aegis/issues/{iid}/history" in detail.text


def test_history_page_404s_a_hidden_issue(tmp_path):
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "Admin", "password": "pw"},
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
        pid = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H_CREATOR
        ).json()["id"]
        iid = client.post(
            "/issues", json={"title": "secret", "project_id": pid}, headers=H_CREATOR
        ).json()["id"]
        client.put(
            f"/projects/{pid}/visibility",
            json={"visibility": "private"},
            headers=H_CREATOR,
        )

        # The web layer's gate keys off request.state.user (the session), so this asserts
        # at least that an anonymous viewer can't see a private issue's history.
        assert client.get(f"/aegis/issues/{iid}/history").status_code == 404
        # And an unknown ref is a 404 too.
        assert client.get("/aegis/issues/99999/history").status_code == 404
