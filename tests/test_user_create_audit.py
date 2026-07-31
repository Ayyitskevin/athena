"""Creating a user — a new actor entering the system — is now audited atomically.

A new user is a new principal: someone (or some agent) who can hold tokens, own
issues, and act. Creating one was a bare INSERT with NO activity event, so a fresh
account — including a fresh ADMIN — could appear with zero trace. These tests pin that
every user-facing create records a 'created_user' event in the SAME transaction as the
insert: attributed to the acting admin when an admin adds a user, and SELF-attributed
on the credentialed bootstrap of the first user and on an SSO first-login. The
internal 'Automation' system actor is deliberately NOT audited (it is plumbing, not a
person joining), and the password hash never reaches the detail.
"""

from fastapi.testclient import TestClient

from athena.aegis import automation
from athena.core import activity, db, oidc_flow, user_commands, users
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="user_create.db"):
    return create_app(tmp_path / name), tmp_path / name


def _events(db_file, *verbs):
    conn = db.connect(db_file)
    return [e for e in activity.list_activity(conn, limit=200) if e["verb"] in verbs]


# --- REST ------------------------------------------------------------------


def test_bootstrap_first_user_is_self_attributed(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        # The suite fixture supplies the bootstrap transport grant; this test owns
        # attribution and atomicity, while test_bootstrap_auth owns authentication.
        r = c.post(
            "/users", json={"email": "boss@e.com", "name": "Boss", "password": "pw"}
        )
        assert r.status_code == 201, r.text
        uid = r.json()["id"]

    ev = _events(db_file, "created_user")
    assert len(ev) == 1
    # Self-attributed: no other actor exists, so the new user is both actor and target.
    assert ev[0]["actor_id"] == uid
    assert ev[0]["target_kind"] == "user" and ev[0]["target_id"] == uid
    assert "boss@e.com" in ev[0]["detail"] and "admin" in ev[0]["detail"]


def test_admin_created_user_is_attributed_to_the_admin(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post(
            "/users", json={"email": "a@e.com", "name": "A", "password": "pw"}
        )  # admin, id 1
        r = c.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "role": "member"},
            headers=H_ADMIN,
        )
        assert r.status_code == 201, r.text
        member_id = r.json()["id"]

    ev = _events(db_file, "created_user")
    # One for the bootstrap admin (self), one for the member (by the admin).
    assert len(ev) == 2
    member_ev = [e for e in ev if e["target_id"] == member_id]
    assert len(member_ev) == 1
    assert member_ev[0]["actor_id"] == 1  # the acting admin
    assert "m@e.com" in member_ev[0]["detail"] and "member" in member_ev[0]["detail"]


def test_duplicate_email_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post(
            "/users", json={"email": "a@e.com", "name": "A", "password": "pw"}
        )  # id 1
        # Re-using the email is a 400 and must leave no created_user event behind.
        dup = c.post(
            "/users", json={"email": "a@e.com", "name": "Dup"}, headers=H_ADMIN
        )
        assert dup.status_code == 400
    # Only the bootstrap admin's create was recorded.
    assert len(_events(db_file, "created_user")) == 1


def test_created_user_detail_never_contains_the_password(tmp_path):
    app, db_file = _app(tmp_path)
    secret = "hunter2-super-secret"
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "A", "password": secret})
    conn = db.connect(db_file)
    hits = conn.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE detail LIKE ?", (f"%{secret}%",)
    ).fetchone()["n"]
    assert hits == 0


# --- web -------------------------------------------------------------------


def test_web_admin_create_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        created = c.post(
            "/admin/users",
            data={
                "email": "new@e.com",
                "name": "New",
                "password": "pw",
                "role": "viewer",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303, created.text

    ev = [e for e in _events(db_file, "created_user") if "new@e.com" in e["detail"]]
    assert len(ev) == 1 and ev[0]["actor_id"] == 1 and "viewer" in ev[0]["detail"]


# --- SSO provision ---------------------------------------------------------


def test_sso_first_login_provision_is_self_attributed(tmp_path):
    conn = db.connect(tmp_path / "sso.db")
    db.migrate(conn)
    user_commands.bootstrap_user(
        conn,
        email="bootstrap-admin@local.test",
        name="Bootstrap Admin",
        password="test-password",
    )
    claims = {
        "sub": "s1",
        "email": "new@acme.com",
        "email_verified": True,
        "name": "New",
    }
    user = oidc_flow.provision_or_link(
        conn, issuer="https://idp.example.com", claims=claims
    )

    ev = [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "created_user" and e["target_id"] == user["id"]
    ]
    assert len(ev) == 1
    # The person signing in provisions their OWN account — actor and target are the user.
    assert ev[0]["actor_id"] == user["id"] and ev[0]["target_id"] == user["id"]
    conn.close()


# --- exclusion: the internal automation system actor ------------------------


def test_automation_system_actor_creation_is_not_audited(tmp_path):
    # The 'Automation' actor is internal plumbing get-or-created on first use, not a
    # person joining the system — creating it must NOT emit a created_user event.
    conn = db.connect(tmp_path / "auto.db")
    db.migrate(conn)
    aid = automation.system_actor_id(conn)
    assert users.get_user(conn, aid) is not None  # it WAS created
    assert [
        e for e in activity.list_activity(conn, limit=50) if e["verb"] == "created_user"
    ] == []  # but not audited
    conn.close()


# --- command atomicity -----------------------------------------------------


def test_command_create_records_event_in_same_transaction(tmp_path):
    conn = db.connect(tmp_path / "cmd.db")
    db.migrate(conn)
    user = user_commands.create_user(
        conn, actor_id=None, email="x@e.com", name="X", role="member"
    )
    assert users.get_user(conn, user["id"]) is not None
    ev = [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "created_user" and e["target_id"] == user["id"]
    ]
    assert len(ev) == 1
    conn.close()
