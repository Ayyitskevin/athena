"""The admin rule-builder web UI (slice 4) — a thin client over the slice-3 API.

The page lists rules and offers a create form whose option sets are the SAME closed
sets the validator enforces, so the form can't suggest a value the boundary rejects. The
mutating routes are admin-gated by an in-handler check (verify_csrf runs first but checks
no role), so a logged-in MEMBER with a valid CSRF token must still be refused — that's the
case a happy-path test wouldn't catch. The last test proves the form isn't cosmetic: a
rule built through it actually fires in the engine.
"""

from fastapi.testclient import TestClient

from athena.aegis import automation, issues
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # user 1 — admin


def _login(client, email="a@e.com", password="pw"):
    client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _rule_count(db_file):
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM automation_rules").fetchone()[
            "n"
        ]
    finally:
        conn.close()


def test_admin_can_create_toggle_and_delete_a_rule(tmp_path):
    db_file = tmp_path / "web.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)

        page = client.get("/admin/automation")
        assert page.status_code == 200 and "New rule" in page.text

        created = client.post(
            "/admin/automation",
            data={
                "name": "auto-triage",
                "trigger_verb": "created",
                "action_type": "comment",
                "action_body": "thanks for filing",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        rules = automation.list_rules(db.connect(db_file))
        assert len(rules) == 1 and rules[0]["name"] == "auto-triage"
        rid = rules[0]["id"]

        # The new rule shows on the page (with a human-readable summary, not raw json).
        listing = client.get("/admin/automation")
        assert "auto-triage" in listing.text and "thanks for filing" in listing.text

        # Pause it from the UI.
        toggled = client.post(
            f"/admin/automation/{rid}/enabled",
            data={"enabled": "0"},
            follow_redirects=False,
        )
        assert toggled.status_code == 303
        assert automation.get_rule(db.connect(db_file), rid)["enabled"] is False

        # Delete it from the UI.
        client.post(f"/admin/automation/{rid}/delete", follow_redirects=False)
        assert _rule_count(db_file) == 0


def test_malformed_rule_is_rejected_with_an_error(tmp_path):
    db_file = tmp_path / "bad.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)
        # A comment action with no body would no-op on every fire — the shared validator
        # rejects it, the page re-renders with the error, and nothing is persisted.
        resp = client.post(
            "/admin/automation",
            data={"name": "empty", "trigger_verb": "created", "action_type": "comment"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "non-empty string" in resp.text
        assert _rule_count(db_file) == 0


def test_page_and_mutations_require_admin(tmp_path):
    db_file = tmp_path / "guard.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw", "role": "member"},
            headers=H1,
        )
        # An admin seeds one rule via the API so the member has something to try to mutate.
        rid = client.post(
            "/automation/rules",
            json={
                "name": "r",
                "trigger_verb": "created",
                "action_type": "comment",
                "action_params": {"body": "hi"},
            },
            headers=H1,
        ).json()["id"]

        _login(client, "m@e.com", "pw")  # a valid member session + CSRF token
        denied = client.get("/admin/automation")
        assert denied.status_code == 403 and "Admin role required" in denied.text
        # Every mutating route refuses a member, and the rule is left untouched.
        assert (
            client.post(
                "/admin/automation",
                data={
                    "name": "x",
                    "trigger_verb": "created",
                    "action_type": "comment",
                    "action_body": "y",
                },
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/admin/automation/{rid}/enabled",
                data={"enabled": "0"},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/admin/automation/{rid}/delete", follow_redirects=False
            ).status_code
            == 403
        )
        assert _rule_count(db_file) == 1
        assert automation.get_rule(db.connect(db_file), rid)["enabled"] is True


def test_csrf_required_on_mutating_routes(tmp_path):
    db_file = tmp_path / "csrf.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        rid = client.post(
            "/automation/rules",
            json={
                "name": "r",
                "trigger_verb": "created",
                "action_type": "comment",
                "action_params": {"body": "hi"},
            },
            headers=H1,
        ).json()["id"]
        _login(client)
        client.headers.pop("X-CSRF-Token", None)  # drop the token the helper set

        assert (
            client.post(
                "/admin/automation",
                data={
                    "name": "x",
                    "trigger_verb": "created",
                    "action_type": "comment",
                    "action_body": "y",
                },
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/admin/automation/{rid}/enabled",
                data={"enabled": "0"},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/admin/automation/{rid}/delete", follow_redirects=False
            ).status_code
            == 403
        )
        assert _rule_count(db_file) == 1  # nothing changed


def test_rule_built_in_the_ui_actually_fires(tmp_path):
    db_file = tmp_path / "fires.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post(
            "/users",
            json={"email": "b@e.com", "name": "Bob", "password": "pw"},
            headers=H1,
        )
        pid = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H1
        ).json()["id"]
        _login(client)
        # Build "when an issue is created in P, assign it to user 2" entirely through the form.
        assert (
            client.post(
                "/admin/automation",
                data={
                    "name": "auto-assign",
                    "trigger_verb": "created",
                    "condition_project": str(pid),
                    "action_type": "assign",
                    "action_user_id": "2",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )

        iid = client.post(
            "/issues", json={"title": "x", "project_id": pid}, headers=H1
        ).json()["id"]
        assert automation.run_pass(db_file) == 1
        assert issues.get_issue(db.connect(db_file), iid)["assignee_id"] == 2


def test_admin_can_create_and_render_a_scheduled_rule(tmp_path):
    db_file = tmp_path / "scheduled-web.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        project = client.post(
            "/projects", json={"name": "Delivery", "key": "DEL"}, headers=H1
        ).json()
        sprint = client.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "June close"},
            headers=H1,
        ).json()
        _login(client)

        page = client.get("/admin/automation")
        assert 'name="trigger_type"' in page.text
        assert 'name="schedule_at"' in page.text
        assert 'name="condition_sprint"' in page.text
        assert 'name="condition_inactive_for_seconds"' in page.text

        created = client.post(
            "/admin/automation",
            data={
                "name": "stale-nudge",
                "trigger_type": "schedule",
                "schedule_at": "2099-06-30T23:59:00Z",
                "schedule_every_seconds": "86400",
                "condition_project": str(project["id"]),
                "condition_sprint": str(sprint["id"]),
                "condition_inactive_for_seconds": "604800",
                "action_type": "comment",
                "action_body": "Still working on this?",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

        conn = db.connect(db_file)
        try:
            rules = automation.list_rules(conn)
        finally:
            conn.close()
        assert len(rules) == 1
        assert rules[0]["trigger_type"] == "schedule"
        assert rules[0]["trigger_verb"] == automation.SCHEDULE_TRIGGER_VERB
        assert rules[0]["schedule_at"] == "2099-06-30T23:59:00Z"
        assert rules[0]["schedule_every_seconds"] == 86400
        assert rules[0]["conditions"] == {
            "project_id": project["id"],
            "sprint_id": sprint["id"],
            "inactive_for_seconds": 604800,
        }

        listing = client.get("/admin/automation")
        assert "2099-06-30T23:59:00Z" in listing.text
        assert "every 86400 seconds" in listing.text
        assert "Delivery" in listing.text
        assert "sprint June close" in listing.text
        assert "inactive for 604800 seconds" in listing.text
        assert ">scheduled</span>" in listing.text


def test_schedule_form_rejects_non_utc_and_non_integer_values(tmp_path):
    db_file = tmp_path / "bad-schedule-web.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)
        base = {
            "name": "bad-schedule",
            "trigger_type": "schedule",
            "action_type": "comment",
            "action_body": "hello",
        }

        local_time = client.post(
            "/admin/automation",
            data={**base, "schedule_at": "2099-06-30T23:59:00"},
            follow_redirects=False,
        )
        assert local_time.status_code == 400
        assert "canonical UTC" in local_time.text

        bad_repeat = client.post(
            "/admin/automation",
            data={
                **base,
                "schedule_at": "2099-06-30T23:59:00Z",
                "schedule_every_seconds": "hourly",
            },
            follow_redirects=False,
        )
        assert bad_repeat.status_code == 400
        assert "schedule_every_seconds must be an integer" in bad_repeat.text
        assert _rule_count(db_file) == 0


def test_schedule_health_states_and_progress_are_visible(tmp_path):
    db_file = tmp_path / "schedule-health-web.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)

        conn = db.connect(db_file)
        try:
            common = {
                "trigger_verb": automation.SCHEDULE_TRIGGER_VERB,
                "action_type": "comment",
                "action_params": {"body": "nudge"},
                "created_by": 1,
                "trigger_type": "schedule",
            }
            automation.create_rule(
                conn,
                name="future-schedule",
                schedule_at="2099-01-01T00:00:00Z",
                **common,
            )
            disabled = automation.create_rule(
                conn,
                name="disabled-schedule",
                schedule_at="2099-01-02T00:00:00Z",
                **common,
            )
            automation.set_enabled(conn, disabled["id"], False)
            automation.create_rule(
                conn,
                name="overdue-schedule",
                schedule_at="2000-01-01T00:00:00Z",
                **common,
            )
            failing = automation.create_rule(
                conn,
                name="failing-schedule",
                schedule_at="2099-01-03T00:00:00Z",
                **common,
            )
            conn.execute(
                "UPDATE automation_rules SET last_scheduled_for = ?, "
                "schedule_missed_count = 3, last_schedule_target_count = 12, "
                "last_schedule_overflow_count = 2 WHERE id = ?",
                ("2098-12-31T00:00:00Z", failing["id"]),
            )
            conn.execute(
                "INSERT INTO automation_schedule_firings "
                "(rule_id, scheduled_for, state, last_error) VALUES (?, ?, 'failed', ?)",
                (failing["id"], "2098-12-31T00:00:00Z", "target overflow"),
            )
            conn.execute(
                "INSERT INTO automation_rules "
                "(name, trigger_verb, target_kind, conditions, action_type, "
                "action_params, created_by, trigger_type, schedule_at, "
                "next_scheduled_at) VALUES (?, ?, 'issue', '{}', 'comment', ?, "
                "1, 'schedule', ?, ?)",
                (
                    "malformed-schedule",
                    automation.SCHEDULE_TRIGGER_VERB,
                    '{"body":"nudge"}',
                    "2099-01-01 00:00:00",
                    "2099-01-01 00:00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        listing = client.get("/admin/automation")
        assert listing.status_code == 200
        assert ">scheduled</span>" in listing.text
        assert ">disabled</span>" in listing.text
        assert ">overdue</span>" in listing.text
        assert ">malformed</span>" in listing.text
        assert ">failing</span>" in listing.text
        assert "last slot: 12 target(s)" in listing.text
        assert "catch-up skipped 3 older slot(s)" in listing.text
        assert "2 target(s) over limit; slot failed closed" in listing.text
        assert "canonical UTC" in listing.text


def test_admin_can_create_a_buzz_message_rule_from_the_form(tmp_path):
    # The form's buzz fields fold into action_params exactly like the other
    # actions; blank channel means "assign channel at fire time" and stores no
    # key at all, so a later channel change needs no rule edits.
    db_file = tmp_path / "web-buzz.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)
        created = client.post(
            "/admin/automation",
            data={
                "name": "radio-p0",
                "trigger_verb": "created",
                "action_type": "buzz_message",
                "action_buzz_channel": "",
                "action_buzz_mention": "ab" * 32,
                "action_buzz_note": "New issue filed.",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        rules = automation.list_rules(db.connect(db_file))
        assert len(rules) == 1
        assert rules[0]["action_params"] == {
            "mention": "ab" * 32,
            "note": "New issue filed.",
        }
        listing = client.get("/admin/automation")
        assert "buzz message" in listing.text and "assign channel" in listing.text

        # A malformed channel is rejected at the boundary with the field named.
        rejected = client.post(
            "/admin/automation",
            data={
                "name": "bad",
                "trigger_verb": "created",
                "action_type": "buzz_message",
                "action_buzz_channel": "not-a-uuid",
            },
            follow_redirects=False,
        )
        assert rejected.status_code == 400 and "channel" in rejected.text
