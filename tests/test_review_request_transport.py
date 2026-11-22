"""A conductor sends one bounded request through the existing SDK mutation path."""

import asyncio
import json

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from athena.mcp import server
from athena.mcp.client import AthenaClient


def test_conductor_requests_one_exact_review_with_retry_key_and_run():
    calls = []
    receipt = {
        "request_id": 5,
        "review_issue_id": 12,
        "candidate_id": 4,
        "reviewer_id": 8,
    }

    def receive(request):
        calls.append(request)
        return httpx.Response(200, json=receipt)

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(receive)
    ) as http:
        client = AthenaClient(client=http, run_id="conductor-run")
        mcp = server.build_server(
            client,
            scopes=frozenset({"read", "issue:write"}),
            profile=server.CONDUCTOR_PROFILE,
        )
        response = asyncio.run(
            mcp.call_tool(
                "request_candidate_review",
                {
                    "review_issue_id": 12,
                    "candidate_id": 4,
                    "reviewer_id": 8,
                    "idempotency_key": "one-request",
                },
            )
        )
    assert json.loads(response[0][0].text) == receipt
    assert len(calls) == 1
    req = calls[0]
    assert (req.method, req.url.path) == ("POST", "/issues/12/review-requests")
    assert json.loads(req.content) == {"candidate_id": 4, "reviewer_id": 8}
    assert req.headers["Idempotency-Key"] == "one-request"
    assert req.headers["X-Athena-Run"] == "conductor-run"


@pytest.mark.parametrize("field", ["candidate_id", "review_issue_id", "reviewer_id"])
@pytest.mark.parametrize("value", [True, "7", 0, 2**63])
def test_request_ids_are_validated_before_dispatch(field, value):
    args = {"review_issue_id": 12, "candidate_id": 4, "reviewer_id": 8, field: value}
    mcp = server.build_server(
        object(),
        scopes=frozenset({"read", "issue:write"}),
        profile=server.CONDUCTOR_PROFILE,
    )
    with pytest.raises(ToolError, match="validation error"):
        asyncio.run(mcp.call_tool("request_candidate_review", args))
