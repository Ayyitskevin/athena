"""Refusals leave a trace — an actor probing its boundary is now on the trail.

Success was always first-class history; failures were invisible: an agent could
guess passwords, present a revoked token, or walk into its scope wall a hundred
times with zero events. These pin the four failure verbs (login_failed,
revoked_token_used, scope_denied, paused_account_refused), their attribution to
the account involved, that responses stay byte-identical (no oracle changes),
that unknown principals record NOTHING (no existence probes via the trail), and
that invalid-bearer hammering is now rate-limited before it can flood the log.
"""
from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="failaudit.db", **kwargs):
    return create_app(tmp_path / name, **kwargs), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"})


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=50)
    conn.close()
    return rows


def test_failed_login_against_a_real_account_is_recorded(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post("/login", data={"email": "ann@e.com", "password": "wrong"})
        assert r.status_code == 401
        assert "Invalid email or password." in r.text  # response stays opaque

    events = _events(db_file, "login_failed")
    assert [(e["actor_id"], e["target_id"]) for e in events] == [(1, 1)]
    assert events[0]["detail"] == "failed browser login"


def test_unknown_email_login_records_nothing(tmp_path):
    # The trail must not become an existence oracle: probing a nonexistent
    # account leaves no row (there is no account to attribute it to).
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post("/login", data={"email": "ghost@e.com", "password": "x"})
        assert r.status_code == 401
    assert _events(db_file, "login_failed") == []


def test_revoked_token_use_is_attributed_to_its_owner(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        minted = c.post(
            "/tokens", json={"name": "t", "scopes": ["read"]}, headers=H1
        ).json()
        assert c.delete(f"/tokens/{minted['id']}", headers=H1).status_code == 204
        denied = c.get(
            "/users/me", headers={"Authorization": f"Bearer {minted['token']}"}
        )
        assert denied.status_code == 401
        assert denied.json()["detail"] == "invalid or revoked token"
        # Garbage that never was a token records nothing.
        assert (
            c.get("/users/me", headers={"Authorization": "Bearer ath_nonsense"})
            .status_code
            == 401
        )

    events = _events(db_file, "revoked_token_used")
    assert [(e["actor_id"], e["target_kind"], e["target_id"]) for e in events] == [
        (1, "token", minted["id"])
    ]


def test_scope_denial_is_recorded_with_the_probe_context(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        raw = c.post(
            "/tokens", json={"name": "ro", "scopes": ["read"]}, headers=H1
        ).json()["token"]
        denied = c.post(
            "/issues",
            json={"title": "past the wall"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "token scope required: issue:write"

    events = _events(db_file, "scope_denied")
    assert len(events) == 1
    assert events[0]["actor_id"] == 1
    assert events[0]["detail"] == "issue:write on POST /issues"


def test_paused_account_attempts_are_recorded_per_try(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = c.post(
            "/users/onboard_agent",
            json={"email": "sol@e.com", "name": "Sol", "scopes": ["read"]},
            headers=H1,
        ).json()
        c.put(
            f"/users/{agent['user']['id']}/paused", json={"paused": True}, headers=H1
        )
        bearer = {"Authorization": f"Bearer {agent['token']['token']}"}
        for _ in range(2):
            assert c.get("/issues", headers=bearer).status_code == 403

    events = _events(db_file, "paused_account_refused")
    assert [e["actor_id"] for e in events] == [agent["user"]["id"]] * 2


def test_invalid_bearer_hammering_is_rate_limited(tmp_path):
    # Before this slice, invalid-token requests were entirely unthrottled; now
    # the anonymous limiter bounds them (and therefore bounds trail writes).
    app, _ = _app(tmp_path, anon_rate_limit_per_minute=3)
    with TestClient(app) as c:
        _bootstrap(c)
        statuses = [
            c.get(
                "/users/me", headers={"Authorization": "Bearer ath_wrong"}
            ).status_code
            for _ in range(5)
        ]
    assert statuses[0] == 401
    assert 429 in statuses


def test_failure_recording_never_breaks_the_refusal_on_foreign_run(tmp_path):
    # A scope denial carrying ANOTHER actor's run header: the recording would
    # violate run binding, so it is skipped — but the 403 still goes out clean.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post(
            "/issues",
            json={"title": "claims the run"},
            headers={**H1, "X-Athena-Run": "owned-by-ann"},
        )
        c.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "role": "member"},
            headers=H1,
        )
        raw = c.post(
            "/tokens",
            json={"name": "ro", "scopes": ["read"]},
            headers={"X-Athena-Actor": "2"},
        ).json()["token"]
        denied = c.post(
            "/issues",
            json={"title": "x"},
            headers={
                "Authorization": f"Bearer {raw}",
                "X-Athena-Run": "owned-by-ann",
            },
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "token scope required: issue:write"
    # The event was skipped (binding refused), not recorded against the run.
    assert _events(db_file, "scope_denied") == []
