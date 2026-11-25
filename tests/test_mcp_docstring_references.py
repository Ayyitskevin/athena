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

A named profile (`seat`, `seat+knowledge`, `conductor`) narrows the promise: a
seat once read "drain `recent_events(after=...)`" on `my_desk` and "Call
query_help()" on `search_work`, and neither tool was in its profile. So, per
profile, any registered tool name written as a call — backticked or not — in an
in-profile description or in the server instructions must be in that profile.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re

import pytest

from athena.mcp import server as mcp_server

from profile_names import carries, profiles_named

# A backticked identifier written as a call: `my_desk(`, `recent_events(after=...`.
_CALL = re.compile(r"`([a-z_][a-z0-9_]*)\s*\(")
# The same thing in a markdown code fence, where the backticks wrap the block
# instead of each name. Attached paren only, and never after a dot, so prose and
# `cursor.after_id)` cannot masquerade as a call.
_FENCED_CALL = re.compile(r"(?<![\w.`])([a-z_][a-z0-9_]*)\(")
# Any identifier written as a call, backticked or bare: `my_desk(`, query_help().
# Attached paren only, so prose such as "full-text search (FTS)" is not a call.
_ANY_CALL = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)`?\(")
# Any snake_case identifier, as a whole word: a tool name written in prose.
_TOOL_WORD = re.compile(r"(?<![\w.-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\w-])")
REPO = Path(__file__).resolve().parents[1]
DESK_DOC = REPO / "docs" / "DESK.md"
NAMED_PROFILES = ("seat", "seat+knowledge", "conductor")
# A backticked bare identifier: how a one-word tool (`watch`, `whoami`) is named.
_TICKED = re.compile(r"`([a-z_][a-z0-9_]*)`")
_FENCE_LINE = re.compile(r"^\s*```")

# Documents a seat reads, and the profile each addresses (G9). The desk sheet
# and the Field Guide are written for a seat; the desk-loop prompt is handed to
# the `seat+knowledge` agent RUNTIME_RECIPE.md onboards. `watch`,
# `mark_notifications_read` and `recent_events` were named there as a seat's
# next call while no seat profile carried them.
FIELD_GUIDE = REPO / "src" / "athena" / "field_guide"
SEAT_DOCUMENTS: dict[Path, str] = {
    DESK_DOC: "seat",
    REPO / "examples" / "desk_loop.md": "seat+knowledge",
    **{page: "seat" for page in sorted(FIELD_GUIDE.glob("*.md"))},
}


class _NoWhoamiStub:
    """No reachable server: registration must not depend on one."""


def _tools(profile: frozenset[str] | None = None) -> dict[str, str]:
    server = mcp_server.build_server(_NoWhoamiStub(), scopes=None, profile=profile)
    return {
        tool.name: tool.description or "" for tool in asyncio.run(server.list_tools())
    }


def _out_of_profile_calls(
    texts: dict[str, str], visible: set[str], registered: set[str]
) -> set[str]:
    """Registered tools named as calls in ``texts`` that ``visible`` lacks."""
    return {
        f"{where}: {called}"
        for where, text in texts.items()
        for called in _ANY_CALL.findall(text)
        if called in registered and called not in visible
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


@pytest.mark.parametrize("profile_name", NAMED_PROFILES)
def test_a_profile_is_never_told_to_call_a_tool_it_does_not_carry(profile_name):
    """Profile closure: what a profile's session reads (its tool descriptions and
    the server instructions) names as a call only tools that session carries. A
    capability the profile lacks is named by the profile that has it instead."""
    registered = set(_tools())
    tools = _tools(mcp_server.PROFILES[profile_name])
    texts = {**tools, "SERVER_INSTRUCTIONS": mcp_server.SERVER_INSTRUCTIONS}
    dangling = _out_of_profile_calls(texts, set(tools), registered)
    assert dangling == set(), (
        f"the {profile_name!r} profile is told to call tools it does not carry: "
        f"{sorted(dangling)}"
    )


@pytest.mark.parametrize("profile_name", NAMED_PROFILES)
def test_a_profiles_tool_text_names_only_tools_it_carries(profile_name):
    """Profile closure, by name and not only by call: an in-profile tool's
    description that points at a registered tool at all ("Use list_users to
    resolve agent user ids", F9) points at one the session carries. A conductor
    read that on `delegate_issue` and had no `list_users`, which a member is
    refused anyway."""
    registered = set(_tools())
    tools = _tools(mcp_server.PROFILES[profile_name])
    named = {
        f"{name}: {word}"
        for name, description in tools.items()
        for word in _TOOL_WORD.findall(description)
        if word in registered and word not in tools
    }
    assert named == set(), (
        f"the {profile_name!r} profile's tool texts name tools it does not carry: "
        f"{sorted(named)}"
    )


def test_the_profile_guard_would_catch_bare_and_backticked_calls():
    """The negative self-test: the two shapes that shipped, against a seat-sized
    surface, plus prose that must not count as a call."""
    texts = {
        "my_desk": "The loop: `my_desk()` -> drain `recent_events(after=...)`.",
        "search_work": "Call query_help() for the vocabulary as data.",
        "prose": "A full-text search (FTS) over `search_work` results.",
    }
    registered = {"my_desk", "recent_events", "query_help", "search", "search_work"}
    visible = {"my_desk", "search_work"}
    assert _out_of_profile_calls(texts, visible, registered) == {
        "my_desk: recent_events",
        "search_work: query_help",
    }


# --- documents a seat reads -----------------------------------------------------


def _units(text: str) -> list[tuple[int, str]]:
    """(first line, text) of each paragraph, list item or table, with a fenced
    block joined to the paragraph before it: the prose that introduces a call
    is where the profile that carries it is named."""
    units: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    fence: list[str] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if fence is not None:
            fence.append(line)
            if _FENCE_LINE.match(line):
                fence, current = None, None
            continue
        if _FENCE_LINE.match(line):
            if current is None:
                if not units:
                    units.append((number, []))
                current = units[-1][1]
            fence = current
            fence.append(line)
            continue
        if not line.strip():
            current = None
            continue
        if current is None or line.lstrip().startswith(("- ", "* ")):
            current = []
            units.append((number, current))
        current.append(line)
    return [(start, "\n".join(lines)) for start, lines in units]


def _unqualified_tools(
    text: str, addressed: str, registered: set[str]
) -> list[tuple[int, str]]:
    """(line, tool) for each registered tool the text names that the addressed
    profile does not carry and whose paragraph names no profile that does."""
    found: list[tuple[int, str]] = []
    for line, unit in _units(text):
        named = (
            set(_TOOL_WORD.findall(unit))
            | set(_ANY_CALL.findall(unit))
            | set(_TICKED.findall(unit))
        ) & registered
        profiles = profiles_named(unit) | {addressed}
        found += [
            (line, tool)
            for tool in sorted(named)
            if not any(carries(profile, tool) for profile in profiles)
        ]
    return found


@pytest.mark.parametrize(
    "path", SEAT_DOCUMENTS, ids=lambda path: path.relative_to(REPO).as_posix()
)
def test_a_seat_document_names_only_tools_its_profile_carries(path):
    """Profile closure for the documents a seat reads (G9): a tool the
    document's profile does not carry is named only in a paragraph that names
    the profile that does ("the full profile also offers ..."), so a seat is
    never sent to a tool its session does not list."""
    registered = set(_tools())
    addressed = SEAT_DOCUMENTS[path]
    found = _unqualified_tools(path.read_text(encoding="utf-8"), addressed, registered)
    assert found == [], (
        f"{path.relative_to(REPO)} addresses `{addressed}` and names tools it does "
        f"not carry, with no profile that does named beside them: {found}"
    )


def test_every_field_guide_page_is_checked():
    """The guide's manifest and the checked set are the same pages."""
    from athena import guide

    checked = {path.name for path in SEAT_DOCUMENTS if path.parent == FIELD_GUIDE}
    assert checked == {filename for filename, _, _ in guide.PAGES}
    # Each addresses a named profile: `full` carries every tool, so a document
    # held to it would be held to nothing.
    assert set(SEAT_DOCUMENTS.values()) <= set(NAMED_PROFILES)


SEAT_PAGE = """Subscribe to the space:

```
watch("space", space_id)
```

`unwatch` stops it. Mark them read with `mark_notifications_read()`.

With the full profile, `watch("issue", issue_id)` follows one issue.

- Search pages with `search(query)`; `seat+knowledge` carries it.
- A conductor reads `get_project_review_queue` as well.
"""


def test_the_seat_document_guard_would_catch_a_tool_the_seat_lacks():
    """The negative self-test: a fenced call, a backticked one-word tool and a
    bare call with no profile named all fail; the same tools in a paragraph
    that names a profile carrying them do not."""
    registered = set(_tools())
    assert _unqualified_tools(SEAT_PAGE, "seat", registered) == [
        (1, "watch"),
        (7, "mark_notifications_read"),
        (7, "unwatch"),
    ]
