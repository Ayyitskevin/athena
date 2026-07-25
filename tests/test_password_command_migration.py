"""Password writes are audited-atomic commands.

Both password paths ran the hash write and the session revocation as SEPARATE
commits with NO audit event on any transport: a crash between them left a rotated
password with live sessions, and an admin could reset any account — handing
themselves that account, since every later write is attributed to the target —
with nothing on the append-only trail. The admin reset also existed only in the
browser, so the one privilege lever with no audit was also the one with no REST
surface.

user_commands.change_own_password / reset_user_password now fold the hash write,
the session revocation that rotation forces, and the audit event into one
transaction, with authorization resolved inside the command. These pin
attribution, atomicity, session revocation, secret hygiene (no password or hash
on the trail), authorization, and REST/browser parity.
"""

from fastapi.testclient import TestClient

from athena.core import db, user_commands, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="password_cmd.db"):
    return create_app(tmp_path / name), tmp_path / name


def _admin(client, email="admin@e.com", password="adminpw"):
    return client.post(
        "/users", json={"email": email, "name": "Admin", "password": password}
    ).json()


def _member(client, email="mem@e.com", password="memberpw"):
    return client.post(
        "/users",
        json={"email": email, "name": "Mem", "password": password, "role": "member"},
        headers=H1,
    ).json()


def _login(client, email, password):
    r = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, target_id, detail FROM activity WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _session_count(db_file, user_id):
    conn = db.connect(db_file)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (user_id,)
    ).fetchone()["n"]
    conn.close()
    return n


def _password_hash(db_file, user_id):
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT password_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["password_hash"]


def test_admin_reset_is_attributed_and_revokes_every_session(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _admin(c)
        member = _member(c)
        # The member signs in on two devices; both cookies must die with the reset.
        for _ in range(2):
            other = TestClient(app)
            other.post(
                "/login",
                data={"email": "mem@e.com", "password": "memberpw"},
                follow_redirects=False,
            )
        assert _session_count(db_file, member["id"]) == 2

        _login(c, "admin@e.com", "adminpw")
        assert (
            c.post(
                f"/admin/users/{member['id']}/password",
                data={"password": "rotated"},
                follow_redirects=False,
            ).status_code
            == 303
        )

    assert _session_count(db_file, member["id"]) == 0
    reset = _events(db_file, "password_reset")
    # Attributed to the acting ADMIN, targeting the member — the fact the trail
    # could not answer before.
    assert [(e["actor_id"], e["target_id"]) for e in reset] == [(1, member["id"])]
    assert reset[0]["detail"] == "2 session(s) revoked"


def test_self_change_is_attributed_and_keeps_only_this_session(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _admin(c)
        member = _member(c)
        stale = TestClient(app)
        stale.post(
            "/login",
            data={"email": "mem@e.com", "password": "memberpw"},
            follow_redirects=False,
        )
        here = TestClient(app)
        _login(here, "mem@e.com", "memberpw")
        assert _session_count(db_file, member["id"]) == 2

        assert (
            here.post(
                "/settings/password",
                data={
                    "current_password": "memberpw",
                    "new_password": "rotated",
                    "confirm_password": "rotated",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        # This browser stays signed in; the other device is gone.
        assert here.get("/settings/password").status_code == 200
        assert stale.get("/settings/password").status_code == 401

    assert _session_count(db_file, member["id"]) == 1
    changed = _events(db_file, "password_changed")
    # Self-attributed: actor and target are the same account.
    assert [(e["actor_id"], e["target_id"]) for e in changed] == [
        (member["id"], member["id"])
    ]
    assert changed[0]["detail"] == "1 other session(s) revoked"


def test_no_password_or_hash_ever_reaches_the_trail(tmp_path):
    app, db_file = _app(tmp_path)
    secret = "sup3r-secret-value"
    with TestClient(app) as c:
        _admin(c)
        member = _member(c)
        _login(c, "admin@e.com", "adminpw")
        c.post(
            f"/admin/users/{member['id']}/password",
            data={"password": secret},
            follow_redirects=False,
        )
    stored = _password_hash(db_file, member["id"])
    conn = db.connect(db_file)
    details = [
        r["detail"] for r in conn.execute("SELECT detail FROM activity").fetchall()
    ]
    conn.close()
    blob = " ".join(d or "" for d in details)
    assert secret not in blob
    assert stored not in blob


def test_reset_rolls_back_the_hash_and_sessions_when_audit_fails(tmp_path):
    import athena.core.activity as activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _admin(c)
        member = _member(c)
        other = TestClient(app)
        other.post(
            "/login",
            data={"email": "mem@e.com", "password": "memberpw"},
            follow_redirects=False,
        )
    before_hash = _password_hash(db_file, member["id"])
    conn = db.connect(db_file)
    admin_actor = {"id": 1, "role": "admin", "_token_scopes": None}
    original = activity.record

    def boom(*a, **k):
        raise RuntimeError("audit down")

    activity.record = boom
    try:
        try:
            user_commands.reset_user_password(
                conn, actor=admin_actor, target_user_id=member["id"], password="rotated"
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        activity.record = original
    conn.close()
    # The hash write AND the session revocation rolled back with the failed event.
    # Before the migration the password would have rotated with no trail entry.
    assert _password_hash(db_file, member["id"]) == before_hash
    assert _session_count(db_file, member["id"]) == 1
    assert _events(db_file, "password_reset") == []


def test_reset_requires_admin_role_and_admin_scope(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _admin(c)
        member = _member(c)
        victim = c.post(
            "/users",
            json={"email": "v@e.com", "name": "Vic", "password": "victimpw"},
            headers=H1,
        ).json()

        # A member cannot reset anyone's password over REST...
        as_member = {"X-Athena-Actor": str(member["id"])}
        denied = c.put(
            f"/users/{victim['id']}/password",
            json={"password": "hijacked"},
            headers=as_member,
        )
        assert denied.status_code == 403

        # ...nor through the browser form.
        _login(c, "mem@e.com", "memberpw")
        assert (
            c.post(
                f"/admin/users/{victim['id']}/password",
                data={"password": "hijacked"},
                follow_redirects=False,
            ).status_code
            == 403
        )

        # An admin's bearer token WITHOUT the admin scope is refused too: a scope
        # never widens a role, and this lever hands over an account.
        raw = c.post(
            "/tokens", json={"name": "t", "scopes": ["issue:write"]}, headers=H1
        ).json()["token"]
        scoped = c.put(
            f"/users/{victim['id']}/password",
            json={"password": "hijacked"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert scoped.status_code == 403

    # The victim's password never changed and nothing was recorded.
    conn = db.connect(db_file)
    assert users.verify_credentials(conn, email="v@e.com", password="victimpw")
    conn.close()
    assert _events(db_file, "password_reset") == []


def test_rest_reset_matches_the_browser_command(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _admin(c)
        member = _member(c)
        other = TestClient(app)
        other.post(
            "/login",
            data={"email": "mem@e.com", "password": "memberpw"},
            follow_redirects=False,
        )
        assert _session_count(db_file, member["id"]) == 1

        response = c.put(
            f"/users/{member['id']}/password",
            json={"password": "rotated"},
            headers=H1,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == member["id"]
        # The response never echoes the password or the stored hash.
        assert "password" not in body and "password_hash" not in body

        # A blank password is refused, and an unknown target 404s.
        assert (
            c.put(
                f"/users/{member['id']}/password", json={"password": "  "}, headers=H1
            ).status_code
            == 422
        )
        assert (
            c.put(
                "/users/9999/password", json={"password": "x"}, headers=H1
            ).status_code
            == 404
        )

    # Same effects as the browser path: sessions gone, event attributed to the admin.
    assert _session_count(db_file, member["id"]) == 0
    assert [
        (e["actor_id"], e["target_id"]) for e in _events(db_file, "password_reset")
    ] == [(1, member["id"])]
    conn = db.connect(db_file)
    assert users.verify_credentials(conn, email="mem@e.com", password="rotated")
    conn.close()


def test_self_change_rejects_a_wrong_current_password_without_writing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _admin(c)
        member = _member(c)
        here = TestClient(app)
        _login(here, "mem@e.com", "memberpw")
        before = _password_hash(db_file, member["id"])

        rejected = here.post(
            "/settings/password",
            data={
                "current_password": "wrong",
                "new_password": "rotated",
                "confirm_password": "rotated",
            },
            follow_redirects=False,
        )
        assert rejected.status_code == 400
        assert "Current password is incorrect." in rejected.text

    assert _password_hash(db_file, member["id"]) == before
    assert _session_count(db_file, member["id"]) == 1
    assert _events(db_file, "password_changed") == []
