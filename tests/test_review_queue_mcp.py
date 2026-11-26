"""Exact candidate identities cross one request, without client-side publication.

MockTransport supplies historical receipts and queue packets here. These controls
prove wire/schema behavior, not backend immutability, visibility or atomicity.
"""

import asyncio
import json

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from athena.core.ids import MAX_SQLITE_INTEGER
from athena.mcp.client import AthenaClient, AthenaError
from athena.mcp import server as mcp_server

GENERATION = "a" * 32
IDENTITY = {"kind": "git", "source": "owner/repository", "revision": "b" * 40}
SUBMISSION = {"identity": IDENTITY, "expected_previous_candidate_id": None}


def _call(client, method, arguments, *, mcp):
    if not mcp:
        return getattr(client, method)(**arguments)
    server = mcp_server.build_server(
        client,
        scopes=frozenset({"read", "issue:write"}),
        profile=mcp_server.PROFILES["conductor"],
    )
    return json.loads(asyncio.run(server.call_tool(method, arguments))[0][0].text)


@pytest.mark.parametrize("mcp", [False, True])
@pytest.mark.parametrize(
    "extension, receipt",
    [
        ({}, {"released": True, "issue_id": 7}),
        (
            {"candidate_submission": None, "review_candidate_id": None},
            {"released": True, "issue_id": 7},
        ),
        (
            {"candidate_submission": SUBMISSION},
            {"candidate_submission": {"candidate_id": 11, "completion_event_id": 42}},
        ),
        (
            {
                "candidate_submission": {
                    "identity": {**IDENTITY, "kind": "artifact", "revision": "c" * 64},
                    "expected_previous_candidate_id": MAX_SQLITE_INTEGER,
                }
            },
            {"candidate_submission": {"candidate_id": 12, "completion_event_id": 43}},
        ),
        (
            {"review_candidate_id": MAX_SQLITE_INTEGER},
            {
                "candidate_review": {
                    "review_id": 3,
                    "candidate_id": MAX_SQLITE_INTEGER,
                    "completion_event_id": 44,
                }
            },
        ),
    ],
)
@pytest.mark.parametrize("key", [None, "same-exact-completion"])
def test_completion_extensions_are_one_post_and_keep_nested_null(
    mcp, extension, receipt, key
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=receipt, headers={"Idempotent-Replay": "true"})

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(respond)
    ) as http:
        client = AthenaClient(
            client=http, run_id="review-run", parent_run_id="project-run"
        )
        args = {
            "issue_id": 7,
            "generation": GENERATION,
            "verdict": "HOLD",
            "unfinished": ["integration pending"],
            "idempotency_key": key,
            **extension,
        }
        # The transport marks every answer a replay; the client says so (F14).
        assert _call(client, "complete_claim", args, mcp=mcp) == {
            **receipt,
            "_replayed": True,
        }

    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == ("POST", "/issues/7/complete")
    assert request.url.query == b""
    assert json.loads(request.content) == {
        "generation": GENERATION,
        "verdict": "HOLD",
        "unfinished": ["integration pending"],
        **{name: value for name, value in extension.items() if value is not None},
    }
    assert request.headers.get("Idempotency-Key") == key
    assert request.headers["X-Athena-Run"] == "review-run"
    assert request.headers["X-Athena-Parent-Run"] == "project-run"
    assert "If-Match" not in request.headers


@pytest.mark.parametrize("mcp", [False, True])
@pytest.mark.parametrize("limit", [1, 5, 20])
@pytest.mark.parametrize("cursor", [None, MAX_SQLITE_INTEGER])
def test_review_queue_is_one_bounded_get_and_preserves_packet(mcp, limit, cursor):
    requests = []
    packet = {
        "schema": "athena.project_review_queue.v1",
        "items": [
            {"candidate": {"candidate_id": 11}, "review_state": "awaiting_review"}
        ],
        "visible_total": 22,
        "returned": 1,
        "limit": limit,
        "clipped": True,
        "next_cursor": 11,
    }

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=packet)

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(respond)
    ) as http:
        client = AthenaClient(client=http)
        args = {"project_id": MAX_SQLITE_INTEGER}
        if cursor is not None:
            args["cursor"] = cursor
        if limit != 5:
            args["limit"] = limit
        assert _call(client, "get_project_review_queue", args, mcp=mcp) == packet
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/projects/{MAX_SQLITE_INTEGER}/review-queue"
    expected = {"limit": str(limit)}
    if cursor is not None:
        expected["cursor"] = str(cursor)
    assert dict(request.url.params) == expected
    assert request.content == b""
    assert (
        "If-Match" not in request.headers and "Idempotency-Key" not in request.headers
    )


@pytest.mark.parametrize("mcp", [False, True])
@pytest.mark.parametrize("method", ["complete_claim", "get_project_review_queue"])
@pytest.mark.parametrize("status", [403, 404, 409, 422])
def test_refusals_remain_errors_without_fallback(mcp, method, status):
    requests = []

    def refuse(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"detail": "refused", "code": "synthetic_refusal"},
            headers={"Retry-After": "2"},
        )

    args = (
        {"issue_id": 7, "generation": GENERATION, "review_candidate_id": 11}
        if method == "complete_claim"
        else {"project_id": 7}
    )
    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(refuse)
    ) as http:
        with pytest.raises(ToolError if mcp else AthenaError) as caught:
            _call(AthenaClient(client=http), method, args, mcp=mcp)
    assert len(requests) == 1
    if mcp:
        payload = json.loads(str(caught.value).split("ATHENA_ERROR_JSON=", 1)[1])
    else:
        payload = caught.value.as_dict()
    assert payload["status_code"] == status
    assert payload["code"] == "synthetic_refusal"
    assert payload["retry_after"] == "2"


BAD_IDS = [False, 0, -1, MAX_SQLITE_INTEGER + 1, 1.5, "1"]


def test_sdk_explicit_null_queue_limit_keeps_the_existing_rest_refusal():
    def refuse(request):
        assert list(request.url.params.multi_items()) == [("limit", "")]
        return httpx.Response(422, json={"detail": "invalid limit"})

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(refuse)
    ) as http:
        with pytest.raises(AthenaError) as caught:
            AthenaClient(client=http).get_project_review_queue(1, limit=None)
    assert caught.value.status_code == 422


@pytest.mark.parametrize("cursor", [False, 1.0, "1"])
def test_sdk_rejects_noninteger_cursor_before_serializing(cursor):
    with pytest.raises(ValueError, match="cursor must be an integer"):
        AthenaClient(client=object()).get_project_review_queue(1, cursor=cursor)


BAD_SUBMISSIONS = [
    {},
    {"identity": IDENTITY},
    {**SUBMISSION, "extra": "no"},
    {**SUBMISSION, "identity": {**IDENTITY, "extra": "no"}},
    {**SUBMISSION, "identity": {**IDENTITY, "kind": "branch"}},
    {**SUBMISSION, "identity": {**IDENTITY, "revision": "main"}},
    {**SUBMISSION, "identity": {**IDENTITY, "revision": "B" * 40}},
    {**SUBMISSION, "identity": {**IDENTITY, "kind": "artifact"}},
    {**SUBMISSION, "identity": {**IDENTITY, "source": " "}},
    {**SUBMISSION, "identity": {**IDENTITY, "source": "x" * 201}},
    {**SUBMISSION, "identity": {**IDENTITY, "source": 1}},
    {**SUBMISSION, "identity": {**IDENTITY, "source": "bad\x01source"}},
    *[{**SUBMISSION, "expected_previous_candidate_id": value} for value in BAD_IDS],
]


@pytest.mark.parametrize(
    "method, arguments",
    [
        *[
            ("complete_claim", {"candidate_submission": value})
            for value in BAD_SUBMISSIONS
        ],
        *[("complete_claim", {"review_candidate_id": value}) for value in BAD_IDS],
        *[("get_project_review_queue", {"project_id": value}) for value in BAD_IDS],
        *[("get_project_review_queue", {"cursor": value}) for value in BAD_IDS],
        *[
            ("get_project_review_queue", {"limit": value})
            for value in [False, 0, -1, 21, 1.5, "5", None]
        ],
    ],
)
def test_invalid_mcp_extension_or_read_bounds_do_not_dispatch(method, arguments):
    requests = []

    def must_not_dispatch(request):
        requests.append(request)
        return httpx.Response(200, json={})

    base = (
        {"issue_id": 7, "generation": GENERATION}
        if method == "complete_claim"
        else {"project_id": 7}
    )
    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(must_not_dispatch)
    ) as http:
        with pytest.raises(ToolError, match="validation error"):
            _call(AthenaClient(client=http), method, {**base, **arguments}, mcp=True)
    assert requests == []


def test_queue_is_one_new_read_and_completion_keeps_its_authority():
    tools = {
        t.name: t for t in asyncio.run(mcp_server.build_server(object()).list_tools())
    }
    name = "get_project_review_queue"
    # F9: list_eligible_accounts; F10: get_issue_prior_text; A11: get_idempotency_key;
    # H6: get_issue_comment; decision 8: assign_issue_to_seat
    assert len(tools) == 147
    assert mcp_server.TOOL_SCOPES[name] == mcp_server.READ_ONLY
    assert mcp_server.TOOL_SCOPES["complete_claim"] == "issue:write"
    assert name in mcp_server.PROFILES["conductor"]
    assert name not in mcp_server.PROFILES["seat"]
    assert name not in mcp_server.PROFILES["seat+knowledge"]
    assert mcp_server.PROFILES["full"] is None
    schema = tools[name].inputSchema
    assert schema["required"] == ["project_id"]
    assert set(schema["properties"]) == {"project_id", "limit", "cursor"}
    assert schema["properties"]["limit"] == {
        "default": 5,
        "maximum": 20,
        "minimum": 1,
        "title": "Limit",
        "type": "integer",
    }
    complete = tools["complete_claim"].inputSchema
    assert {"candidate_submission", "review_candidate_id"} <= set(
        complete["properties"]
    )
    submission = complete["$defs"]["CandidateSubmission"]
    assert set(submission["required"]) == {"identity", "expected_previous_candidate_id"}
    assert submission["additionalProperties"] is False
    assert complete["$defs"]["CandidateIdentity"]["additionalProperties"] is False


@pytest.mark.parametrize("mcp", [False, True])
def test_mutually_exclusive_extensions_reach_one_authoritative_refusal(mcp):
    requests = []

    def refuse(request):
        requests.append(request)
        return httpx.Response(422, json={"detail": "extensions are mutually exclusive"})

    args = {
        "issue_id": 7,
        "generation": GENERATION,
        "candidate_submission": SUBMISSION,
        "review_candidate_id": 11,
    }
    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(refuse)
    ) as http:
        with pytest.raises(
            ToolError if mcp else AthenaError, match="mutually exclusive"
        ):
            _call(AthenaClient(client=http), "complete_claim", args, mcp=mcp)
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["candidate_submission"] == SUBMISSION
    assert body["review_candidate_id"] == 11


def test_mcp_uses_shared_source_normalization_and_accepts_full_git_sha256():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"released": True})

    submission = {
        "identity": {**IDENTITY, "source": " owner/repository ", "revision": "d" * 64},
        "expected_previous_candidate_id": 1,
    }
    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(respond)
    ) as http:
        _call(
            AthenaClient(client=http),
            "complete_claim",
            {
                "issue_id": 7,
                "generation": GENERATION,
                "candidate_submission": submission,
            },
            mcp=True,
        )
    assert len(requests) == 1
    actual = json.loads(requests[0].content)["candidate_submission"]
    assert actual == {
        "identity": {"kind": "git", "source": "owner/repository", "revision": "d" * 64},
        "expected_previous_candidate_id": 1,
    }


def test_rejected_mcp_candidate_source_stays_out_of_tool_error():
    # A short, synthetic credential-shaped source would fit entirely in a
    # Pydantic validation message. It must never be copied to the tool transcript.
    source = "https://demo:marker@invalid"
    submission = {**SUBMISSION, "identity": {**IDENTITY, "source": source}}
    with pytest.raises(ToolError, match="validation error") as caught:
        _call(
            object(),
            "complete_claim",
            {
                "issue_id": 7,
                "generation": GENERATION,
                "candidate_submission": submission,
            },
            mcp=True,
        )
    assert source not in str(caught.value)
