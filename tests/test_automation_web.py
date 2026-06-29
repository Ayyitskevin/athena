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
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _rule_count(db_file):
    conn = db.connect(db_file)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM automation_rules").fetchone()["n"]
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
            f"/admin/automation/{rid}/enabled", data={"enabled": "0"}, follow_redirects=False
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
            json={"name": "r", "trigger_verb": "created", "action_type": "comment",
                  "action_params": {"body": "hi"}},
            headers=H1,
        ).json()["id"]

        _login(client, "m@e.com", "pw")  # a valid member session + CSRF token
        denied = client.get("/admin/automation")
        assert denied.status_code == 403 and "Admin role required" in denied.text
        # Every mutating route refuses a member, and the rule is left untouched.
        assert client.post(
            "/admin/automation",
            data={"name": "x", "trigger_verb": "created", "action_type": "comment", "action_body": "y"},
            follow_redirects=False,
        ).status_code == 403
        assert client.post(
            f"/admin/automation/{rid}/enabled", data={"enabled": "0"}, follow_redirects=False
        ).status_code == 403
        assert client.post(
            f"/admin/automation/{rid}/delete", follow_redirects=False
        ).status_code == 403
        assert _rule_count(db_file) == 1
        assert automation.get_rule(db.connect(db_file), rid)["enabled"] is True


def test_csrf_required_on_mutating_routes(tmp_path):
    db_file = tmp_path / "csrf.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        rid = client.post(
            "/automation/rules",
            json={"name": "r", "trigger_verb": "created", "action_type": "comment",
                  "action_params": {"body": "hi"}},
            headers=H1,
        ).json()["id"]
        _login(client)
        client.headers.pop("X-CSRF-Token", None)  # drop the token the helper set

        assert client.post(
            "/admin/automation",
            data={"name": "x", "trigger_verb": "created", "action_type": "comment", "action_body": "y"},
            follow_redirects=False,
        ).status_code == 403
        assert client.post(
            f"/admin/automation/{rid}/enabled", data={"enabled": "0"}, follow_redirects=False
        ).status_code == 403
        assert client.post(
            f"/admin/automation/{rid}/delete", follow_redirects=False
        ).status_code == 403
        assert _rule_count(db_file) == 1  # nothing changed


def test_rule_built_in_the_ui_actually_fires(tmp_path):
    db_file = tmp_path / "fires.db"
    with TestClient(create_app(db_file)) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        client.post("/users", json={"email": "b@e.com", "name": "Bob", "password": "pw"}, headers=H1)
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H1).json()["id"]
        _login(client)
        # Build "when an issue is created in P, assign it to user 2" entirely through the form.
        assert client.post(
            "/admin/automation",
            data={
                "name": "auto-assign",
                "trigger_verb": "created",
                "condition_project": str(pid),
                "action_type": "assign",
                "action_user_id": "2",
            },
            follow_redirects=False,
        ).status_code == 303

        iid = client.post("/issues", json={"title": "x", "project_id": pid}, headers=H1).json()["id"]
        assert automation.run_pass(db_file) == 1
        assert issues.get_issue(db.connect(db_file), iid)["assignee_id"] == 2
