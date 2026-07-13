"""Automation failure is visible, not silent.

The engine fires a rule's action best-effort (at-most-once, fire-and-forget), so a
raising action must NOT strand the cursor — that stays true. What changed: a failure is
no longer swallowed with a bare `pass`. It is logged AND recorded on the rule
(failure_count / last_error / last_error_at), bringing automation to parity with a
webhook's per-subscriber health so an operator can actually see a misbehaving rule on
/admin and via the API. These tests pin exactly that: the failure is recorded and the
cursor still advances (behavior preserved), and the record surfaces on both boundaries.
"""
import logging

from fastapi.testclient import TestClient

from athena.aegis import automation
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # user 1 — first user, admin


def _boom(_conn, _rule, _event):
    raise ValueError("kaboom")


def test_failing_action_records_failure_and_still_advances_cursor(tmp_path, caplog):
    db_file = tmp_path / "fail.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post("/issues", json={"title": "x"}, headers=H1)  # emits a 'created' event

        conn = db.connect(db_file)
        rule = automation.create_rule(
            conn, name="boom", trigger_verb="created", action_type="comment",
            action_params={"body": "x"}, created_by=1,
        )
        assert rule["failure_count"] == 0 and rule["last_error"] is None

        cursor_before = automation.get_cursor(conn)
        # Inject an executor that raises — the same seam the unit tests use — so the
        # matched rule's action fails.
        with caplog.at_level(logging.WARNING):
            automation.process_pending(conn, executor=_boom)

        # 1) The failure is RECORDED on the rule (no longer swallowed silently).
        failed = automation.get_rule(conn, rule["id"])
        assert failed["failure_count"] == 1
        assert "ValueError: kaboom" in failed["last_error"]
        assert failed["last_error_at"] is not None

        # 2) It is also LOGGED at WARNING (the "zero trace" hole is closed).
        assert "kaboom" in caplog.text

        # 3) Behavior preserved: the cursor still advanced past the event (at-most-once),
        #    so the bad rule can't wedge the loop — a second pass has nothing to process.
        assert automation.get_cursor(conn) > cursor_before
        assert automation.process_pending(conn, executor=_boom) == 0


def test_repeated_failures_accumulate_the_count(tmp_path):
    db_file = tmp_path / "count.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        conn = db.connect(db_file)
        rule = automation.create_rule(
            conn, name="boom", trigger_verb="*", action_type="comment",
            action_params={"body": "x"}, created_by=1,
        )
        # Two matching events, each failing, bump the count to 2 (it is cumulative, like
        # a webhook's failure_count — not reset each pass).
        client.post("/issues", json={"title": "one"}, headers=H1)
        automation.process_pending(conn, executor=_boom)
        client.post("/issues", json={"title": "two"}, headers=H1)
        automation.process_pending(conn, executor=_boom)
        assert automation.get_rule(conn, rule["id"])["failure_count"] == 2


def test_rule_failure_is_exposed_via_api(tmp_path):
    db_file = tmp_path / "api.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post("/issues", json={"title": "x"}, headers=H1)
        created = client.post(
            "/automation/rules",
            json={"name": "boom", "trigger_verb": "created",
                  "action_type": "comment", "action_params": {"body": "x"}},
            headers=H1,
        ).json()
        # A fresh rule reports clean health over the API.
        clean = client.post(
            "/automation/rules",
            json={
                "name": "quiet",
                "trigger_verb": "commented",
                "action_type": "comment",
                "action_params": {"body": "x"},
            },
            headers=H1,
        ).json()

        assert created["failure_count"] == 0
        assert created["last_error"] is None and created["last_error_at"] is None
        assert client.get(
            "/automation/rules?failing_only=true", headers=H1
        ).json() == []

        conn = db.connect(db_file)
        automation.process_pending(conn, executor=_boom)

        listed = client.get("/automation/rules", headers=H1).json()
        failing = client.get(
            "/automation/rules?failing_only=true", headers=H1
        ).json()
        assert [rule["id"] for rule in failing] == [created["id"]]
        row = next(r for r in listed if r["id"] == created["id"])
        assert row["failure_count"] == 1
        assert "ValueError: kaboom" in row["last_error"] and row["last_error_at"]

        assert next(r for r in listed if r["id"] == clean["id"])["failure_count"] == 0


def test_admin_page_shows_rule_failure_badge(tmp_path):
    db_file = tmp_path / "web.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        # Session login for the admin web surface.
        client.post("/login", data={"email": "a@e.com", "password": "pw"},
                    follow_redirects=False)
        client.post("/issues", json={"title": "x"}, headers=H1)
        conn = db.connect(db_file)
        automation.create_rule(
            conn, name="boom", trigger_verb="created", action_type="comment",
            action_params={"body": "x"}, created_by=1,
        )
        # Clean rule: the page shows no failure badge.
        automation.create_rule(
            conn, name="quiet", trigger_verb="commented", action_type="comment",
            action_params={"body": "x"}, created_by=1,
        )

        assert "⚠" not in client.get("/admin/automation").text
        assert "Automation failures" not in client.get("/admin/agents/runs").text

        automation.process_pending(conn, executor=_boom)
        page = client.get("/admin/automation").text
        assert "⚠" in page and "1 failed" in page

        mission_control = client.get("/admin/agents/runs").text
        assert "Automation failures" in mission_control
        assert "boom" in mission_control and "ValueError: kaboom" in mission_control
        assert "quiet" not in mission_control
