"""Every MCP tool carries hints a client can act on, and none of them lies (A4).

WHY: before A4 none of the 140 tools carried MCP annotations, so a client could
not tell a read from a write, or a write it may safely retry from one that
destroys something. A hint is worse than none when it is wrong: a write marked
read-only would be called without the care a write needs.

So: readOnlyHint is generated (the read scope plus the admin reads, which
declare hints=READS) and checked here against the HTTP verbs each tool's client
calls really use, as ``scripts/generate_agent_api.py`` derives them;
destructive and idempotent hints are declared once per write tool, at its
definition (``WRITE_HINTS`` is the derived view), and a write tool without
them refuses to build.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest

from athena.core import tokens
from athena.mcp import server as mcp_server

REPO = Path(__file__).resolve().parents[1]
# POST routes that only read: they take their input as a body, not a query.
READ_ONLY_POSTS = frozenset({"POST /embeds/resolve"})


class _NoServer:
    """Listing tools needs no reachable Athena."""


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_agent_api", REPO / "scripts" / "generate_agent_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["generate_agent_api"] = module
    spec.loader.exec_module(module)
    return module


def _rest_calls() -> dict[str, list[str]]:
    """Each tool's REST calls, "VERB /path", from the generator's own analysis."""
    generator = _generator()
    by_method = generator.client_method_calls()
    return {
        tool: [call for method in methods for call in by_method.get(method, [])]
        for tool, methods in generator.tool_client_methods().items()
    }


def _listed(profile=None) -> dict:
    server = mcp_server.build_server(_NoServer(), profile=profile)
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def test_every_registered_tool_carries_annotations():
    tools = _listed()
    assert set(tools) == set(mcp_server.TOOL_SCOPES)
    missing = sorted(name for name, tool in tools.items() if tool.annotations is None)
    assert missing == []
    for name, tool in tools.items():
        hints = tool.annotations
        assert hints.readOnlyHint is mcp_server.is_read_only(name), name
        if hints.readOnlyHint:
            # A read has nothing to destroy or repeat: the write hints stay unset.
            assert hints.destructiveHint is None, name
            assert hints.idempotentHint is None, name
        else:
            declared = mcp_server.WRITE_HINTS[name]
            assert hints.destructiveHint is declared.destructive, name
            assert hints.idempotentHint is declared.idempotent, name


def test_read_only_is_the_read_scope_plus_the_admin_reads():
    generated = {
        name for name in mcp_server.TOOL_SCOPES if mcp_server.is_read_only(name)
    }
    read_scope = {
        name
        for name, required in mcp_server.TOOL_SCOPES.items()
        if required == mcp_server.READ_ONLY
    }
    assert generated == read_scope | mcp_server.ADMIN_READS
    # The admin-read list names only admin-scoped tools: anything else is a read
    # by its scope already, or a write.
    for name in mcp_server.ADMIN_READS:
        assert mcp_server.TOOL_SCOPES[name] == tokens.ADMIN_SCOPE, name


def test_a_read_only_tool_makes_only_get_calls_and_a_write_makes_another():
    # WHY: the cross-check that makes the generated hint trustworthy. A write
    # declared `read` (or an admin write declared READS) would be marked
    # read-only; its client call's verb says otherwise, and this fails.
    calls = _rest_calls()
    assert set(calls) == set(mcp_server.TOOL_SCOPES)
    lying_reads = {
        name: [c for c in made if not c.startswith("GET ") and c not in READ_ONLY_POSTS]
        for name, made in calls.items()
        if mcp_server.is_read_only(name)
    }
    assert {name: made for name, made in lying_reads.items() if made} == {}
    unmarked_reads = sorted(
        name
        for name, made in calls.items()
        if not mcp_server.is_read_only(name)
        and all(c.startswith("GET ") or c in READ_ONLY_POSTS for c in made)
    )
    assert unmarked_reads == []


def test_the_cross_check_would_catch_a_write_marked_read_only(monkeypatch):
    # The negative self-test: declare a POST tool `read`, and the check above fails.
    calls = _rest_calls()
    assert calls["comment_on_issue"] == ["POST /issues/{issue_id}/comments"]
    declared = mcp_server.declared()
    lying = {
        **declared,
        "comment_on_issue": declared["comment_on_issue"]._replace(
            scope=mcp_server.READ_ONLY, hints=None
        ),
    }
    monkeypatch.setattr(mcp_server, "declared", lambda: lying)
    assert mcp_server.is_read_only("comment_on_issue")
    with pytest.raises(AssertionError):
        test_a_read_only_tool_makes_only_get_calls_and_a_write_makes_another()


def test_write_hints_cover_exactly_the_write_tools():
    writes = {
        name for name in mcp_server.TOOL_SCOPES if not mcp_server.is_read_only(name)
    }
    assert set(mcp_server.WRITE_HINTS) == writes


def test_a_write_tool_without_hints_refuses_to_build(monkeypatch):
    # Fail-closed, like an undeclared scope: the missing hints raise even when
    # the session would not carry the tool (here, a seat profile without it).
    declare = mcp_server.declare

    def without_hints(name, **declaration):
        if name == "delete_sprint":
            declaration.pop("hints")
        return declare(name, **declaration)

    monkeypatch.setattr(mcp_server, "declare", without_hints)
    assert "delete_sprint" not in mcp_server.PROFILES["seat"]
    with pytest.raises(RuntimeError) as caught:
        mcp_server.build_server(_NoServer(), profile=mcp_server.PROFILES["seat"])
    assert "delete_sprint" in str(caught.value)
    assert "declares no hints" in str(caught.value)


def test_the_destructive_writes_are_the_ones_that_lose_something():
    # Pinned so a change to the table is a visible decision, not drift.
    destructive = sorted(
        name for name, hint in mcp_server.WRITE_HINTS.items() if hint.destructive
    )
    assert destructive == [
        "clear_agent_budget",
        "clear_watch_preference",
        "delete_automation_rule",
        "delete_sprint",
        "offboard_agent",
        "revoke_agent_tokens",
        "set_watch_preference",
        "unwatch",
        "update_sprint",
    ]
    # Each of these keeps a version or an audited inverse, so none is destructive.
    # An issue edit keeps the text it replaced, and undo restores it (F10).
    for name in (
        "update_page",
        "archive_issue",
        "detach_label",
        "unlink_issues",
        "update_issue",
        "link_mention",
    ):
        assert not mcp_server.WRITE_HINTS[name].destructive, name


def test_writes_that_add_something_each_time_are_not_idempotent():
    additive = sorted(
        name for name, hint in mcp_server.WRITE_HINTS.items() if not hint.idempotent
    )
    assert additive == [
        "claim_issue",
        "claim_next_ready",
        "comment_on_issue",
        "create_automation_rule",
        "create_issue",
        "create_page",
        "create_run_control",
        "create_sprint",
        "dispatch_to_icarus",
        "heartbeat_agent_run",
        "link_mention",
        "mark_notifications_read",
        "record_run_learning",
        "start_playbook",
        "update_page",
        "worker_heartbeat",
    ]


@pytest.mark.parametrize("profile_name", ("seat", "seat+knowledge", "conductor"))
def test_every_tool_a_seat_carries_has_hints_and_an_output_schema(profile_name):
    tools = _listed(mcp_server.PROFILES[profile_name])
    assert set(tools) == mcp_server.PROFILES[profile_name]
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.outputSchema is not None, name
        assert tool.outputSchema["type"] == "object", name
