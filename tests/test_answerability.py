"""Tests for the answerability ledger: asks and answers per agent, never a score.

The contract (docs/ANSWERABILITY.md, adapted from Buzz's web-of-trust idea and
deliberately narrowed): every number is derived at read time from the table the
owning surface lists by, agents with nothing recorded appear zero-filled, a
worker that acknowledges a kill while still reporting running stays UNCONFIRMED,
expiry is the clock's verdict, and nothing here computes a reputation scalar —
Athena records; the operator judges.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from athena.core import answerability, approvals, db
from athena.main import create_app
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="answerability.db"):
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
    return response.json()


def _control(client, run_id, kind="steer", payload="guidance"):
    response = client.post(
        "/run-controls",
        json={"run_id": run_id, "kind": kind, "payload": payload},
        headers=H1,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _row(ledger, agent_id):
    return next(r for r in ledger["agents"] if r["agent_id"] == agent_id)


def test_ledger_zero_fills_agents_and_lists_no_humans(tmp_path):
    # WHY: "no asks yet" is an explicit statement, not a missing row — and the
    # ledger is about AGENT accounts; the human operator never appears in it.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
    conn = db.connect(db_file)
    ledger = answerability.build_answerability(conn)
    conn.close()
    assert ledger["schema"] == answerability.SCHEMA
    assert [r["agent_id"] for r in ledger["agents"]] == [sol["user"]["id"]]
    row = ledger["agents"][0]
    assert row["run_controls"] == {
        "open": 0,
        "expired_unanswered": 0,
        "completed": 0,
        "declined": 0,
    }
    assert row["worker_kills"] == {"told_to_stop": 0, "confirmed": 0}
    assert row["approvals"] == {"pending": 0, "approved": 0, "rejected": 0}
    assert row["reversed_events"] == 0
    assert row["paused"] is False


def test_controls_lane_uses_the_derived_state_including_expiry(tmp_path):
    # WHY: the lane must agree with /admin/run-controls exactly — settled rows
    # answer, and an unsettled row flips from open to expired-unanswered purely
    # by the clock, with no write and no event.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        _bind_run(client, bearer, "goal-1")
        done = _control(client, "goal-1", kind="steer")
        declined = _control(client, "goal-1", kind="request_cancel")
        _control(client, "goal-1", kind="request_fresh_context")
        assert (
            client.post(
                f"/run-controls/{done['id']}/complete",
                json={"summary": "steered"},
                headers=bearer,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/run-controls/{declined['id']}/decline",
                json={"reason": "mid-transaction"},
                headers=bearer,
            ).status_code
            == 200
        )
    conn = db.connect(db_file)
    lane = _row(answerability.build_answerability(conn), sol["user"]["id"])[
        "run_controls"
    ]
    assert lane == {
        "open": 1,
        "expired_unanswered": 0,
        "completed": 1,
        "declined": 1,
    }
    later = datetime.now(UTC) + timedelta(days=2)
    lane = _row(answerability.build_answerability(conn, now=later), sol["user"]["id"])[
        "run_controls"
    ]
    assert lane == {
        "open": 0,
        "expired_unanswered": 1,
        "completed": 1,
        "declined": 1,
    }
    conn.close()


def test_kill_lane_counts_asks_and_worker_confirmations(tmp_path):
    # WHY: "told to stop" is the operator's live question; it only moves to
    # "confirmed" when the WORKER answers. The lane reuses the workers page's
    # own derived kill_state, so the two can never disagree.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        beat = client.put(
            "/workers/heartbeat", json={"worker_key": "w-1"}, headers=bearer
        )
        assert beat.status_code == 200, beat.text
        worker_id = beat.json()["id"]
        assert client.post(f"/workers/{worker_id}/kill", headers=H1).status_code == 200
        conn = db.connect(db_file)
        lane = _row(answerability.build_answerability(conn), sol["user"]["id"])[
            "worker_kills"
        ]
        assert lane == {"told_to_stop": 1, "confirmed": 0}
        conn.close()

        for state in ("stopping", "stopped"):
            confirm = client.put(
                "/workers/heartbeat",
                json={"worker_key": "w-1", "state": state},
                headers=bearer,
            )
            assert confirm.status_code == 200, confirm.text
    conn = db.connect(db_file)
    lane = _row(answerability.build_answerability(conn), sol["user"]["id"])[
        "worker_kills"
    ]
    assert lane == {"told_to_stop": 0, "confirmed": 1}
    conn.close()


def test_approvals_and_reversals_lanes_count_recorded_corrections(tmp_path):
    # WHY: approvals are what the agent ASKED the operator; reversals are what
    # the operator took back. Both are recorded facts with owners — never
    # inferred, never a judgment.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        bearer = _bearer(sol)
        issue = client.post(
            "/issues", json={"title": "to archive"}, headers=bearer
        ).json()
        assert (
            client.post(f"/issues/{issue['id']}/archive", headers=bearer).status_code
            == 200
        )
        archived = client.get(
            "/activity", params={"verb": "archived"}, headers=H1
        ).json()[0]
        assert (
            client.post(f"/activity/{archived['id']}/undo", headers=H1).status_code
            == 201
        )

    conn = db.connect(db_file)
    agent_id = sol["user"]["id"]
    pending = approvals.open_request(
        conn,
        actor_id=agent_id,
        action_kind="issue.close",
        target_kind="issue",
        target_id=1,
        run_id=None,
    )
    approvals.decide(conn, actor_id=1, request_id=pending.id, decision="approve")
    approvals.open_request(
        conn,
        actor_id=agent_id,
        action_kind="dispatch.request",
        target_kind="issue",
        target_id=1,
        run_id=None,
    )
    row = _row(answerability.build_answerability(conn), agent_id)
    assert row["approvals"] == {"pending": 1, "approved": 1, "rejected": 0}
    assert row["reversed_events"] == 1
    conn.close()


def test_rest_surface_is_admin_only_and_filters(tmp_path):
    # WHY: the ledger names every agent and how often the operator corrected
    # each — fleet intelligence, the /security bar. The filter narrows; an
    # unknown agent is a 404, not an empty 200 that reads as "no asks".
    app, _ = _app(tmp_path, "rest.db")
    with TestClient(app) as client:
        _bootstrap(client)
        sol = _agent(client)
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "password": "pw"},
            headers=H1,
        ).json()
        assert client.get("/fleet/answerability").status_code == 401
        assert (
            client.get(
                "/fleet/answerability",
                headers={"X-Athena-Actor": str(member["id"])},
            ).status_code
            == 403
        )
        everyone = client.get("/fleet/answerability", headers=H1)
        assert everyone.status_code == 200
        assert everyone.json()["schema"] == answerability.SCHEMA
        one = client.get(
            "/fleet/answerability",
            params={"agent_id": sol["user"]["id"]},
            headers=H1,
        ).json()
        assert [r["agent_id"] for r in one["agents"]] == [sol["user"]["id"]]
        assert (
            client.get(
                "/fleet/answerability", params={"agent_id": 9999}, headers=H1
            ).status_code
            == 404
        )
        # A HUMAN user id is not an agent: same 404, no oracle about roles.
        assert (
            client.get(
                "/fleet/answerability",
                params={"agent_id": member["id"]},
                headers=H1,
            ).status_code
            == 404
        )


def test_mcp_client_reads_the_ledger(tmp_path):
    # WHY: steering rule 1 — no capability without an MCP path.
    app, _ = _app(tmp_path, "mcp.db")
    with TestClient(app) as tc:
        tc.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        sol = _agent(tc)
        raw = tc.post(
            "/tokens",
            json={"name": "mcp", "scopes": ["admin"]},
            headers=H1,
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        api = AthenaClient(client=tc)
        ledger = api.agent_answerability()
        assert ledger["schema"] == answerability.SCHEMA
        one = api.agent_answerability(agent_id=sol["user"]["id"])
        assert len(one["agents"]) == 1


def test_agents_page_renders_the_ledger(tmp_path):
    # WHY: the agents page is the cockpit; the ledger renders there with its
    # honesty language attached, not just numbers.
    app, _ = _app(tmp_path, "web.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _agent(client)
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        page = client.get("/admin/agents")
        assert page.status_code == 200
        assert "Answerability" in page.text
        assert "never a score" in page.text
        assert "expired unanswered" in page.text
