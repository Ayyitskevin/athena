"""The SSO login routes (/login/sso, /auth/callback) and the login-state handoff.

The flow-core crypto is tested in test_oidc_flow.py; here the network/verify calls
are stubbed so these tests pin the WIRING and its security gates: the routes 404
when SSO is unconfigured; /login/sso stashes a single-use state and binds it to the
browser with a cookie; /auth/callback refuses a state that doesn't match the cookie
(login CSRF), refuses an unknown/expired/consumed state, surfaces an IdP error, and
fails closed when the token/policy is rejected — and on success mints a session
exactly like a password login. Plus the login-state data layer's single-use + expiry.
"""

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from athena import config
from athena.core import db, oidc, oidc_flow, users
from athena.main import create_app

_DISCOVERY = {
    "issuer": "https://idp.example.com",
    "authorization_endpoint": "https://idp.example.com/authorize",
    "token_endpoint": "https://idp.example.com/token",
    "jwks_uri": "https://idp.example.com/jwks",
}


def _enable_oidc(monkeypatch, *, allowed_domains=()):
    monkeypatch.setattr(config, "OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "athena-client")
    monkeypatch.setattr(config, "OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setattr(
        config, "OIDC_REDIRECT_URL", "https://app.example.com/auth/callback"
    )
    monkeypatch.setattr(config, "OIDC_ALLOWED_DOMAINS", allowed_domains)


def _stub_idp(monkeypatch, *, claims):
    monkeypatch.setattr(oidc_flow, "discover", lambda issuer: _DISCOVERY)
    monkeypatch.setattr(oidc_flow, "exchange_code", lambda *a, **k: {"id_token": "tok"})
    monkeypatch.setattr(oidc_flow, "verify_id_token", lambda *a, **k: claims)


def _begin_login(client):
    """Drive /login/sso and return the state it minted (the cookie is now set)."""
    r = client.get("/login/sso", follow_redirects=False)
    assert r.status_code == 302, r.text
    query = parse_qs(urlparse(r.headers["location"]).query)
    state = query["state"][0]
    assert client.cookies.get("athena_oidc_state") == state
    return state, query


# --- the routes 404 when SSO is off -----------------------------------------


def test_routes_404_when_sso_disabled(tmp_path):
    with TestClient(create_app(tmp_path / "off.db")) as client:
        assert client.get("/login/sso", follow_redirects=False).status_code == 404
        assert (
            client.get(
                "/auth/callback?code=x&state=y", follow_redirects=False
            ).status_code
            == 404
        )


# --- /login/sso begins the flow ---------------------------------------------


def test_login_sso_redirects_with_pkce_and_binds_state(tmp_path, monkeypatch):
    db_file = tmp_path / "begin.db"
    with TestClient(create_app(db_file)) as client:
        _enable_oidc(monkeypatch)
        monkeypatch.setattr(oidc_flow, "discover", lambda issuer: _DISCOVERY)
        state, query = _begin_login(client)
        # The redirect carries the OIDC params, incl. PKCE S256.
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["athena-client"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == ["https://app.example.com/auth/callback"]
        # The state was stashed server-side for the callback to consume.
        conn = db.connect(db_file)
        try:
            row = conn.execute(
                "SELECT nonce, code_verifier FROM oidc_login_states WHERE state = ?",
                (state,),
            ).fetchone()
            assert row is not None and row["nonce"] and row["code_verifier"]
        finally:
            conn.close()


# --- /auth/callback: happy path + every gate --------------------------------


def test_callback_provisions_user_and_mints_session(tmp_path, monkeypatch):
    db_file = tmp_path / "ok.db"
    with TestClient(create_app(db_file)) as client:
        _enable_oidc(monkeypatch)
        _stub_idp(
            monkeypatch,
            claims={
                "sub": "u-1",
                "email": "new@acme.com",
                "email_verified": True,
                "name": "New",
            },
        )
        state, _ = _begin_login(client)

        done = client.get(
            f"/auth/callback?code=abc&state={state}", follow_redirects=False
        )
        assert done.status_code == 303 and done.headers["location"] == "/aegis"
        assert done.cookies.get("athena_session")  # a real session was minted

        # The user exists and the IdP identity is linked.
        conn = db.connect(db_file)
        try:
            user = users.get_user_by_email(conn, "new@acme.com")
            assert user is not None and user["role"] == users.MEMBER_ROLE
            assert (
                oidc.find_user_by_identity(
                    conn, issuer=config.OIDC_ISSUER, subject="u-1"
                )["id"]
                == user["id"]
            )
        finally:
            conn.close()


def test_callback_rejects_state_cookie_mismatch(tmp_path, monkeypatch):
    # A forged/replayed callback whose state doesn't match this browser's cookie is
    # refused — the login-CSRF defense.
    with TestClient(create_app(tmp_path / "csrf.db")) as client:
        _enable_oidc(monkeypatch)
        _stub_idp(
            monkeypatch,
            claims={"sub": "x", "email": "a@acme.com", "email_verified": True},
        )
        _begin_login(client)  # sets the cookie to the real state
        bad = client.get(
            "/auth/callback?code=abc&state=not-the-cookie-state", follow_redirects=False
        )
        assert bad.status_code == 400
        assert not bad.cookies.get("athena_session")


def test_callback_rejects_unknown_or_consumed_state(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "consumed.db")) as client:
        _enable_oidc(monkeypatch)
        _stub_idp(
            monkeypatch,
            claims={"sub": "x", "email": "a@acme.com", "email_verified": True},
        )
        state, _ = _begin_login(client)
        # First callback succeeds and consumes the state.
        assert (
            client.get(
                f"/auth/callback?code=abc&state={state}", follow_redirects=False
            ).status_code
            == 303
        )
        # The browser still presents the same state value, but it's been used — refuse.
        client.cookies.set("athena_oidc_state", state)
        replay = client.get(
            f"/auth/callback?code=abc&state={state}", follow_redirects=False
        )
        assert replay.status_code == 400


def test_callback_surfaces_idp_error(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "err.db")) as client:
        _enable_oidc(monkeypatch)
        monkeypatch.setattr(oidc_flow, "discover", lambda issuer: _DISCOVERY)
        _begin_login(client)
        denied = client.get(
            "/auth/callback?error=access_denied&state=whatever", follow_redirects=False
        )
        assert denied.status_code == 400
        assert not denied.cookies.get("athena_session")


def test_callback_fails_closed_on_unverified_email(tmp_path, monkeypatch):
    # The token validates but the policy refuses an unverified email — no session,
    # no account, and a generic error (no detail leaked).
    db_file = tmp_path / "unverified.db"
    with TestClient(create_app(db_file)) as client:
        _enable_oidc(monkeypatch)
        _stub_idp(
            monkeypatch,
            claims={
                "sub": "u",
                "email": "a@acme.com",
                "email_verified": False,
            },
        )
        state, _ = _begin_login(client)
        failed = client.get(
            f"/auth/callback?code=abc&state={state}", follow_redirects=False
        )
        assert failed.status_code == 400
        assert not failed.cookies.get("athena_session")
        conn = db.connect(db_file)
        try:
            assert users.count_users(conn) == 0
        finally:
            conn.close()


def test_login_page_shows_sso_button_only_when_configured(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "btn.db")) as client:
        # Off by default: no SSO button.
        assert "/login/sso" not in client.get("/login").text
        # Configured: the button appears.
        _enable_oidc(monkeypatch)
        assert "/login/sso" in client.get("/login").text


# --- login-state data layer -------------------------------------------------


def _conn(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    db.migrate(conn)
    return conn


def test_take_login_state_is_single_use(tmp_path):
    conn = _conn(tmp_path)
    oidc.start_login(conn, state="s1", nonce="n1", code_verifier="v1")
    assert oidc.take_login_state(conn, "s1") == {"nonce": "n1", "code_verifier": "v1"}
    # A code/state pair can't be replayed — the row is gone.
    assert oidc.take_login_state(conn, "s1") is None
    conn.close()


def test_take_login_state_expires(tmp_path):
    conn = _conn(tmp_path)
    oidc.start_login(conn, state="s1", nonce="n1", code_verifier="v1")
    conn.execute(
        "UPDATE oidc_login_states SET created_at = datetime('now', '-1 hour') WHERE state = 's1'"
    )
    conn.commit()
    assert oidc.take_login_state(conn, "s1", max_age_seconds=600) is None
    conn.close()
