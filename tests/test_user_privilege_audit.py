"""User privilege changes (role, agent flag) are now audited atomically.

Promoting a user to admin or toggling the agent flag were bare UPDATEs with NO
activity event — an admin (or an admin-scoped agent) could escalate a user's
privileges with zero trace. These tests pin that role/agent changes record a
'changed_role' / 'marked_agent' event in the SAME transaction as the change, once
per real change, with the last-admin guard and the admin-only gate preserved.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db, user_commands, users
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="users.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    r = client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    assert r.status_code == 201, r.text
    return r.json()  # admin, id 1


def _make_user(client, email, *, role="member"):
    r = client.post(
        "/users",
        json={"email": email, "name": email, "password": "pw", "role": role},
        headers=H_ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _events(db_file, *verbs):
    conn = db.connect(db_file)
    return [e for e in activity.list_activity(conn, limit=200) if e["verb"] in verbs]


# --- REST ------------------------------------------------------------------


def test_rest_role_change_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        u = _make_user(c, "m@e.com", role="member")
        r = c.put(f"/users/{u['id']}/role", json={"role": "admin"}, headers=H_ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "admin"

    ev = _events(db_file, "changed_role")
    assert len(ev) == 1
    assert ev[0]["target_kind"] == "user" and ev[0]["target_id"] == u["id"]
    assert ev[0]["actor_id"] == 1
    assert ev[0]["detail"] == "member → admin"


def test_rest_role_noop_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        u = _make_user(c, "m@e.com", role="member")
        assert (
            c.put(
                f"/users/{u['id']}/role", json={"role": "member"}, headers=H_ADMIN
            ).status_code
            == 200
        )
    assert _events(db_file, "changed_role") == []


def test_rest_agent_flag_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        u = _make_user(c, "m@e.com")
        assert (
            c.put(
                f"/users/{u['id']}/agent", json={"is_agent": True}, headers=H_ADMIN
            ).status_code
            == 200
        )
        # Re-marking is a no-op — records nothing the second time.
        assert (
            c.put(
                f"/users/{u['id']}/agent", json={"is_agent": True}, headers=H_ADMIN
            ).status_code
            == 200
        )
        assert (
            c.put(
                f"/users/{u['id']}/agent", json={"is_agent": False}, headers=H_ADMIN
            ).status_code
            == 200
        )
    assert len(_events(db_file, "marked_agent")) == 1
    assert len(_events(db_file, "unmarked_agent")) == 1


def test_last_admin_guard_rejects_and_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)  # the only admin, id 1
        assert (
            c.put("/users/1/role", json={"role": "member"}, headers=H_ADMIN).status_code
            == 409
        )
    assert _events(db_file, "changed_role") == []
    assert users.get_user(db.connect(db_file), 1)["role"] == "admin"


def test_unknown_role_422_and_unknown_user_404(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        u = _make_user(c, "m@e.com")
        assert (
            c.put(
                f"/users/{u['id']}/role", json={"role": "wizard"}, headers=H_ADMIN
            ).status_code
            == 422
        )
        assert (
            c.put(
                "/users/999/role", json={"role": "admin"}, headers=H_ADMIN
            ).status_code
            == 404
        )
    assert _events(db_file, "changed_role") == []


def test_role_change_requires_admin(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = _make_user(c, "m@e.com", role="member")
        target = _make_user(c, "t@e.com", role="member")
        r = c.put(
            f"/users/{target['id']}/role",
            json={"role": "admin"},
            headers={"X-Athena-Actor": str(member["id"])},
        )
        assert r.status_code == 403
    assert _events(db_file, "changed_role") == []


# --- web -------------------------------------------------------------------


def test_web_role_change_audited_and_last_admin_guarded(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)  # a@e.com / pw, the only admin (id 1)
        u = _make_user(c, "m@e.com", role="member")
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")

        # Demoting the last admin (id 1, still the only one) re-renders at 409.
        assert (
            c.post(
                "/admin/users/1/role", data={"role": "member"}, follow_redirects=False
            ).status_code
            == 409
        )
        # Promote the member -> 303 redirect, and it is audited.
        assert (
            c.post(
                f"/admin/users/{u['id']}/role",
                data={"role": "admin"},
                follow_redirects=False,
            ).status_code
            == 303
        )

    ev = _events(db_file, "changed_role")
    assert len(ev) == 1 and ev[0]["detail"] == "member → admin"


def test_web_agent_toggle_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        u = _make_user(c, "m@e.com")
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        assert (
            c.post(
                f"/admin/users/{u['id']}/agent",
                data={"is_agent": "1"},
                follow_redirects=False,
            ).status_code
            == 303
        )
    assert len(_events(db_file, "marked_agent")) == 1


# --- command atomicity -----------------------------------------------------


def test_command_rejects_last_admin_atomically(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    try:
        user_commands.set_user_role(conn, actor_id=1, target_user_id=1, role="member")
        raise AssertionError("expected UserCommandError")
    except user_commands.UserCommandError as exc:
        assert exc.status_code == 409
    # Neither the role nor an event changed — the rejection left no trace.
    assert users.get_user(conn, 1)["role"] == "admin"
    assert [
        e for e in activity.list_activity(conn, limit=50) if e["verb"] == "changed_role"
    ] == []
