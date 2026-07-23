"""Linking and unlinking an SSO identity are now audited atomically.

Binding an external OpenID Connect identity to an account grants a new way to log in
as that user; unlinking one removes it. Both were bare writes with NO activity event —
an account's set of sign-in methods could change with zero trace. These tests pin that
a first-login link records a 'linked_identity' event and a settings unlink records an
'unlinked_identity' event, in the SAME transaction as the change, attributed to (and
recorded against) the user whose account it is, with the self-service authz preserved.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db, oidc, oidc_commands, sessions, users
from athena.main import create_app

ISS = "https://idp.example.com"


# --- SSO first-login link (through provision_or_link) -----------------------


def _conn(tmp_path, name="oidc_audit.db"):
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    return conn


def _events(conn, *verbs):
    return [e for e in activity.list_activity(conn, limit=200) if e["verb"] in verbs]


def test_first_login_link_is_audited_and_self_attributed(tmp_path):
    from athena.core import oidc_flow

    conn = _conn(tmp_path)
    claims = {
        "sub": "s1",
        "email": "new@acme.com",
        "email_verified": True,
        "name": "New",
    }
    user = oidc_flow.provision_or_link(conn, issuer=ISS, claims=claims)

    ev = _events(conn, "linked_identity")
    assert len(ev) == 1
    # Self-attributed: the user who signed in IS the actor and the target.
    assert ev[0]["actor_id"] == user["id"]
    assert ev[0]["target_kind"] == "user" and ev[0]["target_id"] == user["id"]
    assert ISS in ev[0]["detail"] and "s1" in ev[0]["detail"]

    # Idempotent: a second login resolves by sub with NO new link and NO new event.
    oidc_flow.provision_or_link(conn, issuer=ISS, claims=claims)
    assert len(_events(conn, "linked_identity")) == 1
    conn.close()


def test_command_link_records_event_in_same_transaction(tmp_path):
    conn = _conn(tmp_path)
    uid = users.create_user(conn, email="a@e.com", name="A", password="pw")["id"]
    link = oidc_commands.link_identity(
        conn, actor_id=uid, issuer=ISS, subject="sub-1", user_id=uid
    )
    assert link["user_id"] == uid
    # Both the link row and its provenance event are visible together.
    assert oidc.get_identity(conn, issuer=ISS, subject="sub-1") is not None
    assert len(_events(conn, "linked_identity")) == 1
    conn.close()


def test_command_unlink_noop_records_nothing(tmp_path):
    conn = _conn(tmp_path)
    uid = users.create_user(conn, email="a@e.com", name="A", password="pw")["id"]
    # Nothing linked → nothing removed → no event.
    assert (
        oidc_commands.unlink_identity(conn, actor_id=uid, issuer=ISS, subject="ghost")
        is False
    )
    assert _events(conn, "unlinked_identity") == []
    conn.close()


# --- settings unlink (web) --------------------------------------------------


def _login_as(client, db_file, user_id):
    conn = db.connect(db_file)
    try:
        raw = sessions.create_session(conn, user_id)
        csrf = sessions.csrf_token_for(conn, raw)
    finally:
        conn.close()
    client.cookies.set("athena_session", raw)
    client.headers["X-CSRF-Token"] = csrf


def test_web_unlink_is_audited_and_self_attributed(tmp_path):
    db_file = tmp_path / "web_unlink.db"
    with TestClient(create_app(db_file)) as client:
        conn = db.connect(db_file)
        uid = users.create_user(conn, email="a@e.com", name="A", password="pw")["id"]
        oidc.link_identity(conn, issuer=ISS, subject="s1", user_id=uid)
        conn.close()
        _login_as(client, db_file, uid)

        done = client.post(
            "/settings/identities/unlink",
            data={"issuer": ISS, "subject": "s1"},
            follow_redirects=False,
        )
        assert done.status_code == 303

    conn = db.connect(db_file)
    ev = [
        e
        for e in activity.list_activity(conn, limit=200)
        if e["verb"] == "unlinked_identity"
    ]
    assert len(ev) == 1
    assert ev[0]["actor_id"] == uid and ev[0]["target_id"] == uid
    assert ISS in ev[0]["detail"]
    conn.close()


def test_web_unlink_of_another_users_identity_records_nothing(tmp_path):
    # The authz boundary: a user may only unlink their OWN links. A cross-user attempt
    # is a 404 that leaves the victim's link intact AND records no event.
    db_file = tmp_path / "web_authz.db"
    with TestClient(create_app(db_file)) as client:
        conn = db.connect(db_file)
        attacker = users.create_user(
            conn, email="att@e.com", name="Att", password="pw"
        )["id"]
        victim = users.create_user(conn, email="vic@e.com", name="Vic", password="pw")[
            "id"
        ]
        oidc.link_identity(conn, issuer=ISS, subject="victim-sub", user_id=victim)
        conn.close()
        _login_as(client, db_file, attacker)

        denied = client.post(
            "/settings/identities/unlink",
            data={"issuer": ISS, "subject": "victim-sub"},
            follow_redirects=False,
        )
        assert denied.status_code == 404

    conn = db.connect(db_file)
    assert (
        oidc.find_user_by_identity(conn, issuer=ISS, subject="victim-sub") is not None
    )
    assert [
        e
        for e in activity.list_activity(conn, limit=200)
        if e["verb"] == "unlinked_identity"
    ] == []
    conn.close()
