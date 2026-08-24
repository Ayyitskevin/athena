"""Per-agent PAUSE — the operator lever between "watch" and "revoke everything".

Pausing freezes an account at identity resolution: every authenticated action
(reads, writes, heartbeats, over any transport) is refused with a clear 403,
browser sessions read as signed out, and NOTHING is destroyed — resume restores
the account exactly. These pin: enforcement on the bearer-token and trusted
actor-header paths, the web-session freeze, the audited atomic flip (no event on
a no-op), the last-active-admin guard, admin-only authorization, and the MCP
pause → refused → resume → working loop.
"""

import pytest
from fastapi.testclient import TestClient

from athena.core import activity, agent_commands, db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="pause.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com"):
    return client.post(
        "/users/onboard_agent",
        json={
            "email": email,
            "name": email.split("@")[0],
            "scopes": ["read", "issue:write"],
        },
        headers=H1,
    ).json()


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=50)
    conn.close()
    return rows


def test_pause_freezes_every_token_action_and_resume_restores(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        onboarded = _agent(c)
        agent_id = onboarded["user"]["id"]
        bearer = {"Authorization": f"Bearer {onboarded['token']['token']}"}

        assert c.get("/users/me", headers=bearer).status_code == 200
        paused = c.put(f"/users/{agent_id}/paused", json={"paused": True}, headers=H1)
        assert paused.status_code == 200
        assert paused.json()["paused_at"]

        # Reads AND writes are refused — same clear 403 everywhere.
        for attempt in (
            c.get("/users/me", headers=bearer),
            c.get("/issues", headers=bearer),
            c.post("/issues", json={"title": "nope"}, headers=bearer),
        ):
            assert attempt.status_code == 403
            assert attempt.json()["detail"] == "account is paused"

        resumed = c.put(f"/users/{agent_id}/paused", json={"paused": False}, headers=H1)
        assert resumed.status_code == 200 and resumed.json()["paused_at"] is None
        # Nothing was destroyed: the SAME token works again immediately.
        assert c.get("/users/me", headers=bearer).status_code == 200
        assert (
            c.post("/issues", json={"title": "back"}, headers=bearer).status_code == 201
        )


def test_pause_freezes_the_trusted_header_path_and_web_sessions(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post(
            "/users",
            json={
                "email": "m@e.com",
                "name": "Mem",
                "password": "pw",
                "role": "member",
            },
            headers=H1,
        )
        # A live browser session first...
        c.post(
            "/login",
            data={"email": "m@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert "Sign out" in c.get("/").text
        # ...then the pause lands.
        assert (
            c.put("/users/2/paused", json={"paused": True}, headers=H1).status_code
            == 200
        )
        # The trusted actor-header path is refused,
        denied = c.get("/users/me", headers={"X-Athena-Actor": "2"})
        assert denied.status_code == 403
        assert denied.json()["detail"] == "account is paused"
        # ...and the browser session reads as signed out (session NOT burned).
        assert "Sign out" not in c.get("/").text
        # Resume: the same session cookie works again — nothing was revoked.
        assert (
            c.put("/users/2/paused", json={"paused": False}, headers=H1).status_code
            == 200
        )
        assert "Sign out" in c.get("/").text


def test_pause_is_audited_atomically_and_noop_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent_id = _agent(c)["user"]["id"]
        for _ in range(2):  # second call is a no-op
            c.put(f"/users/{agent_id}/paused", json={"paused": True}, headers=H1)
        c.put(f"/users/{agent_id}/paused", json={"paused": False}, headers=H1)

    paused_events = _events(db_file, "paused_user")
    resumed_events = _events(db_file, "resumed_user")
    assert [e["target_id"] for e in paused_events] == [agent_id]
    assert [e["target_id"] for e in resumed_events] == [agent_id]
    assert paused_events[0]["actor_id"] == 1


def test_cannot_pause_the_last_active_admin(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        denied = c.put("/users/1/paused", json={"paused": True}, headers=H1)
        assert denied.status_code == 409
        assert "last active admin" in denied.json()["detail"]
        # With a second admin present, pausing the first is allowed again.
        c.post(
            "/users",
            json={"email": "boss@e.com", "name": "Boss", "role": "admin"},
            headers=H1,
        )
        assert (
            c.put("/users/1/paused", json={"paused": True}, headers=H1).status_code
            == 200
        )


def test_pause_is_admin_only_below_the_transport(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "role": "member"},
            headers=H1,
        )
        denied = c.put(
            "/users/1/paused",
            json={"paused": True},
            headers={"X-Athena-Actor": "2"},
        )
        assert denied.status_code == 403
    # And the command itself refuses a non-admin actor directly.
    conn = db.connect(db_file)
    member = users.get_user(conn, 2)
    with pytest.raises(agent_commands.AgentCommandError) as excinfo:
        agent_commands.set_user_paused(
            conn, actor=member, target_user_id=1, paused=True
        )
    assert excinfo.value.kind == "forbidden"
    conn.close()


def test_mcp_orchestrator_pauses_and_resumes_a_worker(tmp_path):
    from athena.mcp.client import AthenaClient, AthenaError

    app, _ = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        worker = _agent(tc, "worker@e.com")
        raw_admin = tc.post(
            "/tokens", json={"name": "orch", "scopes": ["admin"]}, headers=H1
        ).json()["token"]

        orch_http = TestClient(app)
        orch_http.headers.update({"Authorization": f"Bearer {raw_admin}"})
        orch = AthenaClient(client=orch_http, run_id="orchestration")

        worker_http = TestClient(app)
        worker_http.headers.update(
            {"Authorization": f"Bearer {worker['token']['token']}"}
        )
        worker_client = AthenaClient(client=worker_http, run_id="worker-run")

        worker_client.create_issue(title="before pause")
        paused = orch.set_user_paused(worker["user"]["id"], True)
        assert paused["paused_at"]

        with pytest.raises(AthenaError) as excinfo:
            worker_client.create_issue(title="while paused")
        assert excinfo.value.status_code == 403
        assert "paused" in str(excinfo.value)

        orch.set_user_paused(worker["user"]["id"], False)
        assert worker_client.create_issue(title="after resume")["id"]
    finally:
        tc.__exit__(None, None, None)
