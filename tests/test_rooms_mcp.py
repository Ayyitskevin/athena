"""MCP coverage proving Rooms tools stay thin REST clients."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
import httpx
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from athena.aegis.rooms_api import (
    RoomBriefOut,
    RoomContextOut,
    RoomDetailOut,
    RoomListPageOut,
    TimelineItemOut,
    TimelineOut,
)
from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server


class _RecordingTransport:
    """HTTP-shaped transport that records every AthenaClient request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.headers = httpx.Headers()

    def _response(self, method: str, path: str, **kwargs) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        return httpx.Response(
            200,
            json={"method": method, "path": path},
            request=httpx.Request(method, f"http://athena.test{path}"),
        )

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._response("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self._response("POST", path, **kwargs)


def _call(server, tool_name: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(tool_name, arguments))
    return json.loads(result[0].text)


def _actor(actor_id: int) -> dict[str, str]:
    return {"X-Athena-Actor": str(actor_id)}


def _tool_error_payload(error: ToolError) -> dict:
    marker = "ATHENA_ERROR_JSON="
    assert marker in str(error)
    return json.loads(str(error).split(marker, 1)[1])


def test_room_tools_delegate_to_exact_rest_requests():
    transport = _RecordingTransport()
    server = build_server(AthenaClient(client=transport))

    _call(
        server,
        "list_rooms",
        {
            "project_id": 7,
            "include_archived": True,
            "cursor": "room-list-cursor",
            "limit": 2,
        },
    )
    _call(server, "get_room", {"room_id": 11})
    _call(
        server,
        "get_room_timeline",
        {"room_id": 11, "cursor": "timeline-cursor", "limit": 3},
    )
    _call(
        server,
        "post_room_event",
        {
            "room_id": 11,
            "event_kind": "evidence",
            "body": "recorded through REST",
            "reference_kind": "issue",
            "reference_id": 23,
            "supersedes_event_id": 29,
            "idempotency_key": "room-event-key",
        },
    )
    _call(
        server,
        "get_room_context",
        {"room_id": 11, "question": "what changed?", "limit": 4},
    )
    _call(
        server,
        "get_room_brief",
        {"room_id": 11, "cursor": "brief-cursor"},
    )

    assert transport.calls[:3] == [
        (
            "GET",
            "/projects/7/rooms",
            {
                "params": {
                    "limit": 2,
                    "include_archived": True,
                    "cursor": "room-list-cursor",
                }
            },
        ),
        ("GET", "/rooms/11", {}),
        (
            "GET",
            "/rooms/11/timeline",
            {"params": {"cursor": "timeline-cursor", "limit": 3}},
        ),
    ]
    event_call = transport.calls[3]
    assert event_call[:2] == ("POST", "/rooms/11/events")
    assert event_call[2]["json"] == {
        "event_kind": "evidence",
        "body": "recorded through REST",
        "reference_kind": "issue",
        "reference_id": 23,
        "supersedes_event_id": 29,
    }
    assert event_call[2]["headers"]["Idempotency-Key"] == "room-event-key"
    assert transport.calls[4:] == [
        (
            "POST",
            "/rooms/11/context",
            {"json": {"question": "what changed?", "limit": 4}},
        ),
        (
            "GET",
            "/rooms/11/brief",
            {"params": {"cursor": "brief-cursor"}},
        ),
    ]


def test_room_tool_json_schemas_forbid_additional_properties():
    server = build_server(AthenaClient(client=_RecordingTransport()))
    room_tools = {
        "list_rooms",
        "get_room",
        "get_room_timeline",
        "post_room_event",
        "get_room_context",
        "get_room_brief",
    }
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for tool_name in room_tools:
        assert tools[tool_name].inputSchema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_room", {"room_id": 11, "unexpected": True}),
        ("get_room", {"room_id": True}),
        ("list_rooms", {"project_id": 7, "limit": "2"}),
        ("get_room_timeline", {"room_id": 11, "cursor": "x" * 161}),
        ("get_room_context", {"room_id": 11, "question": ""}),
        (
            "post_room_event",
            {"room_id": 11, "event_kind": "dispatch", "body": "never sent"},
        ),
    ],
)
def test_room_tool_schemas_reject_invalid_input_before_http(
    tool_name: str,
    arguments: dict,
):
    transport = _RecordingTransport()
    server = build_server(AthenaClient(client=transport))

    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(tool_name, arguments))
    assert transport.calls == []


def test_room_tools_traverse_real_http_with_cursors_receipts_and_bounds(
    tmp_path: Path,
):
    db_file = tmp_path / "rooms-mcp.db"
    with TestClient(create_app(db_file)) as test_client:
        admin_response = test_client.post(
            "/users",
            json={
                "email": "admin@example.com",
                "name": "Admin",
                "password": "pw",
                "role": "admin",
            },
        )
        assert admin_response.status_code == 201
        admin = admin_response.json()
        agent_response = test_client.post(
            "/users",
            json={
                "email": "room-agent@example.com",
                "name": "Room Agent",
                "password": "pw",
                "is_agent": True,
            },
            headers=_actor(admin["id"]),
        )
        assert agent_response.status_code == 201
        agent = agent_response.json()

        token_response = test_client.post(
            "/tokens",
            json={"name": "rooms mcp", "scopes": ["rooms:write"]},
            headers=_actor(agent["id"]),
        )
        assert token_response.status_code == 201, token_response.text
        token = token_response.json()

        projects = []
        for name, key in (("MCP One", "MONE"), ("MCP Two", "MTWO")):
            response = test_client.post(
                "/projects",
                json={"name": name, "key": key, "description": "MCP contract"},
                headers=_actor(admin["id"]),
            )
            assert response.status_code == 201
            projects.append(response.json())
        issue_response = test_client.post(
            "/issues",
            json={
                "title": "ORBIT MCP evidence",
                "body": "ORBIT evidence available through room context",
                "project_id": projects[0]["id"],
            },
            headers=_actor(admin["id"]),
        )
        assert issue_response.status_code == 201
        issue = issue_response.json()

        run_id = "mcp room/run with spaces"
        test_client.headers["Authorization"] = f"Bearer {token['token']}"
        athena = AthenaClient(client=test_client, run_id=run_id)
        server = build_server(athena)

        first_page = _call(
            server,
            "list_rooms",
            {"project_id": projects[0]["id"], "limit": 1},
        )
        RoomListPageOut.model_validate(first_page)
        assert first_page["has_more"] is True
        assert first_page["next_cursor"]
        continued = _call(
            server,
            "list_rooms",
            {
                "project_id": projects[0]["id"],
                "limit": 1,
                "cursor": first_page["next_cursor"],
            },
        )
        assert continued["items"][0]["id"] != first_page["items"][0]["id"]

        with pytest.raises(ToolError) as malformed_list:
            _call(
                server,
                "list_rooms",
                {"project_id": projects[0]["id"], "cursor": "malformed"},
            )
        assert _tool_error_payload(malformed_list.value)["status_code"] == 422
        with pytest.raises(ToolError) as cross_project:
            _call(
                server,
                "list_rooms",
                {
                    "project_id": projects[1]["id"],
                    "cursor": first_page["next_cursor"],
                },
            )
        assert _tool_error_payload(cross_project.value)["status_code"] == 422

        all_rooms = _call(
            server,
            "list_rooms",
            {"project_id": projects[0]["id"], "limit": 50},
        )
        work_room = next(
            room for room in all_rooms["items"] if room["room_type"] == "work_item"
        )
        project_room = next(
            room for room in all_rooms["items"] if room["room_type"] == "project"
        )

        detail = _call(server, "get_room", {"room_id": work_room["id"]})
        RoomDetailOut.model_validate(detail)
        assert detail["room"]["project_id"] == projects[0]["id"]

        event_arguments = {
            "room_id": work_room["id"],
            "event_kind": "evidence",
            "body": "ORBIT evidence posted through MCP",
            "reference_kind": "issue",
            "reference_id": issue["id"],
            "idempotency_key": "mcp-room-event-once",
        }
        first_event = _call(server, "post_room_event", event_arguments)
        replay = _call(server, "post_room_event", event_arguments)
        assert replay == first_event
        TimelineItemOut.model_validate(first_event)
        assert first_event["actor"] == {
            "id": agent["id"],
            "name": "Room Agent",
            "is_agent": True,
        }
        assert first_event["run_id"] == run_id
        expected_receipt = f"/activity/runs/{quote(run_id, safe='')}/lineage"
        assert first_event["run_receipt"] == expected_receipt

        second_event = _call(
            server,
            "post_room_event",
            {
                "room_id": work_room["id"],
                "event_kind": "message",
                "body": "second event for timeline paging",
                "idempotency_key": "mcp-room-event-two",
            },
        )
        assert second_event["activity_id"] != first_event["activity_id"]

        timeline = _call(
            server,
            "get_room_timeline",
            {"room_id": work_room["id"], "limit": 1},
        )
        TimelineOut.model_validate(timeline)
        assert timeline["page"]["has_more"] is True
        assert timeline["page"]["next_cursor"]
        older = _call(
            server,
            "get_room_timeline",
            {
                "room_id": work_room["id"],
                "limit": 1,
                "cursor": timeline["page"]["next_cursor"],
            },
        )
        assert older["items"][0]["activity_id"] < timeline["items"][0]["activity_id"]
        assert older["items"][0]["run_receipt"] == expected_receipt
        with pytest.raises(ToolError) as malformed_timeline:
            _call(
                server,
                "get_room_timeline",
                {"room_id": work_room["id"], "cursor": "malformed"},
            )
        assert _tool_error_payload(malformed_timeline.value)["status_code"] == 422

        context = _call(
            server,
            "get_room_context",
            {
                "room_id": work_room["id"],
                "question": "Where is the ORBIT evidence?",
                "limit": 1,
            },
        )
        RoomContextOut.model_validate(context)
        assert context["schema"] == "athena.room-context.v1"
        assert context["bounds"]["selection_limit"] == 1
        assert context["bounds"]["selected_count"] <= 1
        assert len(context["records"]) <= 1

        brief = _call(
            server,
            "get_room_brief",
            {"room_id": project_room["id"]},
        )
        RoomBriefOut.model_validate(brief)
        assert brief["schema"] == "athena.room-brief.v1"

        conn = db.connect(db_file)
        try:
            rows = conn.execute(
                "SELECT a.actor_id, a.run_id, a.delivery_eligible "
                "FROM room_events re JOIN activity a ON a.id = re.activity_id "
                "WHERE re.room_id = ? AND a.detail = ?",
                (work_room["id"], event_arguments["body"]),
            ).fetchall()
        finally:
            conn.close()
        assert [tuple(row) for row in rows] == [(agent["id"], run_id, 0)]
