"""The compact project read crosses one GET without gaining write authority.

These transport controls use MockTransport, not a substitute briefing reader.
Backend visibility and packet semantics belong in the real project-brief suite.
"""

import asyncio
import json

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from athena.core.ids import MAX_SQLITE_INTEGER
from athena.mcp.client import AthenaClient, AthenaError
from athena.mcp import server as mcp_server


@pytest.mark.parametrize(
    "arguments, expected_limit", [({}, 5), ({"limit": 1}, 1), ({"limit": 20}, 20)]
)
@pytest.mark.parametrize("through_mcp", [False, True])
def test_project_brief_is_one_get_with_the_requested_section_limit(
    arguments, expected_limit, through_mcp
):
    requests = []
    packet = {"schema": "athena.project_brief.v1", "project": {"id": 7}}

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=packet)

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(respond)
    ) as http:
        client = AthenaClient(client=http)
        if through_mcp:
            server = mcp_server.build_server(
                client,
                scopes=frozenset({"read"}),
                profile=mcp_server.PROFILES["conductor"],
            )
            content = asyncio.run(
                server.call_tool("get_project_brief", {"project_id": 7, **arguments})
            )
            result = json.loads(content[0].text)
        else:
            result = client.get_project_brief(7, **arguments)

    assert result == packet
    assert "_etag" not in result
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/projects/7/brief"
    assert list(request.url.params.multi_items()) == [("limit", str(expected_limit))]
    assert request.content == b""
    assert "if-match" not in request.headers
    assert "idempotency-key" not in request.headers


@pytest.mark.parametrize("status", [401, 404, 422])
def test_project_brief_preserves_http_refusal_instead_of_inventing_an_empty_packet(
    status,
):
    requests = []

    def refuse(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": "unavailable"})

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(refuse)
    ) as http:
        client = AthenaClient(client=http)
        with pytest.raises(AthenaError) as caught:
            client.get_project_brief(7)
    assert len(requests) == 1
    assert caught.value.status_code == status
    assert caught.value.method == "GET"
    assert caught.value.path == "/projects/7/brief"


@pytest.mark.parametrize(
    "field, invalid",
    [
        ("project_id", 0),
        ("project_id", -1),
        ("project_id", MAX_SQLITE_INTEGER + 1),
        ("project_id", True),
        ("project_id", 1.5),
        ("project_id", "7"),
        ("limit", 0),
        ("limit", -1),
        ("limit", 21),
        ("limit", True),
        ("limit", 1.5),
        ("limit", "5"),
        ("limit", None),
    ],
)
def test_project_brief_rejects_invalid_mcp_bounds_before_http(field, invalid):
    requests = []

    def must_not_dispatch(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(must_not_dispatch)
    ) as http:
        server = mcp_server.build_server(AthenaClient(client=http))
        with pytest.raises(ToolError, match="validation error"):
            asyncio.run(
                server.call_tool("get_project_brief", {"project_id": 7, field: invalid})
            )
    assert requests == []


def test_project_brief_schema_and_profile_are_a_bounded_project_read():
    server = mcp_server.build_server(object(), scopes=frozenset({"read"}))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    tool = tools["get_project_brief"]
    assert mcp_server.TOOL_SCOPES[tool.name] == mcp_server.READ_ONLY
    assert tool.name in mcp_server.PROFILES["conductor"]
    assert tool.name not in mcp_server.PROFILES["seat"]
    assert tool.name not in mcp_server.PROFILES["seat+knowledge"]
    assert mcp_server.PROFILES["full"] is None
    schema = tool.inputSchema
    assert schema["required"] == ["project_id"]
    assert set(schema["properties"]) == {"project_id", "limit"}
    project = schema["properties"]["project_id"]
    assert (project["minimum"], project["maximum"]) == (1, MAX_SQLITE_INTEGER)
    limit = schema["properties"]["limit"]
    assert (limit["default"], limit["minimum"], limit["maximum"]) == (5, 1, 20)


def test_project_brief_accepts_the_upper_project_id_boundary():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(respond)
    ) as http:
        server = mcp_server.build_server(AthenaClient(client=http))
        asyncio.run(
            server.call_tool("get_project_brief", {"project_id": MAX_SQLITE_INTEGER})
        )
    assert len(requests) == 1
    assert requests[0].url.path == f"/projects/{MAX_SQLITE_INTEGER}/brief"
