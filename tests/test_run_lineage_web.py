"""The browser run-lineage view (/aegis/activity/runs/{run_id}/lineage).

A thin client over activity.run_lineage — the same causal tree the JSON endpoint serves.
These encode that the page renders the ancestor trail (goal→…→run), the focal run's
events, and the spawned-run tree; that an unknown/unseeable run is a 404; and that a run
card links to its lineage. Reads are open, like the rest of the activity views.
"""
from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _three_level(client):
    """g1 (create) → c1 (transition) → gc1 (transition), one issue. Returns its id."""
    iid = client.post(
        "/issues", json={"title": "x"}, headers={**H1, "X-Athena-Run": "g1"}
    ).json()["id"]
    client.patch(
        f"/issues/{iid}",
        json={"status": "in_progress"},
        headers={**H1, "X-Athena-Run": "c1", "X-Athena-Parent-Run": "g1"},
    )
    client.patch(
        f"/issues/{iid}",
        json={"status": "done"},
        headers={**H1, "X-Athena-Run": "gc1", "X-Athena-Parent-Run": "c1"},
    )
    return iid


def test_lineage_page_renders_focal_events_and_descendants(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        iid = _three_level(client)

        page = client.get("/aegis/activity/runs/g1/lineage")
        assert page.status_code == 200
        assert "Run lineage" in page.text
        # The focal run shows its events, linking to the issue.
        assert f"/aegis/issues/{iid}" in page.text
        # Its spawned run (c1) links to its own lineage.
        assert "/aegis/activity/runs/c1/lineage" in page.text
        # The focal events offer copyable headers for starting a child fork.
        assert "Fork headers" in page.text
        assert "X-Athena-Run: g1:fork-" in page.text
        assert "X-Athena-Parent-Run: g1" in page.text
        assert "X-Athena-Fork-From-Event:" in page.text


def test_lineage_page_shows_ancestor_trail(tmp_path):
    with TestClient(create_app(tmp_path / "trail.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        _three_level(client)

        page = client.get("/aegis/activity/runs/gc1/lineage")
        assert page.status_code == 200
        # The trail walks from the originating goal down: links to g1 and c1.
        assert "/aegis/activity/runs/g1/lineage" in page.text
        assert "/aegis/activity/runs/c1/lineage" in page.text


def test_unknown_run_is_404(tmp_path):
    with TestClient(create_app(tmp_path / "missing.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        resp = client.get("/aegis/activity/runs/nope/lineage")
        assert resp.status_code == 404
        assert "No such run" in resp.text


def test_runs_card_links_to_lineage(tmp_path):
    with TestClient(create_app(tmp_path / "link.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        _three_level(client)
        # The orchestrator's run g1 card on the runs page offers a lineage link.
        runs = client.get("/aegis/activity/runs?actor=1")
        assert runs.status_code == 200
        assert "View lineage" in runs.text
        assert "/aegis/activity/runs/g1/lineage" in runs.text
