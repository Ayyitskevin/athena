"""Approved desk improvements: public MCP -> client -> real REST, isolated DB."""

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest
from fastapi.testclient import TestClient

from athena.aegis import issues, lease_commands
from athena.core import db, run_controls, users
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server


@pytest.fixture
def desk(tmp_path):
    db_file = tmp_path / "desk.db"
    app = create_app(db_file)
    with TestClient(app) as admin:
        admin.post(
            "/users",
            json={"email": "admin@example.com", "name": "Admin", "password": "pw"},
        )
        admin.headers.update({"X-Athena-Actor": "1"})
        agent = admin.post(
            "/users/onboard_agent",
            json={
                "email": "agent@example.com",
                "name": "Agent",
                "scopes": ["read", "issue:write"],
            },
        ).json()
        http = TestClient(app)
        http.headers.update({"Authorization": f"Bearer {agent['token']['token']}"})
        client = AthenaClient(client=http, run_id="desk-proof")
        yield admin, client, agent["user"], db_file
        http.close()


def call(server, name, **args):
    return asyncio.run(server.call_tool(name, args))


def notify(admin, user, issue, text="please inspect"):
    response = admin.post(
        f"/issues/{issue['id']}/comments",
        json={"body": f"{text} [[user:{user['id']}]]"},
    )
    assert response.status_code == 201


@pytest.mark.parametrize("arrival", [False, True])
def test_targeted_ack_preserves_unshown_and_racing_arrival(desk, arrival):
    admin, client, user, _ = desk
    issue = admin.post("/issues", json={"title": "notifications"}).json()
    for n in range(18):
        notify(admin, user, issue, str(n))
    server = build_server(client, scopes={"read", "issue:write"})
    before = client.my_desk()
    assert before["signals"]["unread_notifications"] == 18
    shown = [
        json.loads(item.text)
        for item in call(server, "list_notifications", unread=True, limit=10)
    ]
    assert len(shown) == 10
    if arrival:
        notify(admin, user, issue, "arrived after read")
        new_id = client.list_notifications(unread=True)[0]["id"]
    call(server, "mark_notification_read", notification_id=shown[0]["id"])
    after = client.my_desk()
    unread = client.list_notifications(unread=True, limit=200)
    assert (
        len(unread)
        == after["signals"]["unread_notifications"]
        == (18 if arrival else 17)
    )
    assert shown[0]["id"] not in {row["id"] for row in unread}
    if arrival:
        assert new_id in {row["id"] for row in unread}
    assert before["cursor"] == after["cursor"]
    assert before["asks"] == after["asks"]
    # A repeated unkeyed acknowledgment matches REST's existing 404 contract.
    with pytest.raises(Exception, match="no such unread notification"):
        call(server, "mark_notification_read", notification_id=shown[0]["id"])
    assert len(client.list_notifications(unread=True, limit=200)) == len(unread)


def test_targeted_ack_refuses_other_actor_and_hidden_target(desk):
    admin, client, user, _ = desk
    project = admin.post("/projects", json={"name": "Secret", "key": "SEC"}).json()
    issue = admin.post(
        "/issues", json={"title": "target", "project_id": project["id"]}
    ).json()
    notify(admin, user, issue)
    own_id = client.list_notifications(unread=True)[0]["id"]
    client.comment_on_issue(issue["id"], "reply to owner")
    other_id = admin.get("/notifications", params={"unread": True}).json()[0]["id"]
    server = build_server(client)
    with pytest.raises(Exception, match="no such unread notification"):
        call(server, "mark_notification_read", notification_id=other_id)
    assert (
        admin.put(
            f"/projects/{project['id']}/visibility", json={"visibility": "private"}
        ).status_code
        == 200
    )
    with pytest.raises(Exception, match="no such unread notification"):
        call(server, "mark_notification_read", notification_id=own_id)
    assert (
        admin.put(
            f"/projects/{project['id']}/visibility", json={"visibility": "public"}
        ).status_code
        == 200
    )
    assert own_id in {row["id"] for row in client.list_notifications(unread=True)}


def test_bulk_help_and_scope_do_not_suggest_acknowledging_unseen_work(desk):
    _, client, _, _ = desk
    reader_tools = asyncio.run(build_server(client, scopes={"read"}).list_tools())
    assert "mark_notification_read" not in {t.name for t in reader_tools}
    tools = {t.name: t for t in asyncio.run(build_server(client).list_tools())}
    assert "including rows not shown" in tools["mark_notifications_read"].description
    assert "not completion" in tools["mark_notification_read"].description


def test_targeted_keyed_retry_preserves_pending_control_and_cursor(desk, monkeypatch):
    admin, client, user, _ = desk
    clock = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(
        run_controls,
        "_utc_now",
        lambda supplied: clock if supplied is None else supplied,
    )
    client.heartbeat_agent_run("desk-proof")
    control = AthenaClient(client=admin).create_run_control(
        run_id="desk-proof", kind="steer", payload="Inspect only"
    )
    issue = admin.post("/issues", json={"title": "one deliberate read"}).json()
    notify(admin, user, issue)
    notification_id = client.list_notifications(unread=True)[0]["id"]
    before = client.my_desk()
    assert before["asks"]["run_controls"]["items"]
    server = build_server(client)
    for _ in range(2):
        call(
            server,
            "mark_notification_read",
            notification_id=notification_id,
            idempotency_key="one-explicit-ack",
        )
    clock += timedelta(seconds=1)
    after = client.my_desk()
    assert after["signals"]["unread_notifications"] == 0
    assert after["cursor"] == before["cursor"]
    # A desk read derives the countdown anew; crossing a second is not a
    # control mutation. Require that exact clock delta, then compare EVERY
    # remaining control field and envelope (including pending state).
    before_seconds = before["asks"]["run_controls"]["items"][0].pop(
        "expires_in_seconds"
    )
    after_seconds = after["asks"]["run_controls"]["items"][0].pop("expires_in_seconds")
    assert after_seconds == before_seconds - 1
    assert after["asks"]["run_controls"] == before["asks"]["run_controls"]
    assert client.get_run_control(control["id"])["state"] == "requested"


@pytest.mark.parametrize("bad_id", [True, "1", 0, -1, 1.5])
def test_targeted_tool_rejects_coerced_or_invalid_ids(desk, bad_id):
    _, client, _, _ = desk
    with pytest.raises(Exception, match="validation"):
        call(build_server(client), "mark_notification_read", notification_id=bad_id)


def test_root_help_distinguishes_missing_and_stale_without_capability_hint(desk):
    _, client, _, _ = desk
    tools = {tool.name: tool for tool in asyncio.run(build_server(client).list_tools())}
    help_text = tools["claim_issue"].description
    assert "No selector" in help_text
    assert "stale explicit" in help_text
    assert "never falls back" in help_text
    assert "docs/ACTIVE_WORK.md" in help_text
    assert not any("catalog" in name or "root_capability" in name for name in tools)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("creator", False),
        ("assignee", True),
        ("contributor", True),
        ("admin", True),
        ("hidden", None),
    ],
)
def test_claim_guidance_uses_command_owned_predicate(desk, case, expected):
    admin, client, user, db_file = desk
    issue = client.create_issue(title="actor prerequisite")
    if case == "assignee":
        client.assign_issue(issue["id"], user["id"])
    if case == "contributor":
        assert (
            admin.post(
                f"/issues/{issue['id']}/contributors", json={"user_id": user["id"]}
            ).status_code
            == 201
        )
    if case == "hidden":
        project = admin.post("/projects", json={"name": "Private", "key": "PRI"}).json()
        assert (
            admin.patch(
                f"/issues/{issue['id']}", json={"project_id": project["id"]}
            ).status_code
            == 200
        )
        assert (
            admin.put(
                f"/projects/{project['id']}/visibility", json={"visibility": "private"}
            ).status_code
            == 200
        )
        with pytest.raises(Exception, match="no such issue"):
            client.get_issue_work_context(str(issue["id"]))
        return
    reader = AthenaClient(client=admin) if case == "admin" else client
    context = reader.get_issue_work_context(str(issue["id"]))
    guidance = context["how_to_claim"]["claimant_prerequisite"]
    with db.connect(db_file) as conn:
        actor = users.get_user(conn, 1 if case == "admin" else user["id"])
        assert (
            lease_commands.claimant_is_eligible(
                conn, issues.get_issue(conn, issue["id"]), actor
            )
            is expected
        )
    assert guidance["satisfied_at_read"] is expected
    assert "not authority" in guidance["meaning"]
    assert guidance["next_operation"] == (
        "get_issue_lease then claim_issue with issue_etag"
        if expected
        else "request assignment or contributor delegation from an authorized actor"
    )
    # The read never assigns or claims the issue itself.
    assert client.get_issue_lease(issue["id"]) is None


def test_claim_guidance_does_not_bypass_changed_assignment(desk):
    admin, client, user, _ = desk
    issue = client.create_issue(title="eligibility can change")
    client.assign_issue(issue["id"], user["id"])
    packet = client.get_issue_work_context(str(issue["id"]))
    assert packet["how_to_claim"]["claimant_prerequisite"]["satisfied_at_read"]
    assert (
        admin.put(
            f"/issues/{issue['id']}/assignee", json={"assignee_id": None}
        ).status_code
        == 200
    )
    with pytest.raises(Exception, match="only the assignee"):
        client.claim_issue(issue["id"], if_match=packet["issue_etag"])
