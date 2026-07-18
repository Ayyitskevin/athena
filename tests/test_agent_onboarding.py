"""Onboarding an agent is ONE audited move: user + first scoped token together.

Before agent_commands.onboard_agent, bringing an agent into the workspace took
three manual steps (create the user, mark it an agent, mint a token) and the mint
had to impersonate the new agent — recording a 'minted_token' event that claimed
the agent minted its own credential before it ever ran. These tests pin the fix:
one admin-gated atomic command; a truthful trail (created_user, minted_token, and
the summarizing onboarded_agent — ALL attributed to the acting admin); the raw
secret never logged; scopes required with nothing half-created on refusal; and a
response that carries a working token plus the ready-to-paste MCP config block.
"""
from fastapi.testclient import TestClient

from athena.core import activity, agent_commands, db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="onboard.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"})


def _onboard(client, headers=H1, **overrides):
    payload = {
        "email": "sol@e.com",
        "name": "Sol",
        "scopes": ["read", "issue:write"],
        **overrides,
    }
    return client.post("/users/onboard_agent", json=payload, headers=headers)


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=100)
    conn.close()
    return rows


def test_onboard_returns_working_scoped_token_and_mcp_config(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = _onboard(c, token_name="sol-mcp")
        assert r.status_code == 201, r.text
        body = r.json()

        assert body["user"]["email"] == "sol@e.com"
        assert body["user"]["role"] == "member"
        assert body["user"]["is_agent"] is True
        assert body["token"]["name"] == "sol-mcp"
        assert body["token"]["scopes"] == ["read", "issue:write"]

        raw = body["token"]["token"]
        env = body["mcp_config"]["mcpServers"]["athena"]["env"]
        assert env["ATHENA_TOKEN"] == raw
        assert env["ATHENA_BASE_URL"] == "http://testserver"

        # The credential WORKS, exactly as scoped: the agent knows itself, can
        # write issues, and is refused the scope it wasn't granted.
        bearer = {"Authorization": f"Bearer {raw}"}
        me = c.get("/users/me", headers=bearer).json()
        assert me["id"] == body["user"]["id"] and me["is_agent"] is True
        assert me["scopes"] == ["read", "issue:write"]
        assert c.post("/issues", json={"title": "sol's"}, headers=bearer).status_code == 201
        denied = c.post("/spaces", json={"key": "S", "name": "S"}, headers=bearer)
        assert denied.status_code == 403


def test_onboarding_is_fully_audited_and_attributed_to_the_admin(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        body = _onboard(c).json()
    raw = body["token"]["token"]

    created = _events(db_file, "created_user")
    minted = _events(db_file, "minted_token")
    onboarded = _events(db_file, "onboarded_agent")
    assert [e["target_id"] for e in created] == [body["user"]["id"], 1]
    assert [e["target_id"] for e in minted] == [body["token"]["id"]]
    assert [e["target_id"] for e in onboarded] == [body["user"]["id"]]
    # Every event names the ADMIN as actor — the agent did nothing yet.
    assert created[0]["actor_id"] == 1
    assert minted[0]["actor_id"] == 1
    assert onboarded[0]["actor_id"] == 1
    # The trail records the credential's POWER, never its secret.
    assert "read issue:write" in onboarded[0]["detail"]
    assert all(raw not in (e["detail"] or "") for e in _events(db_file))


def test_onboard_is_admin_only(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "role": "member"},
            headers=H1,
        )
        r = _onboard(c, headers={"X-Athena-Actor": "2"})
        assert r.status_code == 403
    conn = db.connect(db_file)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email = 'sol@e.com'"
    ).fetchone()["n"] == 0
    conn.close()


def test_admin_bearer_token_needs_the_admin_scope(tmp_path):
    # An admin holding a READ-scoped token must not be able to onboard through
    # it — every transport gates on admin role AND admin token scope
    # (require_admin / _admin_required) before the command runs.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        raw = c.post(
            "/tokens", json={"name": "weak", "scopes": ["read"]}, headers=H1
        ).json()["token"]
        r = _onboard(c, headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 403
        assert "token scope required: admin" in r.json()["detail"]
    conn = db.connect(db_file)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email = 'sol@e.com'"
    ).fetchone()["n"] == 0
    conn.close()


def test_scopes_are_required_and_refusal_creates_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        # REST: scopes is a required field — omitting it never reaches the mint.
        r = c.post(
            "/users/onboard_agent",
            json={"email": "sol@e.com", "name": "Sol"},
            headers=H1,
        )
        assert r.status_code == 422

    # Command level: an empty scope list refuses BEFORE any write.
    conn = db.connect(db_file)
    admin = users.get_user(conn, 1)
    try:
        agent_commands.onboard_agent(
            conn, actor=admin, email="sol@e.com", name="Sol", scopes=[]
        )
        raise AssertionError("expected AgentCommandError")
    except agent_commands.AgentCommandError as exc:
        assert exc.status_code == 422
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email = 'sol@e.com'"
    ).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"] == 0
    conn.close()
    assert _events(db_file, "onboarded_agent") == []


def test_idempotency_key_is_refused_so_the_secret_never_enters_the_replay_store(
    tmp_path,
):
    # The durable-idempotency layer stores and replays response bodies. This
    # response carries a one-time secret, so — like POST /tokens — it must
    # refuse the Idempotency-Key contract outright rather than persist the raw
    # token in the replay store.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post(
            "/users/onboard_agent",
            json={"email": "sol@e.com", "name": "Sol", "scopes": ["read"]},
            headers={**H1, "Idempotency-Key": "onboard-1"},
        )
        assert r.status_code == 400
        assert "one-time secret" in r.json()["detail"]
    conn = db.connect(db_file)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email = 'sol@e.com'"
    ).fetchone()["n"] == 0
    conn.close()


def test_blank_email_or_name_is_422_on_every_surface(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        for payload in (
            {"email": "   ", "name": "Sol"},
            {"email": "sol@e.com", "name": ""},
        ):
            r = _onboard(c, **payload)
            assert r.status_code == 422
            assert "email and name are required" in r.json()["detail"]
    conn = db.connect(db_file)
    assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1
    conn.close()


def test_duplicate_email_is_409_and_leaves_no_partial_state(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        assert _onboard(c).status_code == 201
        again = _onboard(c)
        assert again.status_code == 409
        assert "email already in use" in again.json()["detail"]
    conn = db.connect(db_file)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email = 'sol@e.com'"
    ).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"] == 1
    conn.close()
    assert len(_events(db_file, "onboarded_agent")) == 1


def test_web_cockpit_onboarding_shows_token_and_config_once(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post(
            "/login",
            data={"email": "ann@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        r = c.post(
            "/admin/agents/onboard",
            data={
                "email": "sol@e.com",
                "name": "Sol",
                "scope_read": "1",
                "scope_issue_write": "1",
            },
        )
        assert r.status_code == 201, r.text
        assert "ath_" in r.text  # the one-time secret is on THIS response...
        assert "mcpServers" in r.text  # ...with the paste-ready MCP config
        # ...and only this response: the page re-fetched shows no secret.
        again = c.get("/admin/agents")
        assert "mcpServers" not in again.text
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT role, is_agent FROM users WHERE email = 'sol@e.com'"
    ).fetchone()
    conn.close()
    assert row is not None and row["role"] == "member" and row["is_agent"] == 1


def test_web_cockpit_onboarding_requires_scopes_and_admin(tmp_path):
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
        # Admin with no scope boxes checked: a clear 400, nothing minted.
        c.post(
            "/login",
            data={"email": "ann@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        r = c.post(
            "/admin/agents/onboard", data={"email": "sol@e.com", "name": "Sol"}
        )
        assert r.status_code == 400
        assert "at least one token scope" in r.text
        # A member session is refused outright.
        c.post(
            "/login",
            data={"email": "m@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        denied = c.post(
            "/admin/agents/onboard",
            data={"email": "sol@e.com", "name": "Sol", "scope_read": "1"},
        )
        assert denied.status_code == 403
    conn = db.connect(db_file)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email = 'sol@e.com'"
    ).fetchone()["n"] == 0
    conn.close()


def test_onboard_over_mcp_client(tmp_path):
    # The fleet-ops story: an admin-scoped orchestrator onboards a worker over
    # the same transport agents already use.
    from athena.mcp.client import AthenaClient

    app, _ = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        raw = tc.post(
            "/tokens", json={"name": "orch", "scopes": ["admin"]}, headers=H1
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        ath = AthenaClient(client=tc, run_id="onboarding-run")

        result = ath.onboard_agent(
            email="worker@e.com", name="Worker", scopes=["read", "issue:write"]
        )
        assert result["user"]["is_agent"] is True
        assert result["token"]["token"].startswith("ath_")
        assert (
            result["mcp_config"]["mcpServers"]["athena"]["env"]["ATHENA_TOKEN"]
            == result["token"]["token"]
        )
        assert any(u["email"] == "worker@e.com" for u in ath.list_users())
    finally:
        tc.__exit__(None, None, None)
