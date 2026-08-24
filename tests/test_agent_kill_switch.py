"""Agent credential kill switch + one-click offboard.

The scoped-token thesis needs a STOP lever: an operator must be able to revoke a
compromised or runaway agent's credentials, not merely watch them. Before this
slice the only revoke path was owner-scoped (you could revoke your own tokens, not
another user's), so the admin cockpit could display an agent's live tokens but
offered no way to disable them.

These tests cover the whole path — data layer, the atomic audited command, REST
authorization (role AND token scope), the admin web controls, and the MCP surface —
plus the offboarding session-lockout gap on admin password reset.
"""

import pytest
from fastapi.testclient import TestClient

from athena.core import activity, agent_commands, db, sessions, tokens, users
from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError

H_ADMIN = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="kill.db"):
    db_path = tmp_path / name
    return create_app(db_path), db_path


def _bootstrap_admin(client):
    # The first user is always created as admin (id 1).
    r = client.post(
        "/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_user(client, email, name, *, role="member", is_agent=False, password="pw"):
    r = client.post(
        "/users",
        json={
            "email": email,
            "name": name,
            "password": password,
            "role": role,
            "is_agent": is_agent,
        },
        headers=H_ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mint_token(client, user_id, *, scopes=None):
    # Scopes are now required on mint; default to full access to preserve what
    # these kill-switch tests exercised before the fail-open default was removed.
    body = {"name": "t", "scopes": scopes if scopes is not None else ["admin"]}
    r = client.post("/tokens", json=body, headers={"X-Athena-Actor": str(user_id)})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _login(client, email, password="pw"):
    r = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- data layer -------------------------------------------------------------


def test_revoke_all_tokens_for_user_revokes_live_only_and_is_idempotent(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        other = _create_user(client, "o@e.com", "Other")
        _mint_token(client, agent["id"])
        _mint_token(client, agent["id"])
        _mint_token(client, other["id"])

    conn = db.connect(db_path)
    # First call revokes both of the agent's live tokens.
    assert tokens.revoke_all_tokens_for_user(conn, user_id=agent["id"]) == 2
    # Idempotent: a second call finds nothing live to revoke.
    assert tokens.revoke_all_tokens_for_user(conn, user_id=agent["id"]) == 0
    # The agent's tokens are all revoked; the other user's is untouched.
    assert all(t["revoked_at"] for t in tokens.list_tokens(conn, agent["id"]))
    assert all(t["revoked_at"] is None for t in tokens.list_tokens(conn, other["id"]))


def test_revoke_all_sessions_removes_every_row(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)

    conn = db.connect(db_path)
    a = sessions.create_session(conn, agent["id"])
    sessions.create_session(conn, agent["id"])
    assert sessions.resolve_session(conn, a) is not None
    assert sessions.revoke_all_sessions(conn, agent["id"]) == 2
    assert sessions.resolve_session(conn, a) is None


# --- command layer ----------------------------------------------------------


def test_revoke_agent_tokens_command_kills_auth_and_audits(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        raw = _mint_token(client, agent["id"])
        auth = {"Authorization": f"Bearer {raw}"}
        # The token authenticates before the kill switch.
        assert client.get("/users/me", headers=auth).status_code == 200

        conn = db.connect(db_path)
        result = agent_commands.revoke_agent_tokens(
            conn, actor=users.get_user(conn, 1), target_user_id=agent["id"]
        )
        assert result == {"user_id": agent["id"], "revoked_token_count": 1}

        # One audit event, attributed to the acting admin, targeting the agent user.
        events = activity.list_activity(conn, actor_id=1)
        killed = [e for e in events if e["verb"] == "revoked_agent_tokens"]
        assert len(killed) == 1
        assert killed[0]["target_kind"] == "user"
        assert killed[0]["target_id"] == agent["id"]

        # The token no longer authenticates.
        assert client.get("/users/me", headers=auth).status_code == 401


def test_revoke_agent_tokens_unknown_user_raises_404(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
    conn = db.connect(db_path)
    with pytest.raises(agent_commands.AgentCommandError) as ei:
        agent_commands.revoke_agent_tokens(
            conn, actor=users.get_user(conn, 1), target_user_id=999
        )
    assert ei.value.kind == "not_found"


@pytest.mark.parametrize(
    "command",
    [agent_commands.revoke_agent_tokens, agent_commands.offboard_user],
)
def test_agent_control_commands_enforce_admin_role_and_token_scope(tmp_path, command):
    """The command is the final authorization boundary, even without an adapter."""
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        member = _create_user(client, "member@e.com", "Member")
        agent = _create_user(client, "agent@e.com", "Agent", is_agent=True)
        _mint_token(client, agent["id"])

    conn = db.connect(db_path)
    admin = users.get_user(conn, 1)
    attempts = [
        (None, "unauthorized"),
        (users.get_user(conn, member["id"]), "forbidden"),
        ({**admin, "_token_scopes": ["read"]}, "forbidden"),
    ]
    for actor, kind in attempts:
        with pytest.raises(agent_commands.AgentCommandError) as ei:
            command(conn, actor=actor, target_user_id=agent["id"])
        assert ei.value.kind == kind

    # Every rejected attempt is side-effect free: credentials and role remain live,
    # and no administrative event pretends the protected action happened.
    assert users.get_user(conn, agent["id"])["role"] == "member"
    assert all(t["revoked_at"] is None for t in tokens.list_tokens(conn, agent["id"]))
    protected_verbs = {"revoked_agent_tokens", "offboarded_user"}
    assert not any(e["verb"] in protected_verbs for e in activity.list_activity(conn))


def test_offboard_user_demotes_revokes_sessions_and_tokens_atomically(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        _mint_token(client, agent["id"])

    conn = db.connect(db_path)
    sessions.create_session(conn, agent["id"])
    sessions.create_session(conn, agent["id"])

    result = agent_commands.offboard_user(
        conn, actor=users.get_user(conn, 1), target_user_id=agent["id"]
    )
    assert result["role"] == "viewer"
    assert result["revoked_session_count"] == 2
    assert result["revoked_token_count"] == 1

    # State: viewer role, no live sessions, no live tokens, one audit event.
    assert users.get_user(conn, agent["id"])["role"] == "viewer"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (agent["id"],)
        ).fetchone()["n"]
        == 0
    )
    assert all(t["revoked_at"] for t in tokens.list_tokens(conn, agent["id"]))
    events = activity.list_activity(conn, actor_id=1)
    assert sum(1 for e in events if e["verb"] == "offboarded_user") == 1


def test_offboard_refuses_to_strip_the_last_admin(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
    conn = db.connect(db_path)
    with pytest.raises(agent_commands.AgentCommandError) as ei:
        agent_commands.offboard_user(
            conn, actor=users.get_user(conn, 1), target_user_id=1
        )
    assert ei.value.kind == "conflict"
    # The lone admin is left intact — no partial demotion.
    assert users.get_user(conn, 1)["role"] == "admin"


def test_offboard_admin_allowed_when_another_admin_remains(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        second = _create_user(client, "b@e.com", "B", role="admin")
    conn = db.connect(db_path)
    result = agent_commands.offboard_user(
        conn, actor=users.get_user(conn, 1), target_user_id=second["id"]
    )
    assert result["role"] == "viewer"
    assert users.get_user(conn, second["id"])["role"] == "viewer"


# --- REST authorization -----------------------------------------------------


def test_rest_revoke_tokens_end_to_end(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        member = _create_user(client, "m@e.com", "M", role="member")
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        agent_raw = _mint_token(client, agent["id"])
        agent_auth = {"Authorization": f"Bearer {agent_raw}"}
        assert client.get("/users/me", headers=agent_auth).status_code == 200

        # A non-admin (member role) cannot reach another user's tokens.
        assert (
            client.delete(
                f"/users/{agent['id']}/tokens",
                headers={"X-Athena-Actor": str(member["id"])},
            ).status_code
            == 403
        )
        # Unauthenticated is refused too.
        assert client.delete(f"/users/{agent['id']}/tokens").status_code == 401

        # The admin can revoke ANOTHER user's tokens.
        r = client.delete(f"/users/{agent['id']}/tokens", headers=H_ADMIN)
        assert r.status_code == 200, r.text
        assert r.json() == {"user_id": agent["id"], "revoked_token_count": 1}

        # The revoked bearer token no longer authenticates.
        assert client.get("/users/me", headers=agent_auth).status_code == 401
        # Unknown target 404s.
        assert client.delete("/users/999/tokens", headers=H_ADMIN).status_code == 404


def test_rest_revoke_requires_admin_token_scope_not_just_role(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        _mint_token(client, agent["id"])
        # An admin USER, but acting through a read-scoped token: the admin token
        # scope is required, so the scope gate refuses even though the role is admin.
        read_token = _mint_token(client, 1, scopes=["read"])
        r = client.delete(
            f"/users/{agent['id']}/tokens",
            headers={"Authorization": f"Bearer {read_token}"},
        )
        assert r.status_code == 403


def test_rest_offboard_authz_and_guards(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        member = _create_user(client, "m@e.com", "M", role="member")
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        _mint_token(client, agent["id"])

        # Non-admin refused.
        assert (
            client.post(
                f"/users/{agent['id']}/offboard",
                headers={"X-Athena-Actor": str(member["id"])},
            ).status_code
            == 403
        )
        # Unknown target 404.
        assert client.post("/users/999/offboard", headers=H_ADMIN).status_code == 404
        # Offboarding the only admin (self) is refused.
        assert client.post("/users/1/offboard", headers=H_ADMIN).status_code == 409

        # Offboarding the agent succeeds and returns the counts.
        r = client.post(f"/users/{agent['id']}/offboard", headers=H_ADMIN)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["role"] == "viewer"
        assert body["revoked_token_count"] == 1


# --- web controls -----------------------------------------------------------


def test_web_agents_page_shows_controls_and_revokes(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        _mint_token(client, agent["id"])
        _login(client, "admin@e.com")

        page = client.get("/admin/agents")
        assert page.status_code == 200
        assert "Revoke all tokens" in page.text
        assert f"/admin/agents/{agent['id']}/revoke-tokens" in page.text
        assert f"/admin/agents/{agent['id']}/offboard" in page.text

        r = client.post(
            f"/admin/agents/{agent['id']}/revoke-tokens", follow_redirects=False
        )
        assert r.status_code == 303
        assert "revoked=1" in r.headers["location"]

    conn = db.connect(db_path)
    assert all(t["revoked_at"] for t in tokens.list_tokens(conn, agent["id"]))


def test_web_revoke_requires_csrf(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        _mint_token(client, agent["id"])
        _login(client, "admin@e.com")
        # Simulate a forged cross-site POST: a live session cookie but no CSRF token.
        client.headers.pop("X-CSRF-Token", None)
        r = client.post(
            f"/admin/agents/{agent['id']}/revoke-tokens", follow_redirects=False
        )
        assert r.status_code == 403

    conn = db.connect(db_path)
    # Nothing was revoked.
    assert all(t["revoked_at"] is None for t in tokens.list_tokens(conn, agent["id"]))


def test_web_revoke_requires_admin_role(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        _create_user(client, "member@e.com", "M", role="member")
        agent = _create_user(client, "ag@e.com", "Ag", is_agent=True)
        _mint_token(client, agent["id"])
        _login(client, "member@e.com")
        r = client.post(
            f"/admin/agents/{agent['id']}/revoke-tokens", follow_redirects=False
        )
        assert r.status_code == 403


def test_admin_password_reset_revokes_target_sessions(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap_admin(client)
        target = _create_user(client, "t@e.com", "T", role="member")

        conn = db.connect(db_path)
        cookie = sessions.create_session(conn, target["id"])
        assert sessions.resolve_session(conn, cookie) is not None

        _login(client, "admin@e.com")
        r = client.post(
            f"/admin/users/{target['id']}/password",
            data={"password": "brandnewpw"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    # The target's pre-existing session no longer resolves: an admin reset now
    # actually ends the target's access instead of leaving a cookie live for days.
    assert sessions.resolve_session(db.connect(db_path), cookie) is None


# --- MCP surface (client level; no MCP SDK required) ------------------------


def test_mcp_client_revoke_agent_tokens(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as tc:
        tc.post(
            "/users", json={"email": "a@e.com", "name": "A", "password": "pw"}
        )  # admin id 1
        tc.post(
            "/users",
            json={"email": "ag@e.com", "name": "Ag", "is_agent": True},
            headers=H_ADMIN,
        )  # agent id 2
        agent_raw = tc.post(
            "/tokens",
            json={"name": "t", "scopes": ["admin"]},
            headers={"X-Athena-Actor": "2"},
        ).json()["token"]
        admin_raw = tc.post(
            "/tokens", json={"name": "adm", "scopes": ["admin"]}, headers=H_ADMIN
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {admin_raw}"})

        mcp = AthenaClient(client=tc)
        out = mcp.revoke_agent_tokens(2)
        assert out["revoked_token_count"] == 1
        # The agent's token is dead.
        assert (
            tc.get(
                "/users/me", headers={"Authorization": f"Bearer {agent_raw}"}
            ).status_code
            == 401
        )


def test_mcp_client_offboard_requires_admin(tmp_path):
    app, db_path = _app(tmp_path)
    with TestClient(app) as tc:
        tc.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        tc.post(
            "/users",
            json={
                "email": "m@e.com",
                "name": "M",
                "password": "pw",
                "role": "member",
            },
            headers=H_ADMIN,
        )  # member id 2
        member_raw = tc.post(
            "/tokens",
            json={"name": "t", "scopes": ["admin"]},
            headers={"X-Athena-Actor": "2"},
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {member_raw}"})

        mcp = AthenaClient(client=tc)
        with pytest.raises(AthenaError) as ei:
            mcp.offboard_user(1)
        assert ei.value.status_code == 403
