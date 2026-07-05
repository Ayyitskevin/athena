"""Tests for signed-in users changing their own browser password."""

from fastapi.testclient import TestClient

from athena.core import db, sessions
from athena.main import create_app


def _user(client, email="kevin@example.com", name="Kevin", password="secret"):
    r = client.post(
        "/users",
        json={"email": email, "name": name, "password": password},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email="kevin@example.com", password="secret"):
    r = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _password_hash(db_path):
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT password_hash FROM users").fetchone()[
            "password_hash"
        ]
    finally:
        conn.close()


def test_password_settings_requires_login_and_is_linked_from_nav(tmp_path):
    app = create_app(tmp_path / "password_nav.db")
    with TestClient(app) as client:
        _user(client)

        anon = client.get("/settings/password")
        assert anon.status_code == 401
        assert "sign in" in anon.text

        _login(client)
        home = client.get("/")
        assert "/settings/password" in home.text

        page = client.get("/settings/password")
        assert page.status_code == 200
        assert "Current password" in page.text
        assert "New password" in page.text


def test_user_can_change_their_own_password(tmp_path):
    db_path = tmp_path / "password_change.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _user(client, password="old-pass")
        _login(client, password="old-pass")
        before = _password_hash(db_path)

        changed = client.post(
            "/settings/password",
            data={
                "current_password": "old-pass",
                "new_password": "new-pass",
                "confirm_password": "new-pass",
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303, changed.text
        assert changed.headers["location"] == "/settings/password?updated=1"
        assert _password_hash(db_path) != before

        success = client.get("/settings/password?updated=1")
        assert "Password updated." in success.text

        old_password = client.post(
            "/login",
            data={"email": "kevin@example.com", "password": "old-pass"},
            follow_redirects=False,
        )
        assert old_password.status_code == 401

        new_password = client.post(
            "/login",
            data={"email": "kevin@example.com", "password": "new-pass"},
            follow_redirects=False,
        )
        assert new_password.status_code == 303


def test_password_change_revokes_other_sessions_but_keeps_current(tmp_path):
    # WHY: changing the password must revoke sessions on OTHER devices — a shared/stolen/
    # forgotten browser signed in on the old password must not keep riding a live cookie
    # for up to SESSION_TTL_DAYS — while the browser that made the change stays logged in.
    db_path = tmp_path / "revoke.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        user = _user(client, password="old-pass")
        _login(client, password="old-pass")  # this browser's session
        # A second device's live session for the same user, created out-of-band.
        conn = db.connect(db_path)
        other_raw = sessions.create_session(conn, user["id"])
        assert sessions.resolve_session(conn, other_raw) is not None
        conn.close()

        changed = client.post(
            "/settings/password",
            data={
                "current_password": "old-pass",
                "new_password": "new-pass",
                "confirm_password": "new-pass",
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303

        conn = db.connect(db_path)
        # The other device is signed out; the browser that changed it is not.
        assert sessions.resolve_session(conn, other_raw) is None
        conn.close()
        assert client.get("/settings/password").status_code == 200


def test_password_settings_validates_current_password_and_confirmation(tmp_path):
    db_path = tmp_path / "password_validation.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        _user(client, password="secret")
        _login(client, password="secret")
        before = _password_hash(db_path)

        wrong_current = client.post(
            "/settings/password",
            data={
                "current_password": "wrong",
                "new_password": "new-pass",
                "confirm_password": "new-pass",
            },
        )
        assert wrong_current.status_code == 400
        assert "Current password is incorrect." in wrong_current.text

        mismatch = client.post(
            "/settings/password",
            data={
                "current_password": "secret",
                "new_password": "new-pass",
                "confirm_password": "different",
            },
        )
        assert mismatch.status_code == 400
        assert "New passwords do not match." in mismatch.text

        blank = client.post(
            "/settings/password",
            data={
                "current_password": "secret",
                "new_password": "   ",
                "confirm_password": "   ",
            },
        )
        assert blank.status_code == 400
        assert "New password is required." in blank.text
        assert _password_hash(db_path) == before


def test_password_settings_post_requires_login(tmp_path):
    app = create_app(tmp_path / "password_auth.db")
    with TestClient(app) as client:
        _user(client)

        denied = client.post(
            "/settings/password",
            data={
                "current_password": "secret",
                "new_password": "new-pass",
                "confirm_password": "new-pass",
            },
        )
        assert denied.status_code == 401
        assert "sign in" in denied.text
