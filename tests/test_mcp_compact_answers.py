"""Every MCP answer is sent as compact JSON (F12).

WHY: the response budget (A6) is a number of characters of the text a client
receives, and that text was indented JSON. Measured on the budget test's
workspace at dd594b1 (every desk lane at its maximum, one large issue and
page), the 47 tools the three seat profiles carry sent 282,532 characters, of
which 45,817 (16%) were indentation and the space after ``:`` and ``,``: 18%
of the ``seat`` profile's answers, up to a third of a structured answer such as
``list_notifications`` or ``get_project_brief``. An answer the budget cut was
cut that much earlier. The budget in characters is unchanged; compact text
carries that much more.

The contract pinned here, over the protocol, for every tool on the full
surface:

* the text content is one JSON object with no insignificant whitespace: it is
  exactly the compact serialization of its own parsed value;
* on success it equals the structured content as data, keys in the same order;
* non-ASCII text is sent as itself, not escaped;
* a failure is the single failure object with its registered code, compact too;
* the budget measures this text (``results.serialized_size`` is its length).

The REST transport is mocked: an object answer for one pass, a list of the
same row for another and a D2 page of it for a third, so every tool answers
successfully in at least one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp.shared.memory import create_connected_server_and_client_session
import pydantic_core
import pytest
from support import assert_registered_refusal

from athena.mcp import results
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

#: Text a row carries that JSON could escape: accents, CJK, a typographic
#: apostrophe and dash, and an emoji outside the Basic Multilingual Plane.
NON_ASCII = "Zoë’s naïve café — 日本語 🚀"
ROW = {
    "id": 1,
    "key": "ATH-1",
    "title": NON_ASCII,
    "body": "a body\nwith two lines",
    "status": "open",
    "generation": "0" * 32,
    "nested": {"list": [1, 2.5, None, True], "empty": {}},
}
#: Arguments a schema alone cannot supply: one of two optional ids, a token of
#: a fixed pattern, an explicit confirmation.
OVERRIDES: dict[str, dict[str, Any]] = {
    "get_issue": {"issue_id": 1},
    "get_issue_work_context": {"issue_id": 1},
    "complete_claim": {"generation": "0" * 32},
    "yield_claim": {"generation": "0" * 32},
    "resume_claim_handoff": {"generation": "0" * 32, "handoff_token": "0" * 32},
    "delete_sprint": {"confirm_permanent": True},
    # Takes exactly one of notification_id, ids or through_id; none is required.
    "mark_notification_read": {"through_id": 1},
}


def _value(schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    """The smallest value ``schema`` accepts, for a required argument."""
    if "$ref" in schema:
        return _value(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in schema:
        return next(
            _value(option, defs)
            for option in schema["anyOf"]
            if option.get("type") != "null"
        )
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "integer":
        return max(1, schema.get("minimum", 1))
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {
            key: _value(inner, defs)
            for key, inner in schema.get("properties", {}).items()
            if key in schema.get("required", [])
        }
    return "x" * max(1, schema.get("minLength", 1))


def _arguments(tool) -> dict[str, Any]:
    schema = tool.inputSchema
    defs = schema.get("$defs", {})
    required = {
        name: _value(inner, defs)
        for name, inner in schema.get("properties", {}).items()
        if name in schema.get("required", [])
    }
    return {**required, **OVERRIDES.get(tool.name, {})}


def _every_tool_answering(body: Any) -> dict[str, Any]:
    """Every tool on the full surface called once over the protocol, against a
    REST transport that answers ``body`` to every request."""
    client = AthenaClient(
        client=httpx.Client(
            base_url="http://athena.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json=body, headers={"ETag": '"tag"'}
                )
            ),
        )
    )
    server = mcp_server.build_server(client)

    async def run() -> dict[str, Any]:
        async with create_connected_server_and_client_session(
            server._mcp_server  # noqa: SLF001 — the protocol server under test
        ) as session:
            tools = (await session.list_tools()).tools
            return {
                tool.name: await session.call_tool(tool.name, _arguments(tool))
                for tool in tools
            }

    return asyncio.run(run())


def _ordered(text: str) -> Any:
    """``text`` parsed with every object as its list of (key, value) pairs, so
    two values compare equal only with their keys in the same order."""
    return json.loads(text, object_pairs_hook=list)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sent_text(result) -> str:
    """The one text block a result carries."""
    (block,) = result.content
    assert block.type == "text"
    return block.text


@pytest.fixture(scope="module")
def answers() -> dict[str, dict[str, Any]]:
    return {
        "object": _every_tool_answering(ROW),
        "list": _every_tool_answering([ROW]),
        # H6: the issue, page and comment lists answer one D2 page.
        "page": _every_tool_answering(
            {
                "issues": [ROW],
                "pages": [ROW],
                "comments": [ROW],
                "next_cursor": None,
                "has_more": False,
            }
        ),
    }


def test_every_tool_sends_compact_json_equal_to_its_structured_content(answers):
    tools = set(answers["object"])
    assert tools == set(mcp_server.declared())
    succeeded: set[str] = set()
    indented: dict[str, str] = {}
    for results_by_tool in answers.values():
        for name, result in results_by_tool.items():
            text = _sent_text(result)
            value = json.loads(text)
            assert isinstance(value, dict), name
            if text != _compact(value):
                indented[name] = text[:80]
                continue
            if result.isError:
                continue
            succeeded.add(name)
            # Equal as data, keys in the same order.
            assert value == result.structuredContent, name
            assert _ordered(text) == _ordered(_compact(result.structuredContent))
    assert indented == {}, (
        f"{len(indented)} tools send insignificant whitespace: {sorted(indented)}"
    )
    # Every tool was seen succeeding, not only refusing.
    assert succeeded == tools, sorted(tools - succeeded)


def test_non_ascii_text_is_sent_as_itself(answers):
    seen = 0
    for name, result in answers["object"].items():
        text = _sent_text(result)
        if result.isError or NON_ASCII not in _compact(json.loads(text)):
            continue
        seen += 1
        assert NON_ASCII in text, name
        assert "\\u" not in text, name
    assert seen > 0


def test_a_failure_is_one_compact_object_with_its_registered_code(answers):
    failures = {
        name: _sent_text(result)
        for results_by_tool in answers.values()
        for name, result in results_by_tool.items()
        if result.isError
    }
    assert failures, "a list answer to an object read refuses at least once"
    for name, text in failures.items():
        failure = json.loads(text)
        assert text == _compact(failure), name
        assert {"detail", "code", "status_code"} <= set(failure), name
        assert_registered_refusal(failure)


def test_the_budget_measures_the_compact_text_it_sends():
    value = {"title": NON_ASCII, "rows": [{"id": 1, "body": "x\ny"}], "next": None}
    sent = results.answer_text(value)
    assert sent == _compact(value)
    assert sent == pydantic_core.to_json(value).decode()
    assert results.serialized_size(value) == len(sent)
