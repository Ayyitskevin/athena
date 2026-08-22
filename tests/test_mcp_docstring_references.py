"""Every tool an MCP docstring tells an agent to call must exist.

`TOOL_SCOPES` guards tool REGISTRATION; nothing guarded tool PROSE — and the prose
is what the agent actually reads to decide what to call next. So `my_desk`, the
flagship "START HERE" tool, spent 19 days instructing every agent to drain
`list_events(after=...)`, a tool that has never been registered: calling it raises
`KeyError`. The suite stayed green throughout, because a docstring is not code.

This closes that: a name written as a CALL inside a tool description — backticked
and followed by `(` — is a promise the agent can keep, so it must name a
registered tool. Parameter names, lane names and prose stay untouched, since
nobody writes those with parentheses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re

from athena.mcp import server as mcp_server

# A backticked identifier written as a call: `my_desk(`, `recent_events(after=...`.
_CALL = re.compile(r"`([a-z_][a-z0-9_]*)\s*\(")
# The same thing in a markdown code fence, where the backticks wrap the block
# instead of each name. Attached paren only, and never after a dot, so prose and
# `cursor.after_id)` cannot masquerade as a call.
_FENCED_CALL = re.compile(r"(?<![\w.`])([a-z_][a-z0-9_]*)\(")
DESK_DOC = Path(__file__).resolve().parents[1] / "docs" / "DESK.md"


class _NoWhoamiStub:
    """No reachable server: registration must not depend on one."""


def _tools() -> dict[str, str]:
    server = mcp_server.build_server(_NoWhoamiStub(), scopes=None)
    return {
        tool.name: tool.description or "" for tool in asyncio.run(server.list_tools())
    }


def test_every_tool_call_named_in_a_docstring_is_a_registered_tool():
    tools = _tools()
    dangling = {
        f"{name}: {called}"
        for name, description in tools.items()
        for called in _CALL.findall(description)
        if called not in tools
    }
    assert dangling == set(), (
        "MCP docstrings name tools that do not exist — an agent that follows the "
        f"instruction gets a KeyError: {sorted(dangling)}"
    )


def test_the_desk_field_guide_names_only_registered_tools():
    """DESK.md is the desk loop's instruction sheet and carries the same loop in
    the same call syntax, so it can go stale the same way."""
    tools = _tools()
    text = DESK_DOC.read_text(encoding="utf-8")
    called = set(_CALL.findall(text)) | set(_FENCED_CALL.findall(text))
    dangling = sorted(name for name in called if name not in tools)
    assert dangling == [], f"docs/DESK.md names unregistered tools: {dangling}"


def test_the_guard_would_catch_a_phantom():
    """The negative self-test the sibling checkers all carry: prove this fails on a
    violation rather than passing because the regex never matches anything."""
    tools = {"my_desk": "The loop: `my_desk()` -> drain `list_events(after=...)`."}
    dangling = [
        called
        for description in tools.values()
        for called in _CALL.findall(description)
        if called not in tools
    ]
    assert dangling == ["list_events"]
