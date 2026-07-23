"""The MCP read surface a delegated agent needs to work a loop end-to-end.

An agent picking up delegated work over MCP could see the issue but not the
conversation around it (no comment read), not its own inbox (no notifications
read), not itself (no whoami), and not what any run actually did (no per-run
replay). These tests pin the four additions — whoami (identity + scopes + active
run in one call), list_issue_comments, the notifications inbox round-trip, and
list_run_events — through the real transport pieces: AthenaClient against the
app, and the FastMCP tool for whoami.
"""

import json

import pytest
from fastapi.testclient import TestClient

from athena.main import create_app
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}


def _workspace(tmp_path, name="reads.db"):
    """One app, two principals: an admin TestClient (trusted header) and an
    onboarded agent's AthenaClient (scoped bearer token, tagged run)."""
    app = create_app(tmp_path / name)
    admin = TestClient(app)
    admin.__enter__()
    admin.post("/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"})
    onboarded = admin.post(
        "/users/onboard_agent",
        json={"email": "sol@e.com", "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()
    agent_http = TestClient(app)
    agent_http.headers.update(
        {"Authorization": f"Bearer {onboarded['token']['token']}"}
    )
    agent = AthenaClient(client=agent_http, run_id="sol-session")
    return admin, agent, onboarded["user"]


def test_whoami_reports_identity_scopes_and_active_run(tmp_path):
    admin, agent, user = _workspace(tmp_path)
    try:
        me = agent.whoami()
        assert me["id"] == user["id"]
        assert me["email"] == "sol@e.com"
        assert me["is_agent"] is True
        assert me["scopes"] == ["read", "issue:write"]
    finally:
        admin.__exit__(None, None, None)


def test_whoami_tool_merges_identity_with_the_current_run(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from athena.mcp.server import build_server

    admin, agent, user = _workspace(tmp_path, "whoami_tool.db")
    try:
        server = build_server(agent)
        result = asyncio.run(server.call_tool("whoami", {}))
        payload = json.loads(result[0].text)
        assert payload["email"] == "sol@e.com"
        assert payload["scopes"] == ["read", "issue:write"]
        assert payload["run"]["run_id"] == "sol-session"
    finally:
        admin.__exit__(None, None, None)


def test_list_issue_comments_reads_the_thread_oldest_first(tmp_path):
    admin, agent, _ = _workspace(tmp_path, "comments.db")
    try:
        issue = admin.post("/issues", json={"title": "discuss"}, headers=H1).json()
        admin.post(
            f"/issues/{issue['id']}/comments",
            json={"body": "context from Ann"},
            headers=H1,
        )
        agent.comment_on_issue(issue["id"], "picking this up")
        thread = agent.list_issue_comments(issue["id"])
        assert [c["body"] for c in thread] == ["context from Ann", "picking this up"]
    finally:
        admin.__exit__(None, None, None)


def test_notifications_inbox_roundtrip(tmp_path):
    admin, agent, user = _workspace(tmp_path, "inbox.db")
    try:
        issue = admin.post("/issues", json={"title": "for sol"}, headers=H1).json()
        # A mention lands in the agent's inbox.
        admin.post(
            f"/issues/{issue['id']}/comments",
            json={"body": f"please pick this up [[user:{user['id']}]]"},
            headers=H1,
        )
        unread = agent.list_notifications(unread=True)
        assert len(unread) == 1
        assert unread[0]["target_kind"] == "issue"
        assert unread[0]["target_id"] == issue["id"]

        cleared = agent.mark_all_notifications_read()
        assert cleared == {"count": 1}
        assert agent.list_notifications(unread=True) == []
        # The read history is still there — cleared, not deleted.
        assert len(agent.list_notifications()) == 1
    finally:
        admin.__exit__(None, None, None)


def test_get_run_replay_freezes_the_run_into_an_artifact(tmp_path):
    admin, agent, _ = _workspace(tmp_path, "artifact.db")
    try:
        issue = agent.create_issue(title="artifact work")
        agent.update_issue(issue["id"], status="done")

        artifact = agent.get_run_replay("sol-session")
        assert artifact["run_id"] == "sol-session"
        assert artifact["event_count"] == 2
        assert [e["verb"] for e in artifact["events"]] == ["created", "changed_status"]
        assert artifact["replay_order"] and artifact["determinism_contract"]
    finally:
        admin.__exit__(None, None, None)


def test_list_run_events_replays_exactly_one_run(tmp_path):
    admin, agent, _ = _workspace(tmp_path, "replay.db")
    try:
        first = agent.create_issue(title="under sol-session")
        agent.update_issue(first["id"], status="in_progress")
        # Work under a different run must not leak into the replay.
        agent.set_run("other-run")
        agent.create_issue(title="under other-run")

        replay = agent.list_run_events("sol-session")
        assert {e["run_id"] for e in replay} == {"sol-session"}
        assert [e["verb"] for e in replay] == ["changed_status", "created"]
        assert all(e["target_id"] == first["id"] for e in replay)
    finally:
        admin.__exit__(None, None, None)
