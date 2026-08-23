"""Per-user REMOVAL — the lever after offboarding: gone from sight, kept for history.

Removing offboards (demote to viewer, revoke every session and token) and stamps
a tombstone in one atomic audited move. A removed account vanishes from every
list, picker, and email lookup and can never authenticate — while every
attributed row (issues, activity, forge sources) keeps pointing at a real user,
because the audit trail is load-bearing and nothing is deleted. These pin: the
atomic remove (counts, stamp, audit), hiding from the users list with the
explicit include_removed escape hatch, refusal on the trusted-header path and
the login/email-lookup liveness seams, id-based attribution lookups staying
unfiltered, restore returning an offboarded viewer with no credentials, the
last-admin guard (removed admins no longer count), and idempotency (a repeat
remove or restore records nothing).
"""

from fastapi.testclient import TestClient

from athena.core import activity, answerability, db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="removal.db"):
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


def test_remove_offboards_stamps_and_hides_from_lists(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        onboarded = _agent(c)
        agent_id = onboarded["user"]["id"]
        bearer = {"Authorization": f"Bearer {onboarded['token']['token']}"}
        assert c.get("/users/me", headers=bearer).status_code == 200

        removed = c.post(f"/users/{agent_id}/remove", headers=H1)
        assert removed.status_code == 200
        body = removed.json()
        assert body["removed_at"]
        assert body["revoked_token_count"] == 1

        # The token was revoked BY the removal, so the bearer path dies as a
        # revoked credential (401); the removed-refusal 403 guards the paths
        # that carry no credential (trusted header, below).
        assert c.get("/users/me", headers=bearer).status_code == 401
        denied = c.get("/users/me", headers={"X-Athena-Actor": str(agent_id)})
        assert denied.status_code == 403
        assert denied.json()["detail"] == "account is removed"

        # Hidden from the default list; visible only via the explicit
        # tombstone escape hatch, carrying its stamp.
        assert agent_id not in [u["id"] for u in c.get("/users", headers=H1).json()]
        tombstones = {
            u["id"]: u for u in c.get("/users?include_removed=true", headers=H1).json()
        }
        assert tombstones[agent_id]["removed_at"]

    # Attribution lookups stay unfiltered: the row resolves by id, the
    # liveness lookup by email does not. Rosters that run their own SQL
    # (the cockpit's answerability ledger) exclude the tombstone too — the
    # surface the live-HTTP proof caught bypassing list_users.
    conn = db.connect(db_file)
    assert users.get_user(conn, agent_id) is not None
    assert users.get_user_by_email(conn, "sol@e.com") is None
    assert agent_id not in [u["id"] for u in users.list_users(conn)]
    ledger = answerability.build_answerability(conn)["agents"]
    assert agent_id not in [row["agent_id"] for row in ledger]
    conn.close()


def test_removed_account_cannot_log_back_in(tmp_path):
    app, _ = _app(tmp_path)
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
        c.post(
            "/login",
            data={"email": "m@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert "Sign out" in c.get("/").text

        assert c.post("/users/2/remove", headers=H1).status_code == 200
        # The session was revoked by the removal...
        assert "Sign out" not in c.get("/").text
        # ...and the password no longer opens a new one: the liveness seam
        # treats a removed email exactly like an unknown one.
        c.post(
            "/login",
            data={"email": "m@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert "Sign out" not in c.get("/").text


def test_restore_returns_an_offboarded_viewer_with_no_credentials(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        onboarded = _agent(c)
        agent_id = onboarded["user"]["id"]
        bearer = {"Authorization": f"Bearer {onboarded['token']['token']}"}
        c.post(f"/users/{agent_id}/remove", headers=H1)

        restored = c.post(f"/users/{agent_id}/restore", headers=H1)
        assert restored.status_code == 200
        assert restored.json()["removed_at"] is None
        assert restored.json()["role"] == "viewer"

        # Back on the roster — but with nothing else back: the old token
        # stays revoked, and re-credentialing is its own audited step.
        assert agent_id in [u["id"] for u in c.get("/users", headers=H1).json()]
        assert c.get("/users/me", headers=bearer).status_code == 401

    assert [e["target_id"] for e in _events(db_file, "restored_user")] == [agent_id]


def test_remove_is_audited_once_and_idempotent(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent_id = _agent(c)["user"]["id"]
        first = c.post(f"/users/{agent_id}/remove", headers=H1)
        again = c.post(f"/users/{agent_id}/remove", headers=H1)
        assert first.status_code == again.status_code == 200
        # The repeat is a no-op: nothing left to revoke, same stamp kept.
        assert again.json()["revoked_token_count"] == 0
        assert again.json()["removed_at"] == first.json()["removed_at"]

    events = _events(db_file, "removed_user")
    assert [e["target_id"] for e in events] == [agent_id]
    assert events[0]["actor_id"] == 1


def test_cannot_remove_the_last_admin_and_removed_admins_do_not_count(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        assert c.post("/users/1/remove", headers=H1).status_code == 409

        c.post(
            "/users",
            json={"email": "b@e.com", "name": "Bea", "password": "pw", "role": "admin"},
            headers=H1,
        )
        # Two admins: removing one is allowed...
        assert c.post("/users/2/remove", headers=H1).status_code == 200
        # ...and the removed one no longer satisfies the guard.
        assert c.post("/users/1/remove", headers=H1).status_code == 409

    conn = db.connect(db_file)
    assert users.count_admins(conn) == 1
    conn.close()


def test_remove_requires_the_admin_role(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        onboarded = _agent(c)
        bearer = {"Authorization": f"Bearer {onboarded['token']['token']}"}
        denied = c.post("/users/1/remove", headers=bearer)
        assert denied.status_code in (401, 403)
        restore_denied = c.post("/users/1/restore", headers=bearer)
        assert restore_denied.status_code in (401, 403)
