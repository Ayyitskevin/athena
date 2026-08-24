"""Automation-rule lifecycle changes (create, enable, disable, delete) are now audited.

An automation rule is the most privileged construct an operator can stand up: it fires
on any matching event and writes as the system Automation actor across every issue, with
no human in the loop. Creating, arming/disarming, or deleting one was a bare write with
NO activity event — so who set up (or tore down) an instance-wide automated writer left
zero trace. These tests pin that each lifecycle change records a created_/enabled_/
disabled_/deleted_automation_rule event in the SAME transaction as the change, once per
real change, with the admin-only gate preserved.
"""

from fastapi.testclient import TestClient

from athena.aegis import automation_commands
from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # user 1 — first user, admin
H2 = {"X-Athena-Actor": "2"}  # user 2 — a non-admin member


def _app(tmp_path, name="rules.db"):
    return create_app(tmp_path / name), tmp_path / name


def _two_users(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "Bob", "password": "pw"}, headers=H1
    )


def _valid_rule():
    return {
        "name": "triage",
        "trigger_verb": "created",
        "action_type": "comment",
        "action_params": {"body": "thanks for filing"},
    }


def _events(db_file, *verbs):
    conn = db.connect(db_file)
    return [e for e in activity.list_activity(conn, limit=200) if e["verb"] in verbs]


# --- REST ------------------------------------------------------------------


def test_rest_create_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        r = c.post("/automation/rules", json=_valid_rule(), headers=H1)
        assert r.status_code == 201, r.text
        rid = r.json()["id"]

    ev = _events(db_file, "created_automation_rule")
    assert len(ev) == 1
    assert ev[0]["target_kind"] == "automation_rule" and ev[0]["target_id"] == rid
    assert ev[0]["actor_id"] == 1
    assert "triage" in ev[0]["detail"]


def test_rest_schedule_create_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        payload = {
            "name": "sprint sweep",
            "trigger_type": "schedule",
            "trigger_verb": "scheduled",
            "schedule_at": "2099-01-02T03:04:05Z",
            "schedule_every_seconds": 3600,
            "action_type": "comment",
            "action_params": {"body": "sweep"},
        }
        response = c.post("/automation/rules", json=payload, headers=H1)
        assert response.status_code == 201, response.text
        rid = response.json()["id"]

    ev = _events(db_file, "created_automation_rule")
    assert len(ev) == 1
    assert ev[0]["target_id"] == rid
    assert ev[0]["detail"] == (
        "sprint sweep (at 2099-01-02T03:04:05Z, every 3600 seconds issue → comment)"
    )


def test_rest_enable_disable_are_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        rid = c.post("/automation/rules", json=_valid_rule(), headers=H1).json()["id"]
        # New rules are enabled; disable, then re-enable.
        assert (
            c.patch(
                f"/automation/rules/{rid}", json={"enabled": False}, headers=H1
            ).status_code
            == 200
        )
        assert (
            c.patch(
                f"/automation/rules/{rid}", json={"enabled": True}, headers=H1
            ).status_code
            == 200
        )
        # Re-enabling an already-enabled rule is a no-op — records nothing more.
        assert (
            c.patch(
                f"/automation/rules/{rid}", json={"enabled": True}, headers=H1
            ).status_code
            == 200
        )

    assert len(_events(db_file, "disabled_automation_rule")) == 1
    assert len(_events(db_file, "enabled_automation_rule")) == 1


def test_rest_delete_is_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        rid = c.post("/automation/rules", json=_valid_rule(), headers=H1).json()["id"]
        assert c.delete(f"/automation/rules/{rid}", headers=H1).status_code == 204
        # Deleting again is a 404 and records nothing the second time.
        assert c.delete(f"/automation/rules/{rid}", headers=H1).status_code == 404

    ev = _events(db_file, "deleted_automation_rule")
    assert len(ev) == 1
    assert ev[0]["target_id"] == rid and "triage" in ev[0]["detail"]


def test_create_requires_admin(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
        r = c.post("/automation/rules", json=_valid_rule(), headers=H2)  # non-admin
        assert r.status_code == 403
    assert _events(db_file, "created_automation_rule") == []


# --- web -------------------------------------------------------------------


def test_web_create_toggle_delete_are_audited(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)  # a@e.com / pw is admin (id 1)
        c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")

        created = c.post(
            "/admin/automation",
            data={
                "name": "web-rule",
                "trigger_verb": "created",
                "action_type": "comment",
                "action_body": "hi",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303, created.text
        conn = db.connect(db_file)
        rid = conn.execute("SELECT id FROM automation_rules ORDER BY id").fetchone()[
            "id"
        ]
        conn.close()

        assert (
            c.post(
                f"/admin/automation/{rid}/enabled",
                data={"enabled": "0"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            c.post(
                f"/admin/automation/{rid}/delete", follow_redirects=False
            ).status_code
            == 303
        )

    assert len(_events(db_file, "created_automation_rule")) == 1
    assert len(_events(db_file, "disabled_automation_rule")) == 1
    assert len(_events(db_file, "deleted_automation_rule")) == 1


# --- command atomicity -----------------------------------------------------


def test_command_set_enabled_unknown_rule_rejects_and_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
    conn = db.connect(db_file)
    try:
        automation_commands.set_rule_enabled(
            conn, actor_id=1, rule_id=999, enabled=False
        )
        raise AssertionError("expected AutomationCommandError")
    except automation_commands.AutomationCommandError as exc:
        assert exc.kind == "not_found"
    assert [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "disabled_automation_rule"
    ] == []


def test_command_delete_unknown_rule_returns_false_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _two_users(c)
    conn = db.connect(db_file)
    assert automation_commands.delete_rule(conn, actor_id=1, rule_id=999) is False
    assert [
        e
        for e in activity.list_activity(conn, limit=50)
        if e["verb"] == "deleted_automation_rule"
    ] == []


def test_command_rejects_malformed_schedule_before_row_or_audit(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _two_users(client)
    conn = db.connect(db_file)
    try:
        automation_commands.create_rule(
            conn,
            actor_id=1,
            name="broken schedule",
            trigger_verb="scheduled",
            trigger_type="schedule",
            schedule_at="not-utc",
            action_type="comment",
            action_params={"body": "nudge"},
        )
        raise AssertionError("expected AutomationCommandError")
    except automation_commands.AutomationCommandError as exc:
        assert exc.kind == "invalid"
        assert "canonical UTC" in str(exc)
    assert (conn.execute("SELECT COUNT(*) AS count FROM automation_rules")).fetchone()[
        "count"
    ] == 0
    assert [
        event
        for event in activity.list_activity(conn, limit=50)
        if event["verb"] == "created_automation_rule"
    ] == []
    assert not conn.in_transaction
    conn.close()
