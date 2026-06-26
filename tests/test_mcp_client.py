"""The MCP server's Athena client (and the MCP wiring on top of it).

The MCP server is a client of Athena like the web UI is — it goes through the REST
API with a scoped bearer token. These tests exercise AthenaClient against the REAL
app (an injected TestClient + a real minted token), so the full path tool ->
client -> HTTP -> API -> DB is covered, including auth.

The MCP-SDK wiring itself is tested separately and SKIPPED when the optional `mcp`
extra isn't installed (e.g. in core CI), so the suite stays green without it.
"""
import pytest
from fastapi.testclient import TestClient

from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError


def _client(tmp_path, name) -> tuple[TestClient, AthenaClient]:
    """A TestClient with a real admin bearer token set, wrapped in an AthenaClient.
    Returns both so a test can still bootstrap via the raw client if needed."""
    app = create_app(tmp_path / name)
    tc = TestClient(app)
    tc.__enter__()  # run lifespan (migrate); torn down by the fixture-less caller
    # Bootstrap the first user (becomes admin), then mint a token through the
    # trusted-actor path (enabled in tests by conftest).
    tc.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    raw = tc.post(
        "/tokens", json={"name": "mcp"}, headers={"X-Athena-Actor": "1"}
    ).json()["token"]
    tc.headers.update({"Authorization": f"Bearer {raw}"})
    return tc, AthenaClient(client=tc)


def test_issue_lifecycle_through_the_client(tmp_path):
    tc, ath = _client(tmp_path, "iss.db")
    try:
        issue = ath.create_issue(title="ship it", body="see [[page:1]]", priority="high")
        assert issue["title"] == "ship it" and issue["priority"] == "high"

        # Addressable by id and (once in a project) by key; here by id.
        assert ath.get_issue(str(issue["id"]))["id"] == issue["id"]

        moved = ath.update_issue(issue["id"], status="in_progress")
        assert moved["status"] == "in_progress"

        ath.comment_on_issue(issue["id"], "on it")
        assert ath.list_issues(status="in_progress")[0]["id"] == issue["id"]

        # Full-text search spans the issue we just made.
        hits = ath.search("ship")
        assert any(h["source_id"] == issue["id"] and h["kind"] == "issue" for h in hits)
    finally:
        tc.__exit__(None, None, None)


def test_assign_and_list_users(tmp_path):
    tc, ath = _client(tmp_path, "assign.db")
    try:
        users = ath.list_users()
        assert users and users[0]["id"] == 1
        issue = ath.create_issue(title="assign me")
        assigned = ath.assign_issue(issue["id"], 1)
        assert assigned["assignee_id"] == 1
        # Unassign.
        assert ath.assign_issue(issue["id"], None)["assignee_id"] is None
    finally:
        tc.__exit__(None, None, None)


def test_pages_through_the_client(tmp_path):
    tc, ath = _client(tmp_path, "pages.db")
    try:
        space = tc.post("/spaces", json={"key": "ENG", "name": "Eng"}).json()
        page = ath.create_page(space_id=space["id"], title="Runbook", body="# Step")
        assert page["title"] == "Runbook"
        assert ath.get_page(page["id"])["id"] == page["id"]
        assert [p["id"] for p in ath.list_pages(space["id"])] == [page["id"]]
        updated = ath.update_page(page["id"], body="# Step 1\n# Step 2")
        assert "Step 2" in updated["body"]
        assert any(s["key"] == "ENG" for s in ath.list_spaces())
    finally:
        tc.__exit__(None, None, None)


def test_recent_events_envelope(tmp_path):
    tc, ath = _client(tmp_path, "ev.db")
    try:
        ath.create_issue(title="one")
        ath.create_issue(title="two")
        feed = ath.recent_events()
        assert set(feed) == {"events", "next_after", "has_more"}
        assert [e["verb"] for e in feed["events"]] == ["created", "created"]
        # Resume from the cursor: no new events yet.
        assert ath.recent_events(after=feed["next_after"])["events"] == []
    finally:
        tc.__exit__(None, None, None)


def test_error_surfaces_status_and_detail(tmp_path):
    tc, ath = _client(tmp_path, "err.db")
    try:
        with pytest.raises(AthenaError) as exc:
            ath.get_issue("99999")
        assert "404" in str(exc.value)
    finally:
        tc.__exit__(None, None, None)


# --- MCP wiring (skipped without the optional `mcp` extra) ------------------


def test_mcp_server_registers_tools_and_calls_through(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from athena.mcp.server import build_server

    tc, ath = _client(tmp_path, "mcp.db")
    try:
        server = build_server(ath)
        names = {t.name for t in asyncio.run(server.list_tools())}
        # The agent-facing surface is present.
        assert {
            "search",
            "list_issues",
            "get_issue",
            "create_issue",
            "update_issue",
            "assign_issue",
            "comment_on_issue",
            "recent_events",
            "list_projects",
            "list_users",
            "list_spaces",
            "list_pages",
            "get_page",
            "create_page",
            "update_page",
        } <= names

        # A tool call goes all the way through to the API and creates real data.
        asyncio.run(server.call_tool("create_issue", {"title": "via mcp"}))
        assert any(i["title"] == "via mcp" for i in ath.list_issues())
    finally:
        tc.__exit__(None, None, None)
