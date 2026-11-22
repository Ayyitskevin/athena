"""Every MCP tool call answers exactly one JSON object (A4).

WHY: an agent parses what a tool returns. Before A4 a list arrived as one
unlabelled content block per row, an empty list as zero blocks with ``isError``
false (indistinguishable from a dropped answer), a 204 write as nothing at all,
and a bad argument as a paragraph of prose. None of them said whether a list
was complete.

The contract pinned here, over the protocol and in-process:

* success is one text block holding one JSON object, and the same object as
  structured content matching the tool's advertised output schema;
* a list is ``{items, count, truncated, next}``; ``next`` continues a truncated
  list with the paging the tool already has, and is null otherwise;
* a REST answer with no body is ``{"applied": true}`` (a write) or
  ``{"found": false}`` (a read), never zero blocks;
* a failure that never reached Athena is one object too, with ``isError`` true,
  a ``detail`` and an MCP-side ``code``;
* a tool that already answered a dict answers the same dict.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
import httpx
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from athena.core import refusal_codes
from athena.main import create_app
from athena.mcp import results
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}
LIST_KEYS = {"items", "count", "truncated", "next"}


def _wire(server, calls: list[tuple[str, dict]]):
    """Call tools the way a client does: over the protocol, in one session."""

    async def run():
        async with create_connected_server_and_client_session(
            server._mcp_server  # noqa: SLF001 — the protocol server under test
        ) as session:
            return [await session.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


def _one(result) -> dict:
    """The single JSON object a result carries; also checks the structured copy."""
    assert len(result.content) == 1, result.content
    assert result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    assert isinstance(payload, dict), payload
    if not result.isError:
        assert result.structuredContent == payload
    return payload


@pytest.fixture
def app_client(tmp_path):
    with TestClient(create_app(tmp_path / "shape.db")) as client:
        client.post(
            "/users", json={"email": "a@e.com", "name": "Ann", "password": "secret"}
        )
        client.headers.update(H1)
        yield client


def test_a_list_is_one_object_with_its_four_keys(app_client):
    for title in ("one", "two", "three"):
        app_client.post("/issues", json={"title": title})
    server = mcp_server.build_server(AthenaClient(client=app_client))
    first, rest, everything = _wire(
        server,
        [
            ("list_issues", {"limit": 2}),
            ("list_issues", {"limit": 2, "offset": 2}),
            ("list_issues", {}),
        ],
    )
    page = _one(first)
    assert set(page) == LIST_KEYS
    assert page["count"] == len(page["items"]) == 2
    # A full page may have more behind it: truncated, and next continues it.
    assert page["truncated"] is True
    assert page["next"] == {"offset": 2}
    tail = _one(rest)
    assert tail["count"] == 1
    assert tail["truncated"] is False and tail["next"] is None
    whole = _one(everything)
    assert [row["title"] for row in whole["items"]] == [
        row["title"] for row in page["items"] + tail["items"]
    ]
    # The rows are the REST rows as summaries: everything but the body (A6).
    rest = app_client.get("/issues").json()
    assert whole["items"] == [
        {key: value for key, value in row.items() if key != "body"} for row in rest
    ]


def test_an_empty_list_is_the_same_object_with_no_items(app_client):
    issue = app_client.post("/issues", json={"title": "childless"}).json()
    server = mcp_server.build_server(AthenaClient(client=app_client))
    (result,) = _wire(server, [("list_subtasks", {"issue_id": issue["id"]})])
    assert result.isError is False
    assert _one(result) == {"items": [], "count": 0, "truncated": False, "next": None}


def test_a_write_with_no_body_and_a_read_of_null_are_small_objects(app_client):
    issue = app_client.post("/issues", json={"title": "watched"}).json()
    # The REST answers really are empty: a 204, and a JSON null.
    server = mcp_server.build_server(AthenaClient(client=app_client))
    watched, lease = _wire(
        server,
        [
            ("watch", {"target_kind": "issue", "target_id": issue["id"]}),
            ("get_issue_lease", {"issue_id": issue["id"]}),
        ],
    )
    assert _one(watched) == {"applied": True}
    assert _one(lease) == {"found": False}
    assert app_client.get(f"/issues/{issue['id']}/lease").json() is None
    assert app_client.delete(f"/watches/issue/{issue['id']}").status_code == 204


def test_a_dict_answer_keeps_its_fields(app_client):
    issue = app_client.post("/issues", json={"title": "as it was"}).json()
    server = mcp_server.build_server(AthenaClient(client=app_client))
    (result,) = _wire(server, [("get_issue", {"issue_id": issue["id"]})])
    rest = app_client.get(f"/issues/{issue['id']}")
    assert _one(result) == {**rest.json(), "_etag": rest.headers["ETag"]}


def test_in_process_call_tool_answers_the_same_single_object(app_client):
    app_client.post("/issues", json={"title": "in process"})
    server = mcp_server.build_server(AthenaClient(client=app_client))
    content, structured = asyncio.run(server.call_tool("list_issues", {}))
    assert len(content) == 1
    assert json.loads(content[0].text) == structured
    assert set(structured) == LIST_KEYS


def test_a_bad_argument_is_one_object_that_names_the_argument_not_its_value():
    server = mcp_server.build_server(object())
    secret = "ath_" + "s" * 40
    bad, missing, unknown = _wire(
        server,
        [
            ("get_issue", {"issue_id": secret}),
            ("comment_on_issue", {"issue_id": 1}),
            ("no_such_tool", {}),
        ],
    )
    assert bad.isError is True
    failure = _one(bad)
    assert failure["code"] == "invalid_arguments"
    assert failure["status_code"] is None
    assert "issue_id" in failure["detail"]
    assert secret not in bad.content[0].text  # the rejected value is not echoed
    assert _one(missing)["detail"] == "invalid arguments: body: Field required"
    assert _one(unknown) == {
        "detail": "Unknown tool: no_such_tool",
        "code": "unknown_tool",
        "status_code": None,
    }


def test_a_tool_body_check_is_one_object_with_the_same_code():
    sent = []
    server = mcp_server.build_server(_mocked(sent.append))
    (result,) = _wire(server, [("get_issue", {})])
    assert sent == []
    assert result.isError is True
    assert _one(result) == {
        "detail": "pass exactly one of ref or issue_id",
        "code": "invalid_arguments",
        "status_code": None,
    }


def _mocked(respond) -> AthenaClient:
    return AthenaClient(
        client=httpx.Client(
            base_url="http://athena.test", transport=httpx.MockTransport(respond)
        )
    )


def test_a_client_side_check_after_athena_answered_carries_its_code_and_next():
    # The claim answered 201 but did not echo the requested root: the MCP client
    # refuses to call that success, and says what to read next.
    def respond(request):
        return httpx.Response(201, json={"generation": "0" * 32})

    server = mcp_server.build_server(_mocked(respond))
    (result,) = _wire(
        server,
        [
            (
                "claim_issue",
                {
                    "issue_id": 7,
                    "if_match": '"tag"',
                    "coordination_root": {
                        "root_id": "main",
                        "catalog_sha256": "a" * 64,
                    },
                },
            )
        ],
    )
    assert result.isError is True
    failure = _one(result)
    assert failure["code"] == "coordination_root_unconfirmed"
    assert failure["status_code"] is None
    assert failure["next"] == refusal_codes.next_call("coordination_root_unconfirmed")
    assert "may already exist" in failure["detail"]


def test_no_answer_from_athena_is_one_object_saying_so():
    def respond(request):
        raise httpx.ConnectError("refused", request=request)

    server = mcp_server.build_server(_mocked(respond))
    (result,) = _wire(server, [("get_issue", {"issue_id": 1})])
    assert result.isError is True
    assert _one(result) == {
        "detail": "no answer from Athena (ConnectError)",
        "code": "athena_no_response",
        "status_code": None,
    }


def test_an_answer_of_the_wrong_shape_is_a_tool_failure_not_a_success():
    def respond(request):
        return httpx.Response(200, json=[1, 2])

    server = mcp_server.build_server(_mocked(respond))
    (result,) = _wire(server, [("get_issue", {"issue_id": 1})])
    assert result.isError is True
    failure = _one(result)
    assert failure["code"] == "tool_failed"
    assert "declared result shape" in failure["detail"]


def test_in_process_failures_raise_and_tool_failure_gives_the_wire_object():
    server = mcp_server.build_server(_mocked(lambda request: None))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("get_issue", {}))
    (wire,) = _wire(server, [("get_issue", {})])
    assert mcp_server.tool_failure(caught.value) == _one(wire)


def _bare_list() -> list:
    """A list that forgot its envelope."""
    return []


def test_a_tool_that_returns_a_bare_list_refuses_to_build():
    with pytest.raises(RuntimeError, match="ListResult"):
        mcp_server._one_object(  # noqa: SLF001 — the registration guard itself
            _bare_list, read_only=True
        )


def test_limit_only_routes_page_by_offset_within_their_cap():
    rows = list(range(7))

    def fetch(limit):
        return rows[:limit]

    first = results.window(fetch, limit=3, offset=0, cap=6)
    assert first == {
        "items": [0, 1, 2],
        "count": 3,
        "truncated": True,
        "next": {"offset": 3},
    }
    last = results.window(fetch, limit=3, offset=3, cap=6)
    # The cap is reached: more rows exist, but this route cannot give them.
    assert last == {"items": [3, 4, 5], "count": 3, "truncated": True, "next": None}
    near = results.window(fetch, limit=4, offset=0, cap=6)
    assert near["next"] == {"offset": 4, "limit": 2}
    short = results.window(lambda limit: rows[:2], limit=3, offset=0, cap=6)
    assert short == {"items": [0, 1], "count": 2, "truncated": False, "next": None}
    with pytest.raises(ValueError, match="at most 6"):
        results.window(fetch, limit=4, offset=3, cap=6)
    with pytest.raises(ValueError, match="negative"):
        results.window(fetch, limit=1, offset=-1, cap=6)
    with pytest.raises(ValueError, match="truncated"):
        results.listing([], next={"offset": 1})


def test_run_events_continue_before_the_oldest_row(app_client):
    for title in ("a", "b", "c"):
        app_client.post(
            "/issues", json={"title": title}, headers={"X-Athena-Run": "shape-run"}
        )
    server = mcp_server.build_server(AthenaClient(client=app_client))
    first, rest = _wire(
        server,
        [
            ("list_run_events", {"run_id": "shape-run", "limit": 2}),
            ("list_run_events", {"run_id": "shape-run", "limit": 2}),
        ],
    )
    page = _one(first)
    assert page["truncated"] is True
    assert page["next"] == {"before_id": page["items"][-1]["id"]}
    (older,) = _wire(
        server,
        [("list_run_events", {"run_id": "shape-run", "limit": 2, **page["next"]})],
    )
    remaining = _one(older)
    assert remaining["truncated"] is False
    assert {row["id"] for row in remaining["items"]}.isdisjoint(
        {row["id"] for row in page["items"]}
    )
    assert _one(rest) == page


@pytest.mark.parametrize(
    ("tool", "path", "cap"),
    [
        ("list_notifications", "/notifications", 200),
        ("list_run_controls", "/run-controls", 200),
        ("list_dispatches", "/dispatches", 200),
        ("list_security_events", "/security/events", 200),
        ("list_workers", "/workers", 500),
        ("list_approvals", "/approvals", 200),
    ],
)
def test_a_limit_only_route_is_paged_by_offset_through_the_tool(tool, path, cap):
    # The route takes only a limit, so the tool asks for offset + limit rows and
    # keeps the last `limit`: next names the offset where the following page
    # starts, and the route's cap bounds how far that reaches.
    stored = [{"id": n} for n in range(1, 8)]
    asked = []

    def respond(request):
        assert request.url.path == path
        limit = int(request.url.params["limit"])
        asked.append(limit)
        return httpx.Response(200, json=stored[:limit])

    server = mcp_server.build_server(_mocked(respond))
    first, second, past_cap = _wire(
        server,
        [
            (tool, {"limit": 3}),
            (tool, {"limit": 3, "offset": 6}),
            (tool, {"limit": 3, "offset": cap - 2}),
        ],
    )
    assert _one(first) == {
        "items": stored[:3],
        "count": 3,
        "truncated": True,
        "next": {"offset": 3},
    }
    assert _one(second) == {
        "items": stored[6:],
        "count": 1,
        "truncated": False,
        "next": None,
    }
    assert asked == [3, 9]
    refused = _one(past_cap)
    assert refused["code"] == "invalid_arguments"
    assert f"at most {cap}" in refused["detail"]


def test_activity_runs_continue_by_widening_the_event_window():
    def respond(request):
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"run_id": "r", "partial": limit < 500}])

    server = mcp_server.build_server(_mocked(respond))
    clipped, widest = _wire(
        server,
        [
            ("list_activity_runs", {"actor_id": 1}),
            ("list_activity_runs", {"actor_id": 1, "limit": 500}),
        ],
    )
    assert _one(clipped)["next"] == {"limit": 400}
    assert _one(clipped)["truncated"] is True
    assert _one(widest) == {
        "items": [{"run_id": "r", "partial": False}],
        "count": 1,
        "truncated": False,
        "next": None,
    }
    (at_cap,) = _wire(
        mcp_server.build_server(
            _mocked(lambda request: httpx.Response(200, json=[{"partial": True}]))
        ),
        [("list_activity_runs", {"actor_id": 1, "limit": 500})],
    )
    # The route's widest window still clips: more may exist, out of reach.
    assert _one(at_cap) == {
        "items": [{"partial": True}],
        "count": 1,
        "truncated": True,
        "next": None,
    }
