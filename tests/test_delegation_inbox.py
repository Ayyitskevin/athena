"""Delegation pickup is self-only, bounded, factual, and shared across surfaces."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from athena.aegis import delegations, dependencies
from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient


H_ADMIN = {"X-Athena-Actor": "1"}


def _bootstrap(client: TestClient) -> tuple[dict, dict, dict]:
    admin = client.post(
        "/users",
        json={
            "email": "admin@e.com",
            "name": "Admin",
            "password": "secret",
        },
    ).json()
    agent_a = client.post(
        "/users",
        json={
            "email": "agent-a@e.com",
            "name": "Agent A",
            "password": "secret",
            "is_agent": True,
        },
        headers=H_ADMIN,
    ).json()
    agent_b = client.post(
        "/users",
        json={
            "email": "agent-b@e.com",
            "name": "Agent B",
            "password": "secret",
            "is_agent": True,
        },
        headers=H_ADMIN,
    ).json()
    return admin, agent_a, agent_b


def _token(client: TestClient, user_id: int, scopes: list[str], name: str) -> dict:
    response = client.post(
        "/tokens",
        json={"name": name, "scopes": scopes},
        headers={"X-Athena-Actor": str(user_id)},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _issue(client: TestClient, title: str, **fields) -> dict:
    response = client.post(
        "/issues",
        json={"title": title, **fields},
        headers=H_ADMIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _delegate(client: TestClient, issue_id: int, agent_id: int) -> None:
    response = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent_id},
        headers=H_ADMIN,
    )
    assert response.status_code == 201, response.text


def _login(client: TestClient, email: str) -> None:
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    response = client.post(
        "/login",
        data={"email": email, "password": "secret"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _domain_snapshot(db_path) -> dict[str, list[tuple]]:
    conn = db.connect(db_path)
    try:
        tables = (
            "issues",
            "issue_contributors",
            "issue_links",
            "issue_labels",
            "activity",
            "watches",
            "notifications",
        )
        return {
            table: [
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            ]
            for table in tables
        }
    finally:
        conn.close()


def test_rest_requires_read_scope_isolates_actor_and_has_no_domain_side_effects(
    tmp_path,
):
    db_path = tmp_path / "auth.db"
    with TestClient(create_app(db_path)) as client:
        admin, agent_a, agent_b = _bootstrap(client)
        mine = _issue(
            client,
            "Agent A task",
            body="Inspect the release.",
            priority="high",
        )
        theirs = _issue(client, "Agent B task")
        for issue, agent in ((mine, agent_a), (theirs, agent_b)):
            response = client.put(
                f"/issues/{issue['id']}/assignee",
                json={"assignee_id": admin["id"]},
                headers=H_ADMIN,
            )
            assert response.status_code == 200, response.text
            _delegate(client, issue["id"], agent["id"])

        read_a = _token(client, agent_a["id"], ["read"], "read-a")
        read_b = _token(client, agent_b["id"], ["read"], "read-b")
        docs_only = _token(
            client, agent_a["id"], ["docs:write"], "docs-only"
        )

        assert client.get("/delegations/me").status_code == 401
        denied = client.get("/delegations/me", headers=docs_only)
        assert denied.status_code == 403
        assert denied.json()["detail"] == "token scope required: read"

        before = _domain_snapshot(db_path)
        inbox_a = client.get("/delegations/me", headers=read_a)
        inbox_b = client.get("/delegations/me", headers=read_b)
        after = _domain_snapshot(db_path)

        assert inbox_a.status_code == 200, inbox_a.text
        assert inbox_b.status_code == 200, inbox_b.text
        assert [item["issue"]["title"] for item in inbox_a.json()["items"]] == [
            "Agent A task"
        ]
        assert [item["issue"]["title"] for item in inbox_b.json()["items"]] == [
            "Agent B task"
        ]
        assert inbox_b.json()["items"][0]["warnings"] == ["empty_description"]
        body = inbox_a.json()
        assert body["subject"] == {
            "id": agent_a["id"],
            "name": "Agent A",
            "is_agent": True,
        }
        assert body["blocker_scope"] == "visible_to_subject"
        assert body["items"][0]["visible_to_subject"] is True
        assert before == after


def test_private_assignment_is_hidden_from_self_but_flagged_for_admin_and_web(
    tmp_path,
):
    db_path = tmp_path / "privacy.db"
    with TestClient(create_app(db_path)) as client:
        _, agent, _ = _bootstrap(client)
        visible = _issue(client, "Visible brief")
        _delegate(client, visible["id"], agent["id"])

        project = client.post(
            "/projects",
            json={"name": "Secret", "key": "SEC"},
            headers=H_ADMIN,
        ).json()
        hidden = _issue(
            client,
            "Secret brief",
            body="Private instructions.",
            project_id=project["id"],
        )
        _delegate(client, hidden["id"], agent["id"])
        response = client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H_ADMIN,
        )
        assert response.status_code == 200, response.text

        bearer = _token(client, agent["id"], ["read"], "agent-reader")
        self_inbox = client.get("/delegations/me", headers=bearer).json()
        assert [item["issue"]["title"] for item in self_inbox["items"]] == [
            "Visible brief"
        ]
        assert self_inbox["items"][0]["warnings"] == [
            "empty_description",
            "no_accountable_assignee",
        ]
        assert "Private instructions." not in str(self_inbox)

        _login(client, "agent-a@e.com")
        dashboard = client.get("/aegis/dashboard")
        assert dashboard.status_code == 200
        assert "Delegated to me" in dashboard.text
        assert "Visible brief" in dashboard.text
        assert "Secret brief" not in dashboard.text
        assert "No description" in dashboard.text
        assert "No accountable assignee" in dashboard.text
        assert "does not claim work or report agent liveness" in dashboard.text

        _login(client, "admin@e.com")
        admin_page = client.get("/admin/agents")
        assert admin_page.status_code == 200
        assert "Open Delegations" in admin_page.text
        assert "Secret brief" in admin_page.text
        assert "Agent cannot access this issue's project" in admin_page.text
        assert "Assignment state does not prove the agent is running" in admin_page.text

        granted = client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": agent["id"]},
            headers=H_ADMIN,
        )
        assert granted.status_code == 201, granted.text
        now_visible = client.get("/delegations/me", headers=bearer).json()
        assert {item["issue"]["title"] for item in now_visible["items"]} == {
            "Visible brief",
            "Secret brief",
        }
        assert all(item["visible_to_subject"] for item in now_visible["items"])


def test_self_projection_fails_closed_on_visibility_mismatch(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "privacy_mismatch.db"
    with TestClient(create_app(db_path)) as client:
        _, agent, _ = _bootstrap(client)
        issue = _issue(client, "Do not leak", body="Sensitive at serialization.")
        _delegate(client, issue["id"], agent["id"])
        bearer = _token(client, agent["id"], ["read"], "reader")

        # WHY: the read transaction keeps visibility and content on one snapshot, but
        # this final guard must still fail closed if those predicates ever drift.
        monkeypatch.setattr(delegations, "_visible_to", lambda *_args: False)
        response = client.get("/delegations/me", headers=bearer)
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        assert "Sensitive at serialization." not in response.text


def test_done_archive_order_pagination_bounds_and_legacy_priority(tmp_path):
    db_path = tmp_path / "lifecycle.db"
    with TestClient(create_app(db_path)) as client:
        _, agent, _ = _bootstrap(client)
        bearer = _token(client, agent["id"], ["read"], "reader")
        project = client.post(
            "/projects",
            json={"name": "Release", "key": "REL"},
            headers=H_ADMIN,
        ).json()
        status_response = client.post(
            f"/projects/{project['id']}/statuses",
            json={"name": "shipped", "category": "done"},
            headers=H_ADMIN,
        )
        assert status_response.status_code == 201, status_response.text

        closed = _issue(
            client,
            "Already shipped",
            project_id=project["id"],
            status="shipped",
        )
        archived = _issue(client, "Archived work", project_id=project["id"])
        for issue in (closed, archived):
            _delegate(client, issue["id"], agent["id"])
        archive_response = client.post(
            f"/issues/{archived['id']}/archive", headers=H_ADMIN
        )
        assert archive_response.status_code == 200, archive_response.text

        assert client.get("/delegations/me", headers=bearer).json()["items"] == []
        with_closed = client.get(
            "/delegations/me?include_closed=true", headers=bearer
        ).json()
        assert [item["issue"]["title"] for item in with_closed["items"]] == [
            "Already shipped"
        ]
        assert with_closed["items"][0]["issue"]["status_category"] == "done"

        specs = (
            ("Urgent old", "urgent", "2026-01-01 00:00:00"),
            ("Urgent new", "urgent", "2026-01-02 00:00:00"),
            ("High", "high", "2020-01-01 00:00:00"),
            ("Medium", "medium", "2020-01-01 00:00:00"),
            ("Low", "low", "2020-01-01 00:00:00"),
            ("Legacy", "medium", "2020-01-01 00:00:00"),
        )
        created: list[dict] = []
        for title, priority, _ in specs:
            issue = _issue(client, title, priority=priority)
            _delegate(client, issue["id"], agent["id"])
            created.append(issue)

        conn = db.connect(db_path)
        try:
            for issue, (_, _, delegated_at) in zip(created, specs, strict=True):
                conn.execute(
                    "UPDATE issue_contributors SET added_at = ? "
                    "WHERE issue_id = ? AND user_id = ?",
                    (delegated_at, issue["id"], agent["id"]),
                )
            conn.execute(
                "UPDATE issues SET priority = 'custom' WHERE id = ?",
                (created[-1]["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        first = client.get(
            "/delegations/me?limit=2&offset=0", headers=bearer
        ).json()
        assert [item["issue"]["title"] for item in first["items"]] == [
            "Urgent old",
            "Urgent new",
        ]
        assert first["has_more"] is True
        assert first["next_offset"] == 2

        middle = client.get(
            "/delegations/me?limit=3&offset=2", headers=bearer
        ).json()
        assert [item["issue"]["title"] for item in middle["items"]] == [
            "High",
            "Medium",
            "Low",
        ]
        assert middle["has_more"] is True
        assert middle["next_offset"] == 5

        last = client.get(
            "/delegations/me?limit=3&offset=5", headers=bearer
        ).json()
        assert [item["issue"]["title"] for item in last["items"]] == ["Legacy"]
        assert last["items"][0]["issue"]["priority"] == "custom"
        assert last["has_more"] is False
        assert last["next_offset"] is None

        for query in (
            "limit=0",
            "limit=101",
            "offset=-1",
            f"offset={delegations.MAX_OFFSET + 1}",
        ):
            assert (
                client.get(f"/delegations/me?{query}", headers=bearer).status_code
                == 422
            )
        assert (
            client.get(
                f"/delegations/me?offset={delegations.MAX_OFFSET}",
                headers=bearer,
            ).status_code
            == 200
        )


def test_payload_batches_labels_and_caps_only_subject_visible_open_blockers(
    tmp_path,
):
    db_path = tmp_path / "blockers.db"
    with TestClient(create_app(db_path)) as client:
        admin, agent, _ = _bootstrap(client)
        target = _issue(
            client,
            "Ship release",
            body="Run the release checklist.",
            priority="high",
        )
        assigned = client.put(
            f"/issues/{target['id']}/assignee",
            json={"assignee_id": admin["id"]},
            headers=H_ADMIN,
        )
        assert assigned.status_code == 200, assigned.text
        _delegate(client, target["id"], agent["id"])

        label = client.post(
            "/labels",
            json={"name": "release", "color": "#123abc"},
            headers=H_ADMIN,
        ).json()
        attached = client.post(
            f"/issues/{target['id']}/labels",
            json={"label_id": label["id"]},
            headers=H_ADMIN,
        )
        assert attached.status_code == 201, attached.text

        visible_blockers = [
            _issue(client, f"Visible blocker {number}")["id"]
            for number in range(11)
        ]
        done_project = client.post(
            "/projects",
            json={"name": "Resolved blockers", "key": "RES"},
            headers=H_ADMIN,
        ).json()
        done_status = client.post(
            f"/projects/{done_project['id']}/statuses",
            json={"name": "verified", "category": "done"},
            headers=H_ADMIN,
        )
        assert done_status.status_code == 201, done_status.text
        done_blocker = _issue(
            client,
            "Resolved blocker",
            project_id=done_project["id"],
            status="verified",
        )
        private_project = client.post(
            "/projects",
            json={"name": "Hidden blockers", "key": "HID"},
            headers=H_ADMIN,
        ).json()
        hidden_blocker = _issue(
            client,
            "Hidden blocker",
            project_id=private_project["id"],
        )
        conn = db.connect(db_path)
        try:
            for blocker_id in [
                *visible_blockers,
                done_blocker["id"],
                hidden_blocker["id"],
            ]:
                assert (
                    dependencies.add_link(
                        conn,
                        from_id=blocker_id,
                        to_id=target["id"],
                        relation="blocks",
                        created_by=admin["id"],
                    )
                    is None
                )
        finally:
            conn.close()
        made_private = client.put(
            f"/projects/{private_project['id']}/visibility",
            json={"visibility": "private"},
            headers=H_ADMIN,
        )
        assert made_private.status_code == 200, made_private.text

        bearer = _token(client, agent["id"], ["read"], "reader")
        response = client.get("/delegations/me", headers=bearer)
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]

        assert item["issue"]["body"] == "Run the release checklist."
        assert item["issue"]["assignee_name"] == "Admin"
        assert item["issue"]["labels"] == [
            {"id": label["id"], "name": "release", "color": "#123abc"}
        ]
        assert item["delegated_by"]["id"] == admin["id"]
        assert item["delegated_by"]["name"] == "Admin"
        assert item["visible_open_blocker_count"] == 11
        assert len(item["visible_open_blockers"]) == 10
        assert item["visible_open_blockers_truncated"] is True
        assert [row["id"] for row in item["visible_open_blockers"]] == (
            visible_blockers[:10]
        )
        assert hidden_blocker["id"] not in {
            row["id"] for row in item["visible_open_blockers"]
        }
        assert done_blocker["id"] not in {
            row["id"] for row in item["visible_open_blockers"]
        }
        assert item["warnings"] == ["visible_open_blockers"]
        assert "unblocked" not in item

        conn = db.connect(db_path)
        try:
            supervised = delegations.list_delegations(
                conn, agent, viewer=admin
            )
        finally:
            conn.close()
        supervised_item = supervised["items"][0]
        assert supervised["blocker_scope"] == "visible_to_subject"
        assert supervised_item["visible_open_blocker_count"] == 11
        assert hidden_blocker["id"] not in {
            row["id"] for row in supervised_item["visible_open_blockers"]
        }


class _GetRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, **kwargs):
        self.calls.append((path, kwargs))
        return httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("GET", f"http://athena.test{path}"),
        )


def test_mcp_client_is_a_thin_rest_client():
    transport = _GetRecorder()
    client = AthenaClient(client=transport)

    assert client.list_my_delegated_work() == {"ok": True}
    assert transport.calls.pop() == (
        "/delegations/me",
        {"params": {"limit": 50, "offset": 0}},
    )

    assert client.list_my_delegated_work(
        include_closed=True, limit=7, offset=9
    ) == {"ok": True}
    assert transport.calls.pop() == (
        "/delegations/me",
        {
            "params": {
                "limit": 7,
                "offset": 9,
                "include_closed": True,
            }
        },
    )


def test_fastmcp_delegation_tool_forwards_and_enforces_bounds():
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    class RecordingAthenaClient:
        def __init__(self):
            self.calls: list[dict] = []

        def list_my_delegated_work(self, **kwargs):
            self.calls.append(kwargs)
            return {"items": []}

    client = RecordingAthenaClient()
    server = build_server(client)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["list_my_delegated_work"].inputSchema["properties"]
    assert schema["limit"]["minimum"] == 1
    assert schema["limit"]["maximum"] == delegations.MAX_LIMIT
    assert schema["offset"]["minimum"] == 0
    assert schema["offset"]["maximum"] == delegations.MAX_OFFSET

    asyncio.run(
        server.call_tool(
            "list_my_delegated_work",
            {"include_closed": True, "limit": 7, "offset": 9},
        )
    )
    assert client.calls == [
        {"include_closed": True, "limit": 7, "offset": 9}
    ]

    for invalid in (
        {"limit": delegations.MAX_LIMIT + 1},
        {"offset": delegations.MAX_OFFSET + 1},
    ):
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool("list_my_delegated_work", invalid))
    assert len(client.calls) == 1
