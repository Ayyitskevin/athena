"""The run-controls exception surface: the attention signal and the admin page.

Run Controls v1 shipped with a recorded limitation: pending controls were
visible only on each run's lineage page, so an operator had to already know
which runs they had steered — exactly the "know six places to look" failure
EXCEPTION_SURFACES.md exists to close. These tests pin the closing move: the
fleet-attention rollup counts live controls with the same predicate the
/admin/run-controls page lists by, the count is standing (never
window-bounded), and the page stays operator intelligence — admin-only.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from athena.aegis import fleet_attention
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="controls_attention.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", name="Sol"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _bind_run(client, bearer, run_id):
    response = client.post(
        "/issues",
        json={"title": "tagged work"},
        headers={**bearer, "X-Athena-Run": run_id},
    )
    assert response.status_code == 201, response.text


def _create(client, run_id, kind="steer", payload="use approach B"):
    response = client.post(
        "/run-controls",
        json={"run_id": run_id, "kind": kind, "payload": payload},
        headers=H1,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_attention_card_counts_live_controls_with_the_pages_own_predicate(
    tmp_path,
):
    # WHY: the rollup's design constraint is that a count may never disagree
    # with the page it links to. "Live" here is the page's own `open` filter —
    # settled controls leave the count immediately, and expiry (a read-time
    # derivation, never a stored state) removes a control without any write.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        _bind_run(client, bearer, "goal-1")
        steer = _create(client, "goal-1", kind="steer")
        _create(client, "goal-1", kind="request_cancel", payload="wind down")

        conn = db.connect(db_file)
        card = fleet_attention.build_attention(conn)
        signal = next(s for s in card["signals"] if s["key"] == "open_run_controls")
        assert signal["count"] == 2
        assert signal["href"] == "/admin/run-controls"
        assert signal["scope"] == "asked, not yet answered"
        conn.close()

        # The agent settles one — the count follows the page's list at once.
        done = client.post(
            f"/run-controls/{steer['id']}/complete",
            json={"summary": "steered as asked"},
            headers=bearer,
        )
        assert done.status_code == 200, done.text
        conn = db.connect(db_file)
        card = fleet_attention.build_attention(conn)
        signal = next(s for s in card["signals"] if s["key"] == "open_run_controls")
        assert signal["count"] == 1

        # Expiry is the server clock's verdict at read time: same rows, a later
        # injected clock, and the standing count goes quiet without any event.
        later = datetime.now(UTC) + timedelta(days=2)
        card = fleet_attention.build_attention(conn, now=later)
        signal = next(s for s in card["signals"] if s["key"] == "open_run_controls")
        assert signal["count"] == 0
        conn.close()


def test_admin_page_lists_controls_and_links_each_run(tmp_path):
    # WHY: the page is the one fleet-wide view of what was asked and answered.
    # Each row must lead back to the lineage page that owns creation and the
    # full settlement detail — this page renders the same command read the
    # API serves, and owns no data.
    app, _ = _app(tmp_path, "controls_page.db")
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        _bind_run(client, _bearer(sol), "goal-7")
        _create(client, "goal-7", payload="prefer the safe path")
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        page = client.get("/admin/run-controls")
        assert page.status_code == 200
        assert "goal-7" in page.text
        assert "/aegis/activity/runs/goal-7/lineage" in page.text
        assert "prefer the safe path" in page.text
        assert "steer" in page.text
        # Truthful wording travels with the data, not just the lineage panel.
        assert "clock ran out" in page.text

        # The state filter narrows with the same closed vocabulary the API
        # uses; a hand-edited value falls back rather than erroring.
        completed = client.get("/admin/run-controls?state=completed")
        assert completed.status_code == 200
        assert "goal-7" not in completed.text
        assert "No completed controls recorded" in completed.text
        assert client.get("/admin/run-controls?state=nonsense").status_code == 200

        everything = client.get("/admin/run-controls?state=all")
        assert "goal-7" in everything.text


def test_admin_page_is_operator_intelligence_only(tmp_path):
    # WHY: fleet-wide control traffic names runs, agents, and operator guidance
    # — the same bar as /admin/security. Members and anonymous readers get
    # refusals, and the dashboard card only renders for admins.
    app, _ = _app(tmp_path, "controls_authz.db")
    with TestClient(app) as client:
        _bootstrap(client)
        client.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "password": "pw"},
            headers=H1,
        )
        assert client.get("/admin/run-controls").status_code == 401
        client.post(
            "/login",
            data={"email": "m@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert client.get("/admin/run-controls").status_code == 403


def test_dashboard_card_names_the_signal_when_controls_wait(tmp_path):
    # WHY: steering by exception means the operator sees "something is asking"
    # from the dashboard without knowing the feature exists.
    app, _ = _app(tmp_path, "controls_dash.db")
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        _bind_run(client, _bearer(sol), "goal-9")
        _create(client, "goal-9")
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        dashboard = client.get("/aegis/dashboard")
        assert dashboard.status_code == 200
        assert "Run controls awaiting an agent" in dashboard.text
        assert "/admin/run-controls" in dashboard.text
