"""Webhook lifecycle changes (register, pause, resume, delete) are now audited.

Registering a webhook makes the server push every event it sees to an outbound URL;
pausing, resuming, or deleting one changes that outbound exposure. All four were bare
writes with NO activity event — an admin (or an admin-scoped agent) could stand up an
exfiltration endpoint, or quietly tear one down, with zero trace. These tests pin that
each lifecycle change records a registered_/paused_/resumed_/deleted_webhook event in
the SAME transaction as the change, once per real change, that the one-time signing
SECRET never leaks into the audit detail, and that the admin-only gate is preserved.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db, webhook_commands, webhooks
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
HOOK = "https://93.184.216.34/hook"


def _app(tmp_path, name="webhooks.db"):
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


def test_rest_register_is_audited_and_secret_never_logged(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post("/webhooks", json={"url": HOOK}, headers=H1)
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
        secret = r.json()["secret"]

    ev = _events(db_file, "registered_webhook")
    assert len(ev) == 1
    assert ev[0]["target_kind"] == "webhook" and ev[0]["target_id"] == wid
    assert ev[0]["actor_id"] == 1
    assert HOOK in ev[0]["detail"]
    # The signing secret is a credential — it must appear in NO activity row.
    assert secret not in ev[0]["detail"]
    conn = db.connect(db_file)
    hits = conn.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE detail LIKE ?", (f"%{secret}%",)
    ).fetchone()["n"]
    assert hits == 0


def test_rest_pause_and_resume_are_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        wid = c.post("/webhooks", json={"url": HOOK}, headers=H1).json()["id"]
        # Pause, then resume.
        assert (
            c.patch(f"/webhooks/{wid}", json={"active": False}, headers=H1).status_code
            == 200
        )
        assert (
            c.patch(f"/webhooks/{wid}", json={"active": True}, headers=H1).status_code
            == 200
        )
        # Re-resuming an already-active webhook is a no-op — records nothing more.
        assert (
            c.patch(f"/webhooks/{wid}", json={"active": True}, headers=H1).status_code
            == 200
        )

    assert len(_events(db_file, "paused_webhook")) == 1
    assert len(_events(db_file, "resumed_webhook")) == 1


def test_rest_delete_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        wid = c.post("/webhooks", json={"url": HOOK}, headers=H1).json()["id"]
        assert c.delete(f"/webhooks/{wid}", headers=H1).status_code == 204
        # Deleting again is a 404 and records nothing the second time.
        assert c.delete(f"/webhooks/{wid}", headers=H1).status_code == 404

    ev = _events(db_file, "deleted_webhook")
    assert len(ev) == 1
    assert ev[0]["target_id"] == wid and HOOK in ev[0]["detail"]


def test_register_requires_admin(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = _make_user(c, "m@e.com", role="member")
        r = c.post(
            "/webhooks",
            json={"url": HOOK},
            headers={"X-Athena-Actor": str(member["id"])},
        )
        assert r.status_code == 403
    assert _events(db_file, "registered_webhook") == []


# --- web -------------------------------------------------------------------


def test_web_register_toggle_delete_are_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)  # a@e.com / pw
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")

        created = c.post("/admin/webhooks", data={"url": HOOK})
        assert created.status_code == 201, created.text
        conn = db.connect(db_file)
        wid = conn.execute("SELECT id FROM webhooks ORDER BY id").fetchone()["id"]
        conn.close()

        assert (
            c.post(
                f"/admin/webhooks/{wid}/active",
                data={"active": "0"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            c.post(f"/admin/webhooks/{wid}/delete", follow_redirects=False).status_code
            == 303
        )

    assert len(_events(db_file, "registered_webhook")) == 1
    assert len(_events(db_file, "paused_webhook")) == 1
    assert len(_events(db_file, "deleted_webhook")) == 1


# --- command atomicity -----------------------------------------------------


def test_command_set_active_unknown_webhook_rejects_and_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    try:
        webhook_commands.set_webhook_active(
            conn, actor_id=1, webhook_id=999, active=False
        )
        raise AssertionError("expected WebhookCommandError")
    except webhook_commands.WebhookCommandError as exc:
        assert exc.kind == "not_found"
    assert [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "paused_webhook"
    ] == []


def test_command_delete_unknown_webhook_returns_false_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    assert webhook_commands.delete_webhook(conn, actor_id=1, webhook_id=999) is False
    assert [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "deleted_webhook"
    ] == []


def test_command_register_records_event_in_same_transaction(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    created = webhook_commands.register_webhook(conn, actor_id=1, url=HOOK)
    # Both the webhook row and its provenance event are visible together.
    assert webhooks.get_webhook(conn, created["id"]) is not None
    ev = [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "registered_webhook" and e["target_id"] == created["id"]
    ]
    assert len(ev) == 1
