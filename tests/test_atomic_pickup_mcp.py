"""Atomic pickup crosses one POST; these are transport/schema controls only.

MockTransport deliberately supplies the receipt. Real selection, acquisition,
concurrency and durable replay belong to the backend and installed smoke suites.
"""

import asyncio
import json

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from athena.core.ids import MAX_SQLITE_INTEGER
from athena.mcp.client import AthenaClient, AthenaError
from athena.mcp import server as mcp_server


@pytest.mark.parametrize("through_mcp", [False, True])
@pytest.mark.parametrize(
    "arguments, expected_body, expected_key",
    [
        ({}, {"limit": 1}, None),
        (
            {
                "limit": 5,
                "lease_seconds": 60,
                "project_id": 7,
                "idempotency_key": "pick-1",
            },
            {"limit": 5, "lease_seconds": 60, "project_id": 7},
            "pick-1",
        ),
        (
            {"lease_seconds": 86400, "project_id": MAX_SQLITE_INTEGER},
            {"limit": 1, "lease_seconds": 86400, "project_id": MAX_SQLITE_INTEGER},
            None,
        ),
        (
            {"project_id": None, "lease_seconds": None, "idempotency_key": None},
            {"limit": 1},
            None,
        ),
    ],
)
def test_pickup_is_one_post_with_explicit_arguments_and_run(
    through_mcp, arguments, expected_body, expected_key
):
    requests = []
    receipt = {
        "schema": "athena.ready_claim.v1",
        "observed_at": "2035-01-01T00:00:00+00:00",
        "claimed": True,
        "issue": {"id": 7},
        "lease": {"generation": "a" * 32, "active": True},
        "issue_etag": '"root"',
        "work_context_etag": '"context"',
        "previous_lease": None,
        "open_handoff": None,
        "attempts": [],
    }

    def respond(request):
        requests.append(request)
        assert (request.method, request.url.path) == ("POST", "/issues/claim-next")
        return httpx.Response(200, json=receipt, headers={"Idempotent-Replay": "true"})

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(respond)
    ) as http:
        client = AthenaClient(client=http, run_id="worker-1", parent_run_id="goal-1")
        if through_mcp:
            server = mcp_server.build_server(
                client,
                scopes=frozenset({"issue:write"}),
                profile=mcp_server.PROFILES["seat"],
            )
            content, _ = asyncio.run(server.call_tool("claim_next_ready", arguments))
            result = json.loads(content[0].text)
        else:
            result = client.claim_next_ready(**arguments)

    # Historical receipt bytes pass through; no fresh context read or renewal.
    assert result == receipt
    assert len(requests) == 1
    request = requests[0]
    assert request.url.query == b""
    assert json.loads(request.content) == expected_body
    assert request.headers.get("Idempotency-Key") == expected_key
    assert request.headers["X-Athena-Run"] == "worker-1"
    assert request.headers["X-Athena-Parent-Run"] == "goal-1"
    assert "If-Match" not in request.headers


@pytest.mark.parametrize("through_mcp", [False, True])
@pytest.mark.parametrize(
    "status, code",
    [
        (401, None),
        (403, None),
        (404, None),
        (409, "idempotency_mismatch"),
        (409, "idempotency_in_progress"),
        (409, "idempotency_indeterminate"),
        (409, "idempotency_authorization_changed"),
        (422, None),
        (500, None),
    ],
)
def test_pickup_propagates_http_refusals_without_fallback_or_fresh_key(
    status, code, through_mcp
):
    requests = []

    def refuse(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"detail": "refused", "code": code},
            headers={"Retry-After": "3"},
        )

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(refuse)
    ) as http:
        client = AthenaClient(client=http)
        if through_mcp:
            server = mcp_server.build_server(client)
            with pytest.raises(ToolError) as caught:
                asyncio.run(
                    server.call_tool(
                        "claim_next_ready", {"idempotency_key": "same-key"}
                    )
                )
            error = json.loads(str(caught.value).split("ATHENA_ERROR_JSON=", 1)[1])
        else:
            with pytest.raises(AthenaError) as caught:
                client.claim_next_ready(idempotency_key="same-key")
            error = caught.value.as_dict()
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == "same-key"
    assert error["status_code"] == status
    assert error["code"] == code
    assert error["retry_after"] == "3"
    assert error["path"] == "/issues/claim-next"


@pytest.mark.parametrize("invalid", [True, False, 0, 6, 1.5, "1", None])
def test_sdk_pickup_limit_is_refused_before_dispatch(invalid):
    with httpx.Client(
        base_url="http://athena.test",
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP")),
    ) as http:
        with pytest.raises(ValueError, match="limit must be between 1 and 5"):
            AthenaClient(client=http).claim_next_ready(limit=invalid)


@pytest.mark.parametrize(
    "field, invalid",
    [
        ("limit", 0),
        ("limit", 6),
        ("limit", True),
        ("limit", 1.5),
        ("limit", "1"),
        ("limit", None),
        ("project_id", 0),
        ("project_id", -1),
        ("project_id", MAX_SQLITE_INTEGER + 1),
        ("project_id", True),
        ("project_id", 1.5),
        ("project_id", "7"),
        ("lease_seconds", 59),
        ("lease_seconds", 86401),
        ("lease_seconds", True),
        ("lease_seconds", 60.5),
        ("lease_seconds", "60"),
        ("idempotency_key", ""),
        ("idempotency_key", "with space"),
        ("idempotency_key", "x" * 256),
        ("idempotency_key", "\u00e9"),
    ],
)
def test_pickup_mcp_refuses_invalid_inputs_before_http(field, invalid):
    with httpx.Client(
        base_url="http://athena.test",
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP")),
    ) as http:
        server = mcp_server.build_server(AthenaClient(client=http))
        with pytest.raises(ToolError, match="validation error"):
            asyncio.run(server.call_tool("claim_next_ready", {field: invalid}))


def test_pickup_mcp_keeps_existing_scope_profiles_and_has_no_claim_precondition():
    for profile in mcp_server.PROFILES.values():
        server = mcp_server.build_server(
            object(), scopes=frozenset({"issue:write"}), profile=profile
        )
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        tool = tools["claim_next_ready"]
        assert mcp_server.TOOL_SCOPES[tool.name] == "issue:write"
        schema = tool.inputSchema
        assert set(schema["properties"]) == {
            "limit",
            "lease_seconds",
            "project_id",
            "idempotency_key",
        }
        assert not schema.get("required")
        assert "If-Match" not in schema["properties"]
        assert mcp_server.RETRY_POINTER in tool.description
        assert "historical" in tool.description
    for scopes in ({"read"}, {"docs:write"}):
        server = mcp_server.build_server(object(), scopes=frozenset(scopes))
        assert "claim_next_ready" not in {
            tool.name for tool in asyncio.run(server.list_tools())
        }
