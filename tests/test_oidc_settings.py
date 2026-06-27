"""The linked-identities settings page (/settings/identities).

Wires up the view/unlink half of SSO: a user sees the IdP identities linked to their
account and can unlink one — but never their LAST way to sign in (an SSO-only user
with one identity is locked to it until they set a password). A user can only unlink
their OWN identities, and the route is login-gated + CSRF-protected.
"""
from fastapi.testclient import TestClient

from athena import config
from athena.core import db, oidc, sessions, users
from athena.main import create_app

ISS = "https://idp.example.com"


def _login_as(client, db_file, user_id):
    """Give the client a real session for user_id (works for SSO-only users with no
    password, which a password login couldn't produce)."""
    conn = db.connect(db_file)
    try:
        raw = sessions.create_session(conn, user_id)
        csrf = sessions.csrf_token_for(conn, raw)
    finally:
        conn.close()
    client.cookies.set("athena_session", raw)
    client.headers["X-CSRF-Token"] = csrf


def _make_user(db_file, email, name, *, password=None):
    conn = db.connect(db_file)
    try:
        return users.create_user(conn, email=email, name=name, password=password)["id"]
    finally:
        conn.close()


def _link(db_file, *, user_id, issuer=ISS, subject="sub-1"):
    conn = db.connect(db_file)
    try:
        oidc.link_identity(conn, issuer=issuer, subject=subject, user_id=user_id)
    finally:
        conn.close()


def _identities(db_file, user_id):
    conn = db.connect(db_file)
    try:
        return oidc.list_identities(conn, user_id)
    finally:
        conn.close()


def test_lists_linked_identities(tmp_path):
    db_file = tmp_path / "list.db"
    with TestClient(create_app(db_file)) as client:
        uid = _make_user(db_file, "a@e.com", "A", password="pw")
        _link(db_file, user_id=uid, issuer="https://idp-one.example", subject="s1")
        _link(db_file, user_id=uid, issuer="https://idp-two.example", subject="s2")
        _login_as(client, db_file, uid)

        page = client.get("/settings/identities")
        assert page.status_code == 200
        assert "https://idp-one.example" in page.text
        assert "https://idp-two.example" in page.text


def test_empty_state_when_none_linked(tmp_path):
    db_file = tmp_path / "empty.db"
    with TestClient(create_app(db_file)) as client:
        uid = _make_user(db_file, "a@e.com", "A", password="pw")
        _login_as(client, db_file, uid)
        assert "No single sign-on identities" in client.get("/settings/identities").text


def test_unlink_allowed_with_a_password(tmp_path):
    db_file = tmp_path / "unlink.db"
    with TestClient(create_app(db_file)) as client:
        uid = _make_user(db_file, "a@e.com", "A", password="pw")
        _link(db_file, user_id=uid, subject="s1")
        _login_as(client, db_file, uid)

        done = client.post(
            "/settings/identities/unlink",
            data={"issuer": ISS, "subject": "s1"},
            follow_redirects=False,
        )
        assert done.status_code == 303
        assert _identities(db_file, uid) == []


def test_cannot_unlink_only_signin_method(tmp_path):
    # SSO-only user (no password), single identity → unlinking would lock them out.
    db_file = tmp_path / "lockout.db"
    with TestClient(create_app(db_file)) as client:
        uid = _make_user(db_file, "sso@e.com", "SSO")  # no password
        _link(db_file, user_id=uid, subject="s1")
        _login_as(client, db_file, uid)

        refused = client.post(
            "/settings/identities/unlink",
            data={"issuer": ISS, "subject": "s1"},
            follow_redirects=False,
        )
        assert refused.status_code == 409
        assert len(_identities(db_file, uid)) == 1  # still linked


def test_unlink_allowed_when_another_identity_remains(tmp_path):
    # SSO-only but with two identities → one can go; the other keeps them in.
    db_file = tmp_path / "two.db"
    with TestClient(create_app(db_file)) as client:
        uid = _make_user(db_file, "sso@e.com", "SSO")  # no password
        _link(db_file, user_id=uid, issuer="https://idp-a.example", subject="a")
        _link(db_file, user_id=uid, issuer="https://idp-b.example", subject="b")
        _login_as(client, db_file, uid)

        done = client.post(
            "/settings/identities/unlink",
            data={"issuer": "https://idp-a.example", "subject": "a"},
            follow_redirects=False,
        )
        assert done.status_code == 303
        remaining = _identities(db_file, uid)
        assert [i["issuer"] for i in remaining] == ["https://idp-b.example"]


def test_cannot_unlink_another_users_identity(tmp_path):
    # The authz check: a user may only unlink their OWN links, even though the
    # (issuer, subject) pair is globally unique.
    db_file = tmp_path / "authz.db"
    with TestClient(create_app(db_file)) as client:
        attacker = _make_user(db_file, "att@e.com", "Att", password="pw")
        victim = _make_user(db_file, "vic@e.com", "Vic", password="pw")
        _link(db_file, user_id=victim, issuer=ISS, subject="victim-sub")
        _login_as(client, db_file, attacker)

        denied = client.post(
            "/settings/identities/unlink",
            data={"issuer": ISS, "subject": "victim-sub"},
            follow_redirects=False,
        )
        assert denied.status_code == 404
        assert len(_identities(db_file, victim)) == 1  # untouched


def test_requires_login(tmp_path):
    with TestClient(create_app(tmp_path / "auth.db")) as client:
        assert client.get("/settings/identities").status_code == 401


def test_unlink_requires_csrf(tmp_path):
    db_file = tmp_path / "csrf.db"
    with TestClient(create_app(db_file)) as client:
        uid = _make_user(db_file, "a@e.com", "A", password="pw")
        _link(db_file, user_id=uid, subject="s1")
        _login_as(client, db_file, uid)
        client.headers.pop("X-CSRF-Token", None)  # drop the token
        blocked = client.post(
            "/settings/identities/unlink",
            data={"issuer": ISS, "subject": "s1"},
            follow_redirects=False,
        )
        assert blocked.status_code == 403
        assert len(_identities(db_file, uid)) == 1  # not unlinked


def test_nav_shows_signins_link_only_when_oidc_enabled(tmp_path, monkeypatch):
    db_file = tmp_path / "nav.db"
    with TestClient(create_app(db_file)) as client:
        uid = _make_user(db_file, "a@e.com", "A", password="pw")
        _login_as(client, db_file, uid)
        # Off: no nav link.
        assert "/settings/identities" not in client.get("/aegis").text
        # Configured: the link appears.
        monkeypatch.setattr(config, "OIDC_ISSUER", "https://idp.example.com")
        monkeypatch.setattr(config, "OIDC_CLIENT_ID", "c")
        monkeypatch.setattr(config, "OIDC_CLIENT_SECRET", "s")
        monkeypatch.setattr(config, "OIDC_REDIRECT_URL", "https://app/cb")
        assert "/settings/identities" in client.get("/aegis").text
