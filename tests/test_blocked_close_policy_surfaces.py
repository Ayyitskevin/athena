"""Adversarial transport coverage for blocked-issue close governance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from athena.aegis import automation, issue_commands
from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError


POLICY_CODE = issue_commands.BLOCKED_CLOSE_POLICY_ERROR_CODE
POLICY_DETAIL = issue_commands.BLOCKED_CLOSE_POLICY_ERROR_DETAIL
CUSTOM_DONE = "shipped"


def _actor(user_id: int) -> dict[str, str]:
    return {"X-Athena-Actor": str(user_id)}


@dataclass(frozen=True)
class Scenario:
    db_file: Path
    admin: dict
    agent: dict
    project: dict
    target: dict
    hidden_blocker: dict


def _expect(response, status_code: int) -> None:
    assert response.status_code == status_code, response.text


def _user(
    client: TestClient,
    *,
    email: str,
    name: str,
    actor_id: int | None = None,
    is_agent: bool = False,
    password: str | None = None,
) -> dict:
    payload: dict[str, object] = {
        "email": email,
        "name": name,
        "is_agent": is_agent,
    }
    if password is not None:
        payload["password"] = password
    response = client.post(
        "/users",
        json=payload,
        headers=_actor(actor_id) if actor_id is not None else {},
    )
    _expect(response, 201)
    return response.json()


def _project(client: TestClient, *, actor_id: int, name: str, key: str) -> dict:
    response = client.post(
        "/projects",
        json={"name": name, "key": key},
        headers=_actor(actor_id),
    )
    _expect(response, 201)
    return response.json()


def _issue(
    client: TestClient,
    *,
    actor_id: int,
    title: str,
    project_id: int,
) -> dict:
    response = client.post(
        "/issues",
        json={"title": title, "project_id": project_id},
        headers=_actor(actor_id),
    )
    _expect(response, 201)
    return response.json()


def _enable_policy(client: TestClient, *, actor_id: int, project_id: int) -> None:
    current = client.get(f"/projects/{project_id}", headers=_actor(actor_id))
    _expect(current, 200)
    response = client.put(
        f"/projects/{project_id}/policy",
        json={"block_agent_closes_when_blocked": True},
        headers={**_actor(actor_id), "If-Match": current.headers["ETag"]},
    )
    _expect(response, 200)
    assert response.json()["block_agent_closes_when_blocked"] is True


def _block(
    client: TestClient,
    *,
    actor_id: int,
    target_id: int,
    blocker_id: int,
) -> None:
    response = client.post(
        f"/issues/{target_id}/links",
        json={"target_ref": str(blocker_id), "relation": "blocked_by"},
        headers=_actor(actor_id),
    )
    _expect(response, 201)


def _seed(client: TestClient, db_file: Path) -> Scenario:
    admin = _user(
        client,
        email="admin@example.com",
        name="Admin",
        password="admin-secret",
    )
    agent = _user(
        client,
        email="agent@example.com",
        name="Agent",
        actor_id=admin["id"],
        is_agent=True,
        password="agent-secret",
    )
    governed = _project(client, actor_id=admin["id"], name="Governed", key="GOV")
    added_status = client.post(
        f"/projects/{governed['id']}/statuses",
        json={"name": CUSTOM_DONE, "category": "done"},
        headers=_actor(admin["id"]),
    )
    _expect(added_status, 201)
    _enable_policy(client, actor_id=admin["id"], project_id=governed["id"])
    target = _issue(
        client,
        actor_id=agent["id"],
        title="Visible governed target",
        project_id=governed["id"],
    )

    hidden = _project(client, actor_id=admin["id"], name="Hidden", key="HID")
    blocker = _issue(
        client,
        actor_id=admin["id"],
        title="SECRET blocker title must never leak",
        project_id=hidden["id"],
    )
    _block(
        client,
        actor_id=admin["id"],
        target_id=target["id"],
        blocker_id=blocker["id"],
    )
    private = client.put(
        f"/projects/{hidden['id']}/visibility",
        json={"visibility": "private"},
        headers=_actor(admin["id"]),
    )
    _expect(private, 200)
    return Scenario(db_file, admin, agent, governed, target, blocker)


def _assert_open(client: TestClient, scenario: Scenario) -> None:
    current = client.get(
        f"/issues/{scenario.target['id']}",
        headers=_actor(scenario.admin["id"]),
    )
    _expect(current, 200)
    assert current.json()["status"] == "open"


def _login_agent(client: TestClient, scenario: Scenario) -> None:
    response = client.post(
        "/login",
        data={"email": scenario.agent["email"], "password": "agent-secret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    csrf = client.cookies.get("athena_csrf")
    assert csrf
    client.headers["X-CSRF-Token"] = csrf


@pytest.mark.parametrize("forged_override", [False, True])
def test_rest_agent_denial_is_stable_atomic_and_generic(tmp_path, forged_override):
    db_file = tmp_path / f"rest-agent-{forged_override}.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        conn = db.connect(db_file)
        baseline = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        conn.close()

        response = client.patch(
            f"/issues/{scenario.target['id']}",
            json={
                "status": CUSTOM_DONE,
                "override_blocked_close": forged_override,
            },
            headers=_actor(scenario.agent["id"]),
        )

        _expect(response, 409)
        assert response.json() == {"detail": POLICY_DETAIL, "code": POLICY_CODE}
        assert scenario.hidden_blocker["title"] not in response.text
        assert "private, no-store" in response.headers["Cache-Control"]
        _assert_open(client, scenario)
        conn = db.connect(db_file)
        assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == baseline
        conn.close()


def test_rest_human_override_honors_etag_and_audits_lineage(tmp_path):
    db_file = tmp_path / "rest-human.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        url = f"/issues/{scenario.target['id']}"
        coerced = client.patch(
            url,
            json={"status": CUSTOM_DONE, "override_blocked_close": "true"},
            headers=_actor(scenario.admin["id"]),
        )
        _expect(coerced, 422)
        _assert_open(client, scenario)

        reviewed = client.get(url, headers=_actor(scenario.admin["id"]))
        reviewed_tag = reviewed.headers["ETag"]
        revised = client.patch(
            url,
            json={"title": "Target revised after review"},
            headers=_actor(scenario.admin["id"]),
        )
        _expect(revised, 200)
        current_tag = revised.headers["ETag"]

        stale = client.patch(
            url,
            json={"status": CUSTOM_DONE, "override_blocked_close": True},
            headers={
                **_actor(scenario.admin["id"]),
                "If-Match": reviewed_tag,
            },
        )
        _expect(stale, 412)
        assert stale.json()["code"] == "precondition_failed"
        assert stale.headers["ETag"] == current_tag
        _assert_open(client, scenario)

        # Seed the operator's own run so the override can legitimately claim descent
        # from it: record() keeps a parent only when that run was actually written,
        # and a fork point only when it is an event OF that run.
        session = client.post(
            f"{url}/comments",
            json={"body": "reviewing the blocker before overriding"},
            headers={
                **_actor(scenario.admin["id"]),
                "X-Athena-Run": "operator-session",
            },
        )
        _expect(session, 201)
        conn = db.connect(db_file)
        cutoff = conn.execute("SELECT MAX(id) FROM activity").fetchone()[0]
        conn.close()
        override = client.patch(
            url,
            json={"status": CUSTOM_DONE, "override_blocked_close": True},
            headers={
                **_actor(scenario.admin["id"]),
                "If-Match": current_tag,
                "X-Athena-Run": "human-policy-override",
                "X-Athena-Parent-Run": "operator-session",
                "X-Athena-Fork-From-Event": str(cutoff),
            },
        )
        _expect(override, 200)
        assert override.json()["status"] == CUSTOM_DONE
        assert override.headers["ETag"] != current_tag

    conn = db.connect(db_file)
    events = [
        dict(row)
        for row in conn.execute(
            "SELECT actor_id, verb, detail, run_id, parent_run_id, "
            "forked_from_event_id FROM activity "
            "WHERE target_kind = 'issue' AND target_id = ? AND id > ? "
            "ORDER BY id",
            (scenario.target["id"], cutoff),
        )
    ]
    conn.close()
    assert [event["verb"] for event in events] == [
        "changed_status",
        "overrode_blocked_issue_close",
    ]
    assert events[1]["detail"] == ""
    assert {event["actor_id"] for event in events} == {scenario.admin["id"]}
    assert {event["run_id"] for event in events} == {"human-policy-override"}
    assert {event["parent_run_id"] for event in events} == {"operator-session"}
    assert {event["forked_from_event_id"] for event in events} == {cutoff}


def test_bulk_adds_policy_code_without_changing_legacy_shapes(tmp_path):
    db_file = tmp_path / "bulk.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        unblocked = _issue(
            client,
            actor_id=scenario.agent["id"],
            title="Unblocked batch peer",
            project_id=scenario.project["id"],
        )
        response = client.post(
            "/issues/bulk",
            json={
                "ids": [scenario.target["id"], unblocked["id"], 999_999],
                "status": CUSTOM_DONE,
            },
            headers=_actor(scenario.agent["id"]),
        )

        _expect(response, 200)
        body = response.json()
        assert (body["updated"], body["failed"]) == (1, 2)
        results = {item["id"]: item for item in body["results"]}
        assert results[scenario.target["id"]] == {
            "id": scenario.target["id"],
            "ok": False,
            "error": POLICY_DETAIL,
            "code": POLICY_CODE,
        }
        assert results[unblocked["id"]] == {
            "id": unblocked["id"],
            "ok": True,
            "error": None,
        }
        assert results[999_999] == {
            "id": 999_999,
            "ok": False,
            "error": "no such issue",
        }
        _assert_open(client, scenario)
        good = client.get(
            f"/issues/{unblocked['id']}", headers=_actor(scenario.admin["id"])
        )
        assert good.json()["status"] == CUSTOM_DONE


def test_mcp_agent_receives_policy_status_and_code(tmp_path):
    db_file = tmp_path / "mcp.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        minted = client.post(
            "/tokens",
            json={"name": "agent-mcp", "scopes": ["issue:write"]},
            headers=_actor(scenario.agent["id"]),
        )
        _expect(minted, 201)
        client.headers["Authorization"] = f"Bearer {minted.json()['token']}"
        mcp = AthenaClient(client=client)

        with pytest.raises(AthenaError) as rejected:
            mcp.update_issue(scenario.target["id"], status=CUSTOM_DONE)

        assert rejected.value.status_code == 409
        assert rejected.value.code == POLICY_CODE
        assert rejected.value.detail == POLICY_DETAIL
        assert rejected.value.path == f"/issues/{scenario.target['id']}"
        _assert_open(client, scenario)


def test_browser_agent_forged_confirm_hides_blocker_and_override(tmp_path):
    db_file = tmp_path / "browser.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        _login_agent(client, scenario)
        response = client.post(
            f"/aegis/issues/{scenario.target['id']}/status",
            data={"status": CUSTOM_DONE, "confirm": "1"},
            follow_redirects=False,
        )

        _expect(response, 409)
        assert (
            "workflow policy protects issues with unresolved blockers" in response.text
        )
        assert "Agent actors cannot override this policy" in response.text
        assert "Override policy and mark done" not in response.text
        assert scenario.hidden_blocker["title"] not in response.text
        assert scenario.hidden_blocker["key"] not in response.text
        _assert_open(client, scenario)


def test_board_htmx_and_no_js_policy_denials_stay_visible(tmp_path):
    db_file = tmp_path / "board.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        _login_agent(client, scenario)
        current = client.get(f"/issues/{scenario.target['id']}")
        form = {
            "new_status": CUSTOM_DONE,
            "if_match": current.headers["ETag"],
            "search": "",
            "status": "",
            "project": "",
            "sprint": "",
        }
        htmx = client.post(
            f"/aegis/boards/move/{scenario.target['id']}",
            data=form,
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        _expect(htmx, 409)
        assert "move_conflict=policy" in htmx.headers["HX-Redirect"]
        assert "private, no-store" in htmx.headers["Cache-Control"]
        notice = client.get(htmx.headers["HX-Redirect"])
        assert "Project workflow policy prevents closing this issue" in notice.text
        assert scenario.hidden_blocker["title"] not in notice.text

        native = client.post(
            f"/aegis/boards/move/{scenario.target['id']}",
            data=form,
            follow_redirects=False,
        )
        assert native.status_code == 303
        assert "move_conflict=policy" in native.headers["location"]
        notice = client.get(native.headers["location"])
        assert "Project workflow policy prevents closing this issue" in notice.text
        _assert_open(client, scenario)


def test_event_automation_policy_rejection_records_one_failure(tmp_path):
    db_file = tmp_path / "event-automation.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        conn = db.connect(db_file)
        rule = automation.create_rule(
            conn,
            name="close after priority change",
            trigger_verb="changed_priority",
            action_type="set_status",
            action_params={"status": CUSTOM_DONE},
            conditions={"project_id": scenario.project["id"]},
            created_by=scenario.admin["id"],
        )
        conn.close()
        changed = client.patch(
            f"/issues/{scenario.target['id']}",
            json={"priority": "high"},
            headers=_actor(scenario.agent["id"]),
        )
        _expect(changed, 200)

    assert automation.run_pass(db_file) == 0
    conn = db.connect(db_file)
    current_rule = automation.get_rule(conn, rule["id"])
    assert current_rule["failure_count"] == 1
    assert POLICY_DETAIL in current_rule["last_error"]
    status = conn.execute(
        "SELECT status FROM issues WHERE id = ?", (scenario.target["id"],)
    ).fetchone()["status"]
    assert status == "open"
    conn.close()

    assert automation.run_pass(db_file) == 0
    conn = db.connect(db_file)
    assert automation.get_rule(conn, rule["id"])["failure_count"] == 1
    conn.close()


def test_delegated_agent_close_is_still_denied(tmp_path):
    db_file = tmp_path / "delegated.db"
    with TestClient(create_app(db_file)) as client:
        scenario = _seed(client, db_file)
        delegated = _issue(
            client,
            actor_id=scenario.admin["id"],
            title="Delegated governed work",
            project_id=scenario.project["id"],
        )
        _block(
            client,
            actor_id=scenario.admin["id"],
            target_id=delegated["id"],
            blocker_id=scenario.hidden_blocker["id"],
        )
        delegated_response = client.post(
            f"/issues/{delegated['id']}/delegate",
            json={"user_id": scenario.agent["id"]},
            headers=_actor(scenario.admin["id"]),
        )
        _expect(delegated_response, 201)
        reviewed = client.get(
            f"/issues/{delegated['id']}", headers=_actor(scenario.agent["id"])
        )
        _expect(reviewed, 200)
        rejected = client.patch(
            f"/issues/{delegated['id']}",
            json={
                "status": CUSTOM_DONE,
                "override_blocked_close": True,
            },
            headers={
                **_actor(scenario.agent["id"]),
                "If-Match": reviewed.headers["ETag"],
            },
        )

        _expect(rejected, 409)
        assert rejected.json() == {"detail": POLICY_DETAIL, "code": POLICY_CODE}
        current = client.get(f"/issues/{delegated['id']}", headers=_actor(1))
        assert current.json()["status"] == "open"
