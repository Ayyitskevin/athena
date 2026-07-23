"""The browser run-timeline view (/aegis/activity/runs).

A thin client over activity.reconstruct_runs — the same lens the JSON /activity/runs
serves. These encode that the page requires picking an actor (a run is one actor's
work), renders an actor's reconstructed runs with their events, badges agents, and
degrades gracefully with no actor or an unknown one. Reads are open, like the rest
of the activity views.
"""

from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _human_and_agent(client):
    client.post("/users", json={"email": "h@e.com", "name": "Human"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "Bot", "is_agent": True}, headers=H1
    )


def test_runs_page_shows_picker_and_hint(tmp_path):
    with TestClient(create_app(tmp_path / "pick.db")) as client:
        _human_and_agent(client)
        client.post(
            "/issues",
            json={"title": "visible agent work"},
            headers={"X-Athena-Actor": "2"},
        )
        page = client.get("/aegis/activity/runs")
        assert page.status_code == 200
        # The picker lists actors represented in visible events, marking agents.
        assert "Choose an actor" in page.text
        assert "Bot (agent)" in page.text
        # With no actor chosen, a hint instead of an empty page.
        assert "Pick an actor" in page.text


def test_runs_page_groups_actor_runs(tmp_path):
    with TestClient(create_app(tmp_path / "group.db")) as client:
        _human_and_agent(client)
        # The agent acts twice back-to-back → one run.
        iss = client.post(
            "/issues", json={"title": "agent work"}, headers={"X-Athena-Actor": "2"}
        ).json()
        client.patch(
            f"/issues/{iss['id']}",
            json={"status": "done"},
            headers={"X-Athena-Actor": "2"},
        )

        page = client.get("/aegis/activity/runs?actor=2")
        assert page.status_code == 200
        assert 'class="run-card"' in page.text
        assert "1 run for" in page.text
        # The run carries its events, linking each to the issue.
        assert f"/aegis/issues/{iss['id']}" in page.text
        # The agent is badged.
        assert "agent-badge" in page.text


def test_runs_page_unknown_actor_is_empty_not_error(tmp_path):
    with TestClient(create_app(tmp_path / "unknown.db")) as client:
        _human_and_agent(client)
        page = client.get("/aegis/activity/runs?actor=999")
        assert page.status_code == 200
        assert "No such actor" in page.text


def test_runs_page_actor_with_no_activity_is_not_selectable(tmp_path):
    with TestClient(create_app(tmp_path / "quiet.db")) as client:
        _human_and_agent(client)
        # The agent (id 2) exists but has done nothing.
        page = client.get("/aegis/activity/runs?actor=2")
        assert page.status_code == 200
        assert "No such actor" in page.text


def test_activity_feed_links_to_runs_view(tmp_path):
    with TestClient(create_app(tmp_path / "link.db")) as client:
        _human_and_agent(client)
        feed = client.get("/aegis/activity")
        assert "/aegis/activity/runs" in feed.text
        # The current actor filter carries into the runs link.
        scoped = client.get("/aegis/activity?actor=2")
        assert "/aegis/activity/runs?actor=2" in scoped.text
