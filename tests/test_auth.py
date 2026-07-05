"""Tests for browser login: passwords, sessions, and the cookie contract.

These encode the security contract, not just HTTP shapes: passwords are stored
hashed (never plaintext), a wrong password and an unknown email are
indistinguishable, a session cookie authenticates the browser, logout revokes it
server-side at once, an expired session stops working, and the UI write path
(create issue) is gated on being logged in.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from athena import config
from athena.core import db, sessions, users
from athena.main import create_app

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _user(client, email="kevin@example.com", name="Kevin", password="correct horse"):
    return client.post(
        "/users", json={"email": email, "name": name, "password": password}
    ).json()


def test_password_is_stored_hashed_never_plaintext(tmp_path):
    # WHY: a leak of the users table must not hand over usable passwords. The
    # column holds a pbkdf2 hash, and the raw password is never persisted or
    # echoed back through the API.
    app = create_app(tmp_path / "hash.db")
    with TestClient(app) as client:
        out = _user(client, password="hunter2")
        assert "password" not in out
        assert "password_hash" not in out

        c = db.connect(tmp_path / "hash.db")
        stored = c.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
        c.close()
        assert stored is not None
        assert "hunter2" not in stored
        assert stored.startswith("pbkdf2_sha256$")


def test_login_success_sets_session_cookie(tmp_path):
    # WHY: a correct email+password must mint a session and hand the browser a
    # cookie, then bounce it to the dashboard.
    app = create_app(tmp_path / "login.db")
    with TestClient(app) as client:
        _user(client, password="secret")
        r = client.post(
            "/login",
            data={"email": "kevin@example.com", "password": "secret"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/aegis"
        assert config.SESSION_COOKIE in r.cookies


def test_wrong_password_and_unknown_email_are_indistinguishable(tmp_path):
    # WHY: the login form must never reveal whether an email exists. A wrong
    # password (real account) and an unknown account return the same status and
    # the same opaque message, and neither mints a session. (The form echoes the
    # submitted email back — attacker-supplied, not a leak — so we compare the
    # status and error text, not the whole body.)
    app = create_app(tmp_path / "opaque.db")
    with TestClient(app) as client:
        _user(client, password="secret")

        wrong_pw = client.post(
            "/login",
            data={"email": "kevin@example.com", "password": "nope"},
            follow_redirects=False,
        )
        no_user = client.post(
            "/login",
            data={"email": "ghost@example.com", "password": "whatever"},
            follow_redirects=False,
        )

        assert wrong_pw.status_code == no_user.status_code == 401
        assert "Invalid email or password" in wrong_pw.text
        assert "Invalid email or password" in no_user.text
        assert config.SESSION_COOKIE not in wrong_pw.cookies
        assert config.SESSION_COOKIE not in no_user.cookies


def test_logout_destroys_session_server_side(tmp_path):
    # WHY: logout is a kill switch, not just a cookie clear. After logout the
    # session row is gone, so even a replayed cookie value is worthless.
    app = create_app(tmp_path / "logout.db")
    with TestClient(app) as client:
        _user(client, password="secret")
        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        # Logout is CSRF-protected; echo the token cookie in the header.
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        raw = client.cookies.get(config.SESSION_COOKIE)
        assert raw is not None

        # The session resolves before logout...
        c = db.connect(tmp_path / "logout.db")
        assert sessions.resolve_session(c, raw) is not None

        client.post("/logout", follow_redirects=False)

        # ...and the row is deleted after.
        c2 = db.connect(tmp_path / "logout.db")
        assert sessions.resolve_session(c2, raw) is None
        c.close()
        c2.close()


def test_expired_session_does_not_authenticate(tmp_path):
    # WHY: a session must die at its expiry. We backdate the row and confirm the
    # cookie no longer resolves to a user.
    app = create_app(tmp_path / "expire.db")
    with TestClient(app) as client:
        _user(client, password="secret")
        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        raw = client.cookies.get(config.SESSION_COOKIE)

        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(_TS_FMT)
        c = db.connect(tmp_path / "expire.db")
        c.execute("UPDATE sessions SET expires_at = ?", (past,))
        c.commit()
        assert sessions.resolve_session(c, raw) is None
        c.close()


def test_issue_creation_is_gated_on_login(tmp_path):
    # WHY: the UI write path must obey the same actor rule as the API — no
    # anonymous writes. Logged out, POST /aegis/issues is refused with a sign-in
    # prompt; logged in, it creates the issue stamped to that user.
    app = create_app(tmp_path / "gate.db")
    with TestClient(app) as client:
        user = _user(client, password="secret")

        logged_out = client.post("/aegis/issues", data={"title": "anon"})
        assert logged_out.status_code == 401
        assert "sign in" in logged_out.text.lower()

        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        logged_in = client.post("/aegis/issues", data={"title": "mine"})
        assert logged_in.status_code == 200

        # The issue exists and is stamped to the logged-in user.
        c = db.connect(tmp_path / "gate.db")
        row = c.execute(
            "SELECT created_by FROM issues WHERE title = 'mine'"
        ).fetchone()
        c.close()
        assert row["created_by"] == user["id"]


def test_nav_reflects_login_state(tmp_path):
    # WHY: request.state.user is set once in middleware and the nav reads it; a
    # logged-out visitor is offered "Sign in", a logged-in one sees their name
    # and a sign-out control.
    app = create_app(tmp_path / "nav.db")
    with TestClient(app) as client:
        _user(client, name="Kevin", password="secret")

        anon = client.get("/")
        assert "Sign in" in anon.text
        assert "Sign out" not in anon.text

        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        home = client.get("/")
        assert "Kevin" in home.text
        assert "Sign out" in home.text


def test_login_rotates_session_and_kills_the_prior_one(tmp_path):
    # WHY: the session is rotated at the auth boundary. Logging in a second time
    # must invalidate the session the browser already held server-side, not just
    # overwrite the cookie — otherwise a prior (possibly planted or shared) cookie
    # value stays valid after login. The new session must of course still work.
    app = create_app(tmp_path / "rotate.db")
    with TestClient(app) as client:
        _user(client, password="secret")
        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        first = client.cookies.get(config.SESSION_COOKIE)

        # A second login (e.g. re-auth) while still carrying the first cookie.
        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        second = client.cookies.get(config.SESSION_COOKIE)
        assert second != first  # a fresh value, not the old one re-set

        c = db.connect(tmp_path / "rotate.db")
        # The old session is dead server-side; only the new one resolves.
        assert sessions.resolve_session(c, first) is None
        assert sessions.resolve_session(c, second) is not None
        # Exactly one live session row remains — no orphan left behind.
        assert c.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1
        c.close()


def test_cross_site_login_post_is_blocked(tmp_path):
    # WHY: /login has no session yet, so it can't use the session-bound CSRF token
    # every other mutating route does. A cross-site POST (a page on evil.example
    # auto-submitting the attacker's own credentials) would silently log the victim
    # into an attacker-controlled account — a login-CSRF. A browser always stamps
    # Origin on a cross-origin POST, so we reject when it names a different host.
    app = create_app(tmp_path / "login_csrf.db")
    with TestClient(app) as client:
        _user(client, password="secret")
        r = client.post(
            "/login",
            data={"email": "kevin@example.com", "password": "secret"},
            headers={"Origin": "https://evil.example"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "Cross-site login blocked" in r.text
        # No session was minted despite the correct password.
        assert config.SESSION_COOKIE not in r.cookies


def test_same_origin_login_post_is_allowed(tmp_path):
    # WHY: the Origin guard must not break a legitimate same-origin form POST — a
    # browser submitting the login form from Athena's own page stamps our own host
    # in Origin, and that must sail through to a normal login.
    app = create_app(tmp_path / "login_same_origin.db")
    with TestClient(app) as client:
        _user(client, password="secret")
        r = client.post(
            "/login",
            data={"email": "kevin@example.com", "password": "secret"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert config.SESSION_COOKIE in r.cookies


def test_login_post_without_origin_is_allowed(tmp_path):
    # WHY: non-browser clients (curl, scripts) omit Origin entirely, and there is no
    # CSRF risk without a browser — so a missing Origin is treated as same-site and
    # the login proceeds. (This is the path the rest of the suite exercises.)
    app = create_app(tmp_path / "login_no_origin.db")
    with TestClient(app) as client:
        _user(client, password="secret")
        r = client.post(
            "/login",
            data={"email": "kevin@example.com", "password": "secret"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert config.SESSION_COOKIE in r.cookies


def test_switching_accounts_invalidates_the_first_account_session(tmp_path):
    # WHY: rotation is per-login, not per-user. Logging in as a SECOND user from
    # the same browser must kill the first user's session — the cookie a shared
    # machine carried for account A cannot keep resolving once B has signed in.
    app = create_app(tmp_path / "switch.db")
    # Seed both users straight into the DB: /users is bootstrap-gated (only the
    # first user can be created anonymously), and this test needs two login-capable
    # accounts without that ceremony.
    with TestClient(app):
        pass
    seed = db.connect(tmp_path / "switch.db")
    users.create_user(seed, email="a@e.com", name="A", password="pwA")
    users.create_user(seed, email="b@e.com", name="B", password="pwB")
    seed.close()

    with TestClient(app) as client:
        client.post("/login", data={"email": "a@e.com", "password": "pwA"})
        a_cookie = client.cookies.get(config.SESSION_COOKIE)

        client.post("/login", data={"email": "b@e.com", "password": "pwB"})
        b_cookie = client.cookies.get(config.SESSION_COOKIE)

        c = db.connect(tmp_path / "switch.db")
        assert sessions.resolve_session(c, a_cookie) is None      # A's session gone
        assert sessions.resolve_session(c, b_cookie)["email"] == "b@e.com"
        c.close()
