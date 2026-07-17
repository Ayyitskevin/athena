"""Minting and revoking API tokens are now audited atomically.

An API token IS a credential: minting one hands out live access, revoking one takes
it away. Both were bare writes with NO activity event — a user (or an admin-scoped
agent acting as one) could mint itself a fresh admin token, or quietly revoke one,
with zero trace in the append-only log. These tests pin that mint/revoke record a
'minted_token' / 'revoked_token' event in the SAME transaction as the credential
change, once per real change, that the RAW SECRET never leaks into the audit detail,
and that owner-scoping and the atomic-rejection guarantees are preserved.
"""
from fastapi.testclient import TestClient

from athena.core import activity, db, token_commands, tokens
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="tokens.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    r = client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    assert r.status_code == 201, r.text
    return r.json()  # admin, id 1


def _make_user(client, email, *, role="member"):
    r = client.post(
        "/users",
        json={"email": email, "name": email, "password": "pw", "role": role},
        headers=H1,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _events(db_file, *verbs):
    conn = db.connect(db_file)
    return [e for e in activity.list_activity(conn, limit=200) if e["verb"] in verbs]


# --- REST ------------------------------------------------------------------


def test_rest_mint_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post(
            "/tokens",
            json={"name": "ci", "scopes": ["read", "issue:write"]},
            headers=H1,
        )
        assert r.status_code == 201, r.text
        token_id = r.json()["id"]

    ev = _events(db_file, "minted_token")
    assert len(ev) == 1
    assert ev[0]["target_kind"] == "token" and ev[0]["target_id"] == token_id
    assert ev[0]["actor_id"] == 1
    # The detail names the token and its granted scopes — enough to see WHAT was
    # created and how powerful it is.
    assert "ci" in ev[0]["detail"]
    assert "read" in ev[0]["detail"] and "issue:write" in ev[0]["detail"]


def test_minted_event_never_contains_the_raw_secret(tmp_path):
    # The whole point of hashing the token is that the plaintext exists only in the
    # POST response. The audit log is long-lived and widely readable — a raw secret
    # there would re-introduce exactly the leak the hash prevents.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        raw = c.post("/tokens", json={"name": "laptop"}, headers=H1).json()["token"]

    ev = _events(db_file, "minted_token")
    assert len(ev) == 1
    assert raw not in ev[0]["detail"]
    # And it is nowhere in the whole activity table, not just this row's detail.
    conn = db.connect(db_file)
    hits = conn.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE detail LIKE ?", (f"%{raw}%",)
    ).fetchone()["n"]
    assert hits == 0


def test_rest_revoke_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        token_id = c.post("/tokens", json={"name": "laptop"}, headers=H1).json()["id"]
        assert c.delete(f"/tokens/{token_id}", headers=H1).status_code == 204
        # Revoking again is a 404 and must record nothing the second time.
        assert c.delete(f"/tokens/{token_id}", headers=H1).status_code == 404

    ev = _events(db_file, "revoked_token")
    assert len(ev) == 1
    assert ev[0]["target_kind"] == "token" and ev[0]["target_id"] == token_id
    assert ev[0]["actor_id"] == 1


def test_cross_owner_revoke_records_nothing(tmp_path):
    # Owner-scoping is a security boundary: one user cannot revoke another's token.
    # The failed attempt is a 404 AND leaves no audit event (nothing happened).
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)  # admin, id 1
        b = _make_user(c, "b@e.com")
        a_token = c.post("/tokens", json={"name": "a-tok"}, headers=H1)
        a_token_id = a_token.json()["id"]
        b_raw = c.post(
            "/tokens", json={"name": "b-tok"}, headers={"X-Athena-Actor": str(b["id"])}
        ).json()["token"]

        attempt = c.delete(
            f"/tokens/{a_token_id}", headers={"Authorization": f"Bearer {b_raw}"}
        )
        assert attempt.status_code == 404

    # A's token is untouched, and no revoked_token event was recorded for it.
    conn = db.connect(db_file)
    assert (
        conn.execute(
            "SELECT revoked_at FROM api_tokens WHERE id = ?", (a_token_id,)
        ).fetchone()["revoked_at"]
        is None
    )
    assert [
        e
        for e in activity.list_activity(conn, limit=200)
        if e["verb"] == "revoked_token" and e["target_id"] == a_token_id
    ] == []


# --- web -------------------------------------------------------------------


def test_web_mint_and_revoke_are_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)  # a@e.com / pw
        c.post("/login", data={"email": "a@e.com", "password": "pw"}, follow_redirects=False)
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")

        created = c.post(
            "/settings/tokens", data={"name": "browser", "scope_read": "1"}
        )
        assert created.status_code == 201, created.text
        conn = db.connect(db_file)
        token_id = conn.execute("SELECT id FROM api_tokens ORDER BY id").fetchone()["id"]
        conn.close()

        revoked = c.post(
            f"/settings/tokens/{token_id}/revoke", follow_redirects=False
        )
        assert revoked.status_code == 303

    assert len(_events(db_file, "minted_token")) == 1
    assert len(_events(db_file, "revoked_token")) == 1


# --- command atomicity -----------------------------------------------------


def test_command_mint_rejects_invalid_scope_atomically(tmp_path):
    # An invalid scope must roll the whole mint back — no orphan token row, no event.
    # (REST blocks bad scopes at the Pydantic edge; this pins the command's own
    # all-or-nothing guarantee, the safety net the web form and any direct caller rely on.)
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    before = conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"]
    try:
        token_commands.mint_token(conn, actor_id=1, name="bad", scopes=["wizard"])
        raise AssertionError("expected TokenCommandError")
    except token_commands.TokenCommandError as exc:
        assert exc.status_code == 422
    after = conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"]
    assert after == before  # no token was persisted
    assert [
        e for e in activity.list_activity(conn, limit=50) if e["verb"] == "minted_token"
    ] == []


def test_command_mint_records_event_in_same_transaction(tmp_path):
    # The mint and its event share one transaction: after the command returns, both
    # the token and its 'minted_token' provenance are visible together.
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    created = token_commands.mint_token(
        conn, actor_id=1, name="atomic", scopes=[tokens.READ_SCOPE]
    )
    row = conn.execute(
        "SELECT revoked_at FROM api_tokens WHERE id = ?", (created["id"],)
    ).fetchone()
    assert row is not None and row["revoked_at"] is None
    ev = [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "minted_token" and e["target_id"] == created["id"]
    ]
    assert len(ev) == 1
