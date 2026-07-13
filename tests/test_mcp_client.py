"""The MCP server's Athena client (and the MCP wiring on top of it).

The MCP server is a client of Athena like the web UI is — it goes through the REST
API with a scoped bearer token. These tests exercise AthenaClient against the REAL
app (an injected TestClient + a real minted token), so the full path tool ->
client -> HTTP -> API -> DB is covered, including auth.

The MCP-SDK wiring itself is tested separately and SKIPPED when the optional `mcp`
extra isn't installed (e.g. in core CI), so the suite stays green without it.
"""

import httpx
import pickle
import pytest
from fastapi.testclient import TestClient

from athena.main import create_app
from athena.aegis import automation
from athena.core import db
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


class _RecordingClient:
    """Small injected transport that records the exact per-call HTTP kwargs."""

    def __init__(self):
        self.calls = []

    def _response(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request(method, f"http://athena.test{path}"),
        )

    def post(self, path, **kwargs):
        return self._response("POST", path, **kwargs)

    def patch(self, path, **kwargs):
        return self._response("PATCH", path, **kwargs)

    def put(self, path, **kwargs):
        return self._response("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self._response("DELETE", path, **kwargs)


MUTATION_CASES = [
    (
        "create_issue",
        "POST",
        "/issues",
        lambda c, k: c.create_issue(title="x", idempotency_key=k),
    ),
    (
        "update_issue",
        "PATCH",
        "/issues/7",
        lambda c, k: c.update_issue(7, title="x", idempotency_key=k),
    ),
    (
        "set_issue_placement",
        "PATCH",
        "/issues/7",
        lambda c, k: c.set_issue_placement(
            7, project_id=None, sprint_id=None, idempotency_key=k
        ),
    ),
    (
        "assign_issue",
        "PUT",
        "/issues/7/assignee",
        lambda c, k: c.assign_issue(7, None, idempotency_key=k),
    ),
    (
        "delegate_issue",
        "POST",
        "/issues/7/delegate",
        lambda c, k: c.delegate_issue(7, 9, idempotency_key=k),
    ),
    (
        "comment_on_issue",
        "POST",
        "/issues/7/comments",
        lambda c, k: c.comment_on_issue(7, "x", idempotency_key=k),
    ),
    (
        "archive_issue",
        "POST",
        "/issues/7/archive",
        lambda c, k: c.archive_issue(7, idempotency_key=k),
    ),
    (
        "unarchive_issue",
        "POST",
        "/issues/7/unarchive",
        lambda c, k: c.unarchive_issue(7, idempotency_key=k),
    ),
    (
        "bulk_update_issues",
        "POST",
        "/issues/bulk",
        lambda c, k: c.bulk_update_issues([7], status="done", idempotency_key=k),
    ),
    (
        "set_issue_parent",
        "PUT",
        "/issues/7/parent",
        lambda c, k: c.set_issue_parent(7, None, idempotency_key=k),
    ),
    (
        "link_issues",
        "POST",
        "/issues/7/links",
        lambda c, k: c.link_issues(7, "9", "blocks", idempotency_key=k),
    ),
    (
        "unlink_issues",
        "DELETE",
        "/issues/7/links/blocks/9",
        lambda c, k: c.unlink_issues(7, "blocks", 9, idempotency_key=k),
    ),
    (
        "set_issue_sprint",
        "PUT",
        "/issues/7/sprint",
        lambda c, k: c.set_issue_sprint(7, None, idempotency_key=k),
    ),
    (
        "create_label",
        "POST",
        "/labels",
        lambda c, k: c.create_label("bug", idempotency_key=k),
    ),
    (
        "attach_label",
        "POST",
        "/issues/7/labels",
        lambda c, k: c.attach_label(7, 9, idempotency_key=k),
    ),
    (
        "detach_label",
        "DELETE",
        "/issues/7/labels/9",
        lambda c, k: c.detach_label(7, 9, idempotency_key=k),
    ),
    (
        "create_page",
        "POST",
        "/spaces/4/pages",
        lambda c, k: c.create_page(space_id=4, title="x", idempotency_key=k),
    ),
    (
        "update_page",
        "PATCH",
        "/pages/4",
        lambda c, k: c.update_page(4, title="x", idempotency_key=k),
    ),
]

MUTATION_TOOL_NAMES = {case[0] for case in MUTATION_CASES}
IF_MATCH_TOOL_NAMES = {
    "update_issue",
    "set_issue_placement",
    "assign_issue",
    "set_issue_sprint",
}
MCP_MUTATION_CASES = [
    ("create_issue", {"title": "x"}),
    ("update_issue", {"issue_id": 7}),
    (
        "set_issue_placement",
        {"issue_id": 7, "project_id": None, "sprint_id": None},
    ),
    ("assign_issue", {"issue_id": 7}),
    ("delegate_issue", {"issue_id": 7, "agent_user_id": 9}),
    ("comment_on_issue", {"issue_id": 7, "body": "x"}),
    ("archive_issue", {"issue_id": 7}),
    ("unarchive_issue", {"issue_id": 7}),
    ("bulk_update_issues", {"ids": [7]}),
    ("set_issue_parent", {"issue_id": 7}),
    (
        "link_issues",
        {"issue_id": 7, "target_ref": "9", "relation": "blocks"},
    ),
    (
        "unlink_issues",
        {"issue_id": 7, "relation": "blocks", "target_id": 9},
    ),
    ("set_issue_sprint", {"issue_id": 7}),
    ("create_label", {"name": "bug"}),
    ("attach_label", {"issue_id": 7, "label_id": 9}),
    ("detach_label", {"issue_id": 7, "label_id": 9}),
    ("create_page", {"space_id": 4, "title": "x"}),
    ("update_page", {"page_id": 4}),
]
MCP_IF_MATCH_CASES = [
    case for case in MCP_MUTATION_CASES if case[0] in IF_MATCH_TOOL_NAMES
]


class _MCPRecordingAthenaClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return [] if name == "delegate_issue" else {}

        return record


class _MCPFailingAthenaClient(_MCPRecordingAthenaClient):
    def create_issue(self, **kwargs):
        raise AthenaError(
            method="POST",
            path="/issues",
            status_code=409,
            detail="still running",
            code="idempotency_in_progress",
            retry_after="7",
            current_etag='"current"',
        )


@pytest.mark.parametrize(
    ("name", "method", "path", "invoke"),
    MUTATION_CASES,
    ids=[case[0] for case in MUTATION_CASES],
)
def test_every_client_mutation_forwards_only_explicit_idempotency_keys(
    name, method, path, invoke
):
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    for key, expected_headers in (
        ("stable-key", {"Idempotency-Key": "stable-key"}),
        (None, None),
        ("", {"Idempotency-Key": ""}),
    ):
        assert invoke(client, key) == {"ok": True}
        recorded_method, recorded_path, kwargs = transport.calls.pop()
        assert (recorded_method, recorded_path) == (method, path), name
        if expected_headers is None:
            assert "headers" not in kwargs, name
        else:
            assert kwargs["headers"] == expected_headers, name


def test_mutation_helper_merges_existing_headers_case_insensitively():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    client._mutate(
        transport.post,
        "/issues",
        idempotency_key="new-key",
        if_match='"v2"',
        headers={"if-match": '"v1"', "idempotency-key": "old-key"},
        json={"title": "x"},
    )

    _, _, kwargs = transport.calls.pop()
    headers = httpx.Headers(kwargs["headers"])
    assert headers["If-Match"] == '"v2"'
    assert headers["Idempotency-Key"] == "new-key"
    assert len(headers.get_list("If-Match")) == 1
    assert len(headers.get_list("Idempotency-Key")) == 1


def test_athena_error_preserves_legacy_construction_and_pickle_state():
    legacy = AthenaError("legacy failure")
    assert str(legacy) == "legacy failure"

    structured = AthenaError(
        method="POST",
        path="/issues",
        status_code=409,
        detail="still running",
        code="idempotency_in_progress",
        retry_after="7",
        current_etag='"current"',
    )
    restored = pickle.loads(pickle.dumps(structured))

    assert str(restored) == str(structured)
    assert restored.as_dict() == structured.as_dict()


def test_issue_lifecycle_through_the_client(tmp_path):
    tc, ath = _client(tmp_path, "iss.db")
    try:
        issue = ath.create_issue(
            title="ship it", body="see [[page:1]]", priority="high"
        )
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


def test_client_exposes_etags_and_surfaces_stale_issue_preconditions(tmp_path):
    tc, ath = _client(tmp_path, "etag-client.db")
    try:
        created = ath.create_issue(title="before")
        assert created["_etag"].startswith('"sha256-')

        fetched = ath.get_issue(str(created["id"]))
        assert fetched["_etag"] == created["_etag"]

        updated = ath.update_issue(
            created["id"],
            title="after",
            if_match=fetched["_etag"],
            idempotency_key="guarded-update",
        )
        assert updated["title"] == "after"
        assert updated["_etag"] != fetched["_etag"]

        with pytest.raises(AthenaError) as stale:
            ath.update_issue(
                created["id"],
                title="lost update",
                if_match=fetched["_etag"],
            )
        assert stale.value.status_code == 412
        assert stale.value.code == "precondition_failed"
        assert stale.value.current_etag == updated["_etag"]
        assert ath.get_issue(str(created["id"]))["title"] == "after"
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


def test_mission_control_observation_through_client(tmp_path):
    # WHY: fleet supervision must traverse the same MCP-client -> REST -> DB path as
    # every other agent capability; a web-only cockpit cannot be automated or audited.
    db_file = tmp_path / "mission-control.db"
    tc, ath = _client(tmp_path, db_file.name)
    try:
        agent = tc.post(
            "/users",
            json={"email": "bot@e.com", "name": "Bot", "is_agent": True},
        ).json()
        acted = tc.post(
            "/issues",
            json={"title": "Observed work"},
            headers={
                "Authorization": "not-bearer",
                "X-Athena-Actor": str(agent["id"]),
                "X-Athena-Run": "bot-run",
            },
        )
        assert acted.status_code == 201

        rule = tc.post(
            "/automation/rules",
            json={
                "name": "broken rule",
                "trigger_verb": "created",
                "action_type": "comment",
                "action_params": {"body": "x"},
            },
        ).json()
        conn = db.connect(db_file)
        try:
            automation.record_rule_failure(
                conn, rule["id"], "RuntimeError: operator-visible"
            )
        finally:
            conn.close()

        health = ath.get_agent_run_health()
        assert health["totals"]["agents_with_activity_count"] == 1
        assert [row["user"]["id"] for row in health["agents"]] == [agent["id"]]
        assert health["agents"][0]["latest_run"]["run_id"] == "bot-run"
        assert "replay_export_command" not in health["agents"][0]["latest_run"]
        assert "has_password" not in health["agents"][0]["user"]

        filtered = ath.get_agent_run_health(agent_id=agent["id"])
        assert [row["user"]["id"] for row in filtered["agents"]] == [agent["id"]]

        failures = ath.list_automation_failures()
        assert [item["id"] for item in failures] == [rule["id"]]
        assert failures[0]["last_error"] == "RuntimeError: operator-visible"
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


def test_atomic_issue_placement_through_the_client(tmp_path, monkeypatch):
    # WHY: an agent moving an issue between projects must send the destination
    # project and sprint in ONE request. Sequential project/sprint tools expose an
    # invalid intermediate placement and cannot express an atomic final pair.
    tc, ath = _client(tmp_path, "placement.db")
    try:
        source = tc.post("/projects", json={"name": "Source", "key": "SRC"}).json()
        target = tc.post("/projects", json={"name": "Target", "key": "TGT"}).json()
        source_sprint = tc.post(
            f"/projects/{source['id']}/sprints", json={"name": "Source sprint"}
        ).json()
        target_sprint = tc.post(
            f"/projects/{target['id']}/sprints", json={"name": "Target sprint"}
        ).json()
        issue = ath.create_issue(title="move once", project_id=source["id"])
        ath.set_issue_sprint(issue["id"], source_sprint["id"])

        patch_calls = []
        real_patch = tc.patch

        def recording_patch(path, **kwargs):
            patch_calls.append((path, kwargs.get("json")))
            return real_patch(path, **kwargs)

        monkeypatch.setattr(tc, "patch", recording_patch)

        moved = ath.set_issue_placement(
            issue["id"], project_id=target["id"], sprint_id=target_sprint["id"]
        )
        assert patch_calls == [
            (
                f"/issues/{issue['id']}",
                {"project_id": target["id"], "sprint_id": target_sprint["id"]},
            )
        ]

        assert (moved["project_id"], moved["sprint_id"]) == (
            target["id"],
            target_sprint["id"],
        )

        # Null is a value on this dedicated surface, not an omitted optional field.
        backlog = ath.set_issue_placement(issue["id"], project_id=None, sprint_id=None)
        assert patch_calls[-1] == (
            f"/issues/{issue['id']}",
            {"project_id": None, "sprint_id": None},
        )

        assert (backlog["project_id"], backlog["sprint_id"]) == (None, None)

        # The server's relationship error is preserved for the agent, and the
        # rejected pair does not partially move the issue.
        with pytest.raises(AthenaError) as exc:
            ath.set_issue_placement(
                issue["id"],
                project_id=target["id"],
                sprint_id=source_sprint["id"],
            )
        assert "422" in str(exc.value)
        assert "sprint belongs to a different project than the issue" in str(exc.value)
        unchanged = ath.get_issue(str(issue["id"]))
        assert (unchanged["project_id"], unchanged["sprint_id"]) == (None, None)
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
        assert [b["id"] for b in ath.list_issue_links(epic["id"])["blocks"]] == [
            task["id"]
        ]
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
        assert (
            ath.set_issue_sprint(task["id"], sprint["id"])["sprint_id"] == sprint["id"]
        )
        assert [s["id"] for s in ath.list_sprints(proj["id"])] == [sprint["id"]]
        assert ath.set_issue_sprint(task["id"], None)["sprint_id"] is None
    finally:
        tc.__exit__(None, None, None)


def test_client_replays_one_logical_mutation_and_surfaces_mismatch(tmp_path):
    tc, ath = _client(tmp_path, "idempotent-client.db")
    try:
        key = "mcp-create-once"
        first = ath.create_issue(title="one logical write", idempotency_key=key)
        replay = ath.create_issue(title="one logical write", idempotency_key=key)

        assert replay == first
        assert [issue["id"] for issue in ath.list_issues()] == [first["id"]]
        assert [
            event["verb"] for event in ath.recent_events(kind="issue")["events"]
        ] == ["created"]

        with pytest.raises(AthenaError) as mismatch:
            ath.create_issue(title="different write", idempotency_key=key)
        error = mismatch.value
        assert error.status_code == 409
        assert error.method == "POST"
        assert error.path == "/issues"
        assert error.code == "idempotency_mismatch"
        assert error.retry_after is None
        assert error.detail == "Idempotency-Key reused for a different request"
        assert str(error) == (
            "POST /issues -> 409: Idempotency-Key reused for a different request"
        )

        with pytest.raises(AthenaError) as invalid:
            ath.create_issue(title="invalid key", idempotency_key="")
        assert invalid.value.status_code == 400
        assert invalid.value.code is None
        assert "1-255 visible ASCII" in invalid.value.detail
    finally:
        tc.__exit__(None, None, None)


def test_error_preserves_retry_after_and_machine_code():
    response = httpx.Response(
        409,
        json={
            "detail": "A request with this Idempotency-Key is still in progress",
            "code": "idempotency_in_progress",
        },
        headers={"Retry-After": "1", "ETag": '"current"'},
        request=httpx.Request("POST", "http://athena.test/issues"),
    )

    with pytest.raises(AthenaError) as exc:
        AthenaClient(client=object())._result(response)

    assert exc.value.status_code == 409
    assert exc.value.code == "idempotency_in_progress"
    assert exc.value.retry_after == "1"
    assert exc.value.current_etag == '"current"'


def test_error_surfaces_status_and_detail(tmp_path):
    tc, ath = _client(tmp_path, "err.db")
    try:
        with pytest.raises(AthenaError) as exc:
            ath.get_issue("99999")
        error = exc.value
        assert "404" in str(error)
        assert error.status_code == 404
        assert error.method == "GET"
        assert error.path == "/issues/99999"
        assert error.code is None
        assert error.retry_after is None
    finally:
        tc.__exit__(None, None, None)


# --- MCP wiring (skipped without the optional `mcp` extra) ------------------
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    MCP_MUTATION_CASES,
    ids=[case[0] for case in MCP_MUTATION_CASES],
)
def test_every_mcp_mutation_forwards_the_optional_key(tool_name, arguments):
    pytest.importorskip("mcp")
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    for key in ("stable-key", None):
        tool_arguments = dict(arguments)
        if key is not None:
            tool_arguments["idempotency_key"] = key
        asyncio.run(server.call_tool(tool_name, tool_arguments))

        called_name, _, kwargs = client.calls.pop()
        assert called_name == tool_name
        assert kwargs["idempotency_key"] == key


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    MCP_IF_MATCH_CASES,
    ids=[case[0] for case in MCP_IF_MATCH_CASES],
)
def test_guarded_mcp_issue_mutations_forward_if_match(tool_name, arguments):
    pytest.importorskip("mcp")
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    asyncio.run(server.call_tool(tool_name, {**arguments, "if_match": '"current"'}))

    called_name, _, kwargs = client.calls.pop()
    assert called_name == tool_name
    assert kwargs["if_match"] == '"current"'


@pytest.mark.parametrize(
    "invalid_key",
    ["", "contains space", "é", "x" * 256],
)
def test_mcp_schema_rejects_invalid_idempotency_keys_before_dispatch(invalid_key):
    pytest.importorskip("mcp")
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "create_issue",
                {"title": "never runs", "idempotency_key": invalid_key},
            )
        )
    assert client.calls == []


def test_mcp_error_text_preserves_structured_retry_metadata():
    pytest.importorskip("mcp")
    import asyncio
    import json

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    server = build_server(_MCPFailingAthenaClient())
    with pytest.raises(ToolError) as exc:
        asyncio.run(
            server.call_tool(
                "create_issue",
                {"title": "x", "idempotency_key": "stable-key"},
            )
        )

    marker = "ATHENA_ERROR_JSON="
    assert marker in str(exc.value)
    payload = json.loads(str(exc.value).split(marker, 1)[1])
    assert payload["status_code"] == 409
    assert payload["code"] == "idempotency_in_progress"
    assert payload["retry_after"] == "7"
    assert payload["current_etag"] == '"current"'
    assert payload["message"] == "POST /issues -> 409: still running"


def test_mcp_server_registers_tools_and_calls_through(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from athena.mcp.server import build_server

    tc, ath = _client(tmp_path, "mcp.db")
    try:
        server = build_server(ath)
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        names = set(tools)
        # The agent-facing surface is present.
        assert {
            "search",
            "list_issues",
            "get_issue",
            "create_issue",
            "update_issue",
            "set_issue_placement",
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
            "get_agent_run_health",
            "list_automation_failures",
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

        # Placement's two nullable values are required in the MCP contract. This
        # keeps omission distinct from an explicit backlog/no-sprint placement.
        placement_schema = tools["set_issue_placement"].inputSchema
        assert {"issue_id", "project_id", "sprint_id"} <= set(
            placement_schema["required"]
        )
        for field_name in ("project_id", "sprint_id"):
            types = {
                option["type"]
                for option in placement_schema["properties"][field_name]["anyOf"]
            }
            assert types == {"integer", "null"}
        assert MUTATION_TOOL_NAMES <= names
        for tool_name in MUTATION_TOOL_NAMES:
            schema = tools[tool_name].inputSchema
            assert "idempotency_key" in schema["properties"]
            assert "idempotency_key" not in set(schema.get("required", []))
            key_types = {
                option["type"]
                for option in schema["properties"]["idempotency_key"]["anyOf"]
            }
            assert key_types == {"string", "null"}
            string_schema = next(
                option
                for option in schema["properties"]["idempotency_key"]["anyOf"]
                if option["type"] == "string"
            )
            assert string_schema["minLength"] == 1
            assert string_schema["maxLength"] == 255
            assert string_schema["pattern"] == r"^[\x21-\x7E]+$"

        for tool_name in names - MUTATION_TOOL_NAMES:
            assert "idempotency_key" not in tools[tool_name].inputSchema["properties"]

        for tool_name in IF_MATCH_TOOL_NAMES:
            schema = tools[tool_name].inputSchema
            assert "if_match" in schema["properties"]
            assert "if_match" not in set(schema.get("required", []))
        for tool_name in names - IF_MATCH_TOOL_NAMES:
            assert "if_match" not in tools[tool_name].inputSchema["properties"]

        # Read-only operator tools are wired through FastMCP, not merely client helpers.
        asyncio.run(server.call_tool("get_agent_run_health", {}))
        asyncio.run(server.call_tool("list_automation_failures", {}))

        # An omitted key remains backward-compatible and creates real data.
        asyncio.run(server.call_tool("create_issue", {"title": "via mcp"}))
        assert any(i["title"] == "via mcp" for i in ath.list_issues())

        # A caller-supplied key survives separate MCP invocations and coalesces
        # them into one logical REST mutation.
        keyed_args = {
            "title": "via mcp once",
            "idempotency_key": "mcp-create-once",
        }
        first = asyncio.run(server.call_tool("create_issue", keyed_args))
        replay = asyncio.run(server.call_tool("create_issue", keyed_args))
        assert replay == first
        assert [i["title"] for i in ath.list_issues()].count("via mcp once") == 1
        import json

        from mcp.server.fastmcp.exceptions import ToolError

        created_events = len(ath.recent_events(kind="issue")["events"])
        with pytest.raises(ToolError) as mismatch:
            asyncio.run(
                server.call_tool(
                    "create_issue",
                    {
                        "title": "different via mcp",
                        "idempotency_key": "mcp-create-once",
                    },
                )
            )
        marker = "ATHENA_ERROR_JSON="
        payload = json.loads(str(mismatch.value).split(marker, 1)[1])
        assert payload["code"] == "idempotency_mismatch"
        assert [i["title"] for i in ath.list_issues()].count("via mcp once") == 1
        assert len(ath.recent_events(kind="issue")["events"]) == created_events
    finally:
        tc.__exit__(None, None, None)
