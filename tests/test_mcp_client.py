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


def test_create_issue_omits_status_so_project_default_applies(tmp_path):
    # WHY: project statuses are configurable. MCP must not force the old global
    # "open" default, or agents cannot create issues in projects whose first status
    # was renamed/removed.
    tc, ath = _client(tmp_path, "custom-status.db")
    try:
        project = tc.post("/projects", json={"name": "Ops", "key": "OPS"}).json()
        assert tc.delete(f"/projects/{project['id']}/statuses/open").status_code == 200

        issue = ath.create_issue(title="use project default", project_id=project["id"])
        assert issue["status"] == "in_progress"

        explicit = ath.create_issue(
            title="explicit status", project_id=project["id"], status="done"
        )
        assert explicit["status"] == "done"
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


def test_delegate_issue_through_the_client(tmp_path):
    # WHY: agent delegation must be reachable through MCP as a first-class action,
    # while preserving the human assignee as the accountable owner.
    tc, ath = _client(tmp_path, "delegate.db")
    try:
        agent = tc.post(
            "/users",
            json={"email": "bot@e.com", "name": "Bot", "is_agent": True},
            headers={"X-Athena-Actor": "1"},
        ).json()
        issue = ath.create_issue(title="delegate")
        ath.assign_issue(issue["id"], 1)

        contributors = ath.delegate_issue(issue["id"], agent["id"])
        assert contributors[0]["user_id"] == agent["id"]
        assert contributors[0]["is_agent"] is True
        assert ath.get_issue(str(issue["id"]))["assignee_id"] == 1
        assert "delegated" in [
            e["verb"] for e in ath.recent_events(kind="issue")["events"]
        ]
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


def test_run_lineage_and_issue_time_travel_through_the_client(tmp_path):
    # WHY: the newest log-as-truth features must be reachable by agents over MCP,
    # not only by browser/REST users: reconstruct runs, walk run lineage, and
    # time-travel an issue's lifecycle state from the same activity log.
    tc, ath = _client(tmp_path, "runs.db")
    try:
        tc.headers.update({"X-Athena-Run": "goal"})
        issue = ath.create_issue(title="lineage")

        tc.headers.update({"X-Athena-Run": "child", "X-Athena-Parent-Run": "goal"})
        ath.update_issue(issue["id"], status="in_progress")

        events = ath.recent_events(kind="issue")["events"]
        created_id = min(e["id"] for e in events if e["verb"] == "created")

        now = ath.get_issue_state(issue["id"])
        assert now["state"]["status"] == "in_progress"
        assert now["is_current"] is True

        then = ath.get_issue_state(issue["id"], as_of_event_id=created_id)
        assert then["state"]["status"] == "open"
        assert then["is_current"] is False

        runs = ath.list_activity_runs(actor_id=1)
        assert {"goal", "child"} <= {r["run_id"] for r in runs}

        lineage = ath.get_run_lineage("goal")
        assert lineage["run"]["run_id"] == "goal"
        assert [d["run_id"] for d in lineage["descendants"]] == ["child"]
        assert [a["run_id"] for a in ath.get_run_lineage("child")["ancestors"]] == [
            "goal"
        ]

        contract = ath.get_run_fork_contract(
            "goal", fork_from_event_id=created_id, fork_run_id="goal:alt"
        )
        assert contract["parent_run_id"] == "goal"
        assert contract["fork_run_id"] == "goal:alt"
        assert contract["fork_from_event_id"] == created_id
        assert contract["headers"] == {
            "X-Athena-Run": "goal:alt",
            "X-Athena-Parent-Run": "goal",
            "X-Athena-Fork-From-Event": str(created_id),
        }
        assert [event["id"] for event in contract["shared_prefix_events"]] == [
            created_id
        ]
    finally:
        tc.__exit__(None, None, None)


def test_archive_unarchive_through_the_client(tmp_path):
    # WHY: agents must be able to archive/restore issues and SEE archived ones via
    # MCP, not just through the web/REST — the full archival surface, reachable.
    tc, ath = _client(tmp_path, "arch.db")
    try:
        keep = ath.create_issue(title="keep")
        gone = ath.create_issue(title="gone")
        assert ath.archive_issue(gone["id"])["archived_at"] is not None
        # Default listing hides it; include_archived reveals it.
        assert [i["id"] for i in ath.list_issues()] == [keep["id"]]
        assert {i["id"] for i in ath.list_issues(include_archived=True)} == {
            keep["id"],
            gone["id"],
        }
        assert ath.unarchive_issue(gone["id"])["archived_at"] is None
        assert {i["id"] for i in ath.list_issues()} == {keep["id"], gone["id"]}
    finally:
        tc.__exit__(None, None, None)


def test_bulk_update_through_the_client(tmp_path):
    # WHY: agents must move many issues in one call, not N — the bulk endpoint is
    # reachable as a single MCP tool, best-effort with per-item results.
    tc, ath = _client(tmp_path, "bulk.db")
    try:
        proj = tc.post("/projects", json={"name": "Ops", "key": "OPS"}).json()
        a = ath.create_issue(title="a", project_id=proj["id"])
        b = ath.create_issue(title="b", project_id=proj["id"])
        out = ath.bulk_update_issues(
            [a["id"], b["id"], 9999], status="in_progress", assignee_id=1
        )
        assert out["updated"] == 2 and out["failed"] == 1
        outcomes = {r["id"]: r for r in out["results"]}
        assert outcomes[a["id"]]["ok"] and not outcomes[9999]["ok"]
        # The change actually landed.
        assert ath.get_issue(str(a["id"]))["status"] == "in_progress"
    finally:
        tc.__exit__(None, None, None)


def test_hierarchy_deps_sprints_labels_through_the_client(tmp_path):
    # WHY: these surfaces (parent/child, dependencies, sprints, labels) all have
    # REST endpoints + data layers, but were unreachable via MCP — an agent could
    # create an issue but not organize it. This drives the full path for each.
    tc, ath = _client(tmp_path, "cover.db")
    try:
        proj = tc.post("/projects", json={"name": "Ops", "key": "OPS"}).json()
        epic = ath.create_issue(title="Epic", project_id=proj["id"])
        task = ath.create_issue(title="Task", project_id=proj["id"])

        # Hierarchy: nest the task under the epic, list it, then unnest it.
        ath.set_issue_parent(task["id"], epic["id"])
        assert [c["id"] for c in ath.list_subtasks(epic["id"])] == [task["id"]]
        assert ath.set_issue_parent(task["id"], None)["parent_id"] is None

        # Dependencies: the epic blocks the task; read it back, then remove it.
        linked = ath.link_issues(epic["id"], str(task["id"]), "blocks")
        assert [b["id"] for b in linked["blocks"]] == [task["id"]]
        assert [b["id"] for b in ath.list_issue_links(epic["id"])["blocks"]] == [task["id"]]
        ath.unlink_issues(epic["id"], "blocks", task["id"])
        assert ath.list_issue_links(epic["id"])["blocks"] == []

        # Labels: create one in the shared vocabulary, attach + detach it.
        label = ath.create_label("bug", color="#ff0000")
        assert label["name"] == "bug" and label["id"] in [
            la["id"] for la in ath.list_labels()
        ]
        attached = ath.attach_label(task["id"], label["id"])
        assert label["id"] in [la["id"] for la in attached["labels"]]
        assert ath.detach_label(task["id"], label["id"])["labels"] == []

        # Sprints: create one in the project, put the task in it, list it, clear it.
        sprint = tc.post(f"/projects/{proj['id']}/sprints", json={"name": "S1"}).json()
        assert ath.set_issue_sprint(task["id"], sprint["id"])["sprint_id"] == sprint["id"]
        assert [s["id"] for s in ath.list_sprints(proj["id"])] == [sprint["id"]]
        assert ath.set_issue_sprint(task["id"], None)["sprint_id"] is None
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
            "get_issue_state",
            "assign_issue",
            "delegate_issue",
            "comment_on_issue",
            "archive_issue",
            "unarchive_issue",
            "bulk_update_issues",
            "recent_events",
            "list_activity_runs",
            "get_run_lineage",
            "get_run_fork_contract",
            "list_projects",
            "list_users",
            "list_spaces",
            "list_pages",
            "get_page",
            "create_page",
            "update_page",
            # The newly-added organize-an-issue surface.
            "set_issue_parent",
            "list_subtasks",
            "list_issue_links",
            "link_issues",
            "unlink_issues",
            "list_sprints",
            "set_issue_sprint",
            "list_labels",
            "create_label",
            "attach_label",
            "detach_label",
        } <= names

        # A tool call goes all the way through to the API and creates real data.
        asyncio.run(server.call_tool("create_issue", {"title": "via mcp"}))
        assert any(i["title"] == "via mcp" for i in ath.list_issues())
    finally:
        tc.__exit__(None, None, None)
