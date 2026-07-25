"""Pausing an account also freezes its stored idempotency receipts.

Pause is "the operator lever between watch and revoke": every authenticated
action is refused at identity resolution. But the idempotency middleware
authenticates by credential liveness alone and served stored replays WITHOUT
reaching that gate — so a paused (e.g. suspected-compromised) agent credential
could still read completed mutation responses for up to the receipt TTL, and
`paused_at` was not among the 0043 authorization-revision fence columns, so the
stored bodies survived the pause untouched. These pin the two closures: the
middleware skips claim/replay entirely for a paused account (the route's 403 +
audited refusal answers instead, exactly as for an unkeyed request), and any
pause-state flip bumps the global authorization revision (migration 0061), so
pre-pause receipts are permanently fenced rather than replayable after resume.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com"):
    """Onboard an agent user with a scoped token; returns {user, token, ...}."""
    return client.post(
        "/users/onboard_agent",
        json={
            "email": email,
            "name": email.split("@")[0],
            "scopes": ["read", "issue:write"],
        },
        headers=H1,
    ).json()


def _revision(db_file):
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT revision FROM idempotency_authorization_state WHERE singleton = 1"
    ).fetchone()
    conn.close()
    return row["revision"]


def _set_paused(client, user_id, paused):
    response = client.put(
        f"/users/{user_id}/paused", json={"paused": paused}, headers=H1
    )
    assert response.status_code == 200
    return response


def test_paused_bearer_cannot_replay_and_resume_stays_fenced(tmp_path):
    db_file = tmp_path / "pause-replay.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        onboarded = _agent(client)
        agent_id = onboarded["user"]["id"]
        keyed = {
            "Authorization": f"Bearer {onboarded['token']['token']}",
            "Idempotency-Key": "before-pause",
        }

        first = client.post("/issues", json={"title": "created"}, headers=keyed)
        assert first.status_code == 201

        _set_paused(client, agent_id, True)
        while_paused = client.post("/issues", json={"title": "created"}, headers=keyed)
        # The canonical pause refusal — not the stored 201 body, no replay marker.
        assert while_paused.status_code == 403
        assert while_paused.json() == {"detail": "account is paused"}
        assert while_paused.headers.get("idempotent-replay") is None

        _set_paused(client, agent_id, False)
        after_resume = client.post("/issues", json={"title": "created"}, headers=keyed)
        # The pause/resume flips bumped the authorization revision, so the
        # pre-pause receipt is permanently fenced with the explicit
        # authorization-changed conflict, never replayed under post-resume
        # authorization.
        assert after_resume.status_code == 409
        assert after_resume.json()["code"] == "idempotency_authorization_changed"

        # The fence never re-ran the mutation: exactly one issue exists.
        issues = client.get("/issues", headers=H1).json()
        assert [i["title"] for i in issues] == ["created"]

    conn = db.connect(db_file)
    refusals = conn.execute(
        "SELECT actor_id FROM activity WHERE verb = 'paused_account_refused'"
    ).fetchall()
    conn.close()
    # The keyed attempt landed on the trail like any other paused refusal.
    assert [r["actor_id"] for r in refusals] == [agent_id]


def test_paused_keyed_request_claims_no_receipt(tmp_path):
    db_file = tmp_path / "pause-noclaim.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        onboarded = _agent(client)
        _set_paused(client, onboarded["user"]["id"], True)
        keyed = {
            "Authorization": f"Bearer {onboarded['token']['token']}",
            "Idempotency-Key": "while-paused",
        }
        refused = client.post("/issues", json={"title": "never"}, headers=keyed)
        assert refused.status_code == 403
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT idempotency_key FROM idempotency_keys WHERE idempotency_key = ?",
        ("while-paused",),
    ).fetchall()
    conn.close()
    # No ownership row was claimed for the refused request — a paused account
    # cannot occupy a key it was never allowed to execute under.
    assert rows == []


def test_pause_and_resume_each_bump_the_authorization_revision(tmp_path):
    db_file = tmp_path / "pause-revision.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        agent_id = _agent(client)["user"]["id"]
        before = _revision(db_file)
        _set_paused(client, agent_id, True)
        assert _revision(db_file) == before + 1
        _set_paused(client, agent_id, False)
        assert _revision(db_file) == before + 2
