"""The automation-rules REST API (slice 3).

Managing rules is an operator action — a rule fires across the whole instance and writes
as a privileged system actor — so every route is admin-gated, like webhooks. The boundary
validates the rule spec before persisting, so a typo'd verb/condition key or a malformed
action is a 422, never a row that can't ever fire. The final test ties the API to the
engine: a rule created over HTTP actually fires when its event lands.
"""

from fastapi.testclient import TestClient

from athena.aegis import automation, issues
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # user 1 — first user, admin
H2 = {"X-Athena-Actor": "2"}  # user 2 — a non-admin member


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "api.db"))


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


def test_create_list_show_rule(tmp_path):
    with _client(tmp_path) as client:
        _two_users(client)
        created = client.post("/automation/rules", json=_valid_rule(), headers=H1)
        assert created.status_code == 201
        rule = created.json()
        assert rule["name"] == "triage" and rule["enabled"] is True
        assert rule["trigger_verb"] == "created" and rule["action_type"] == "comment"
        assert rule["action_params"] == {"body": "thanks for filing"}
        assert rule["created_by"] == 1

        listed = client.get("/automation/rules", headers=H1).json()
        assert [r["id"] for r in listed] == [rule["id"]]

        shown = client.get(f"/automation/rules/{rule['id']}", headers=H1)
        assert shown.status_code == 200 and shown.json()["name"] == "triage"


def test_create_rejects_malformed_specs(tmp_path):
    with _client(tmp_path) as client:
        _two_users(client)

        def post(**overrides):
            payload = _valid_rule() | overrides
            return client.post("/automation/rules", json=payload, headers=H1)

        assert post(name="   ").status_code == 422  # blank name
        assert post(trigger_verb="exploded").status_code == 422  # not a real verb
        assert post(action_type="delete_everything").status_code == 422  # not an action
        assert post(conditions={"colour": "blue"}).status_code == 422  # typo'd cond key
        # comment with no body would be a silent no-op at fire time — reject up front.
        assert post(action_params={}).status_code == 422
        # assign needs an integer user_id, not a string.
        assert (
            post(action_type="assign", action_params={"user_id": "two"}).status_code
            == 422
        )
        # target_kind other than issue isn't supported yet.
        assert post(target_kind="page").status_code == 422
        # None of the rejects persisted a row.
        assert client.get("/automation/rules", headers=H1).json() == []


def test_pause_resume_and_delete(tmp_path):
    with _client(tmp_path) as client:
        _two_users(client)
        rid = client.post("/automation/rules", json=_valid_rule(), headers=H1).json()[
            "id"
        ]

        paused = client.patch(
            f"/automation/rules/{rid}", json={"enabled": False}, headers=H1
        )
        assert paused.status_code == 200 and paused.json()["enabled"] is False
        resumed = client.patch(
            f"/automation/rules/{rid}", json={"enabled": True}, headers=H1
        )
        assert resumed.json()["enabled"] is True

        assert client.delete(f"/automation/rules/{rid}", headers=H1).status_code == 204
        assert client.get(f"/automation/rules/{rid}", headers=H1).status_code == 404


def test_unknown_rule_is_404(tmp_path):
    with _client(tmp_path) as client:
        _two_users(client)
        assert client.get("/automation/rules/999", headers=H1).status_code == 404
        assert (
            client.patch(
                "/automation/rules/999", json={"enabled": False}, headers=H1
            ).status_code
            == 404
        )
        assert client.delete("/automation/rules/999", headers=H1).status_code == 404


def test_only_admins_may_manage_rules(tmp_path):
    with _client(tmp_path) as client:
        _two_users(client)  # user 2 is a non-admin member
        # A non-admin is forbidden from every route.
        assert client.get("/automation/rules", headers=H2).status_code == 403
        assert (
            client.post("/automation/rules", json=_valid_rule(), headers=H2).status_code
            == 403
        )
        # An unauthenticated request is unauthorized, not forbidden.
        assert client.post("/automation/rules", json=_valid_rule()).status_code == 401


def test_rule_created_over_http_fires_in_the_engine(tmp_path):
    db_file = tmp_path / "api.db"
    with TestClient(create_app(db_file)) as client:
        _two_users(client)
        pid = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H1
        ).json()["id"]
        # An admin wires up "when an issue is created in P, assign it to user 2".
        body = {
            "name": "auto-assign",
            "trigger_verb": "created",
            "action_type": "assign",
            "conditions": {"project_id": pid},
            "action_params": {"user_id": 2},
        }
        assert (
            client.post("/automation/rules", json=body, headers=H1).status_code == 201
        )
        iid = client.post(
            "/issues", json={"title": "x", "project_id": pid}, headers=H1
        ).json()["id"]

        # The engine (driven directly — the live loop is off in tests) applies it.
        assert automation.run_pass(db_file) == 1
        assert issues.get_issue(db.connect(db_file), iid)["assignee_id"] == 2
