"""Each MCP tool declares its scope, profiles, hints and budget where it is
defined, and the central tables are derived from those declarations (B8).

WHY: adding one tool used to edit `TOOL_SCOPES`, a profile set, `WRITE_HINTS`
and sometimes the budget and admin-read tables in server.py, beside the tool's
body in a tools_*.py module. Parallel lanes adding tools conflicted on those
shared tables. Now the declaration sits on the decorator, the tables are views
over the declarations, and registration fails closed on a declaration that is
missing a part, so moving the policy next to the tool did not loosen it.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from athena.core import tokens
from athena.mcp import server as mcp_server
from athena.mcp import tools_pages

TOOL_MODULES = sorted(
    (Path(__file__).resolve().parents[1] / "src" / "athena" / "mcp").glob("tools_*.py")
)
NARROWER = (mcp_server.SEAT, mcp_server.KNOWLEDGE, mcp_server.CONDUCTOR)


class _NoServer:
    """Listing tools needs no reachable Athena."""


def _listed(scopes=None, profile=None) -> dict:
    server = mcp_server.build_server(_NoServer(), scopes=scopes, profile=profile)
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def _with_synthetic(monkeypatch, decorator: str, **declaration) -> None:
    """Add one synthetic tool, declared as given, after the real ones."""
    mcp_server.declared()  # recorded before the patch, so it never sees the probe
    real = tools_pages.register_page_tools

    def register(tool, mutation_tool, client):
        real(tool, mutation_tool, client)
        chosen = mutation_tool if decorator == "mutation_tool" else tool

        @chosen(**declaration)
        def synthetic_probe() -> dict:
            """A synthetic tool for the declaration tests."""
            return {}

    monkeypatch.setattr(tools_pages, "register_page_tools", register)


def _with_bare_decorator(monkeypatch) -> None:
    mcp_server.declared()
    real = tools_pages.register_page_tools

    def register(tool, mutation_tool, client):
        real(tool, mutation_tool, client)

        @tool
        def synthetic_probe() -> dict:
            """A synthetic tool with no declaration at all."""
            return {}

    monkeypatch.setattr(tools_pages, "register_page_tools", register)


# --- the declarations cover every tool ----------------------------------------


def _decorated_tools() -> dict[str, ast.Call | ast.Name]:
    """Every function a tools_*.py module decorates with tool/mutation_tool."""
    found: dict[str, ast.Call | ast.Name] = {}
    for path in TOOL_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                target = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                if isinstance(target, ast.Name) and target.id in (
                    "tool",
                    "mutation_tool",
                ):
                    assert node.name not in found, node.name
                    found[node.name] = decorator
    return found


def test_every_tool_states_its_declaration_at_its_definition():
    decorated = _decorated_tools()
    assert len(decorated) == 143
    assert set(decorated) == set(mcp_server.declared())
    assert set(decorated) == set(_listed())
    for name, decorator in decorated.items():
        assert isinstance(decorator, ast.Call), name
        keywords = {keyword.arg for keyword in decorator.keywords}
        assert {"scope", "profiles"} <= keywords, name
        assert keywords <= {"scope", "profiles", "hints", "budget"}, name
        assert not decorator.args, name


def test_the_declarations_are_recorded_in_registration_order():
    listed = list(_listed())
    assert list(mcp_server.declared()) == listed


# --- the derived tables are the declarations ------------------------------------


def test_the_derived_tables_equal_what_the_declarations_say():
    declared = mcp_server.declared()
    assert dict(mcp_server.TOOL_SCOPES) == {n: d.scope for n, d in declared.items()}
    assert dict(mcp_server.WRITE_HINTS) == {
        n: d.hints for n, d in declared.items() if d.hints is not None
    }
    assert mcp_server.ADMIN_READS == {
        n
        for n, d in declared.items()
        if d.scope == tokens.ADMIN_SCOPE and d.hints is None
    }
    assert dict(mcp_server.RESPONSE_BUDGET_EXCEPTIONS) == {
        n: d.budget for n, d in declared.items() if d.budget is not None
    }
    profiles = mcp_server.PROFILES
    assert list(profiles) == ["full", "seat", "seat+knowledge", "conductor"]
    assert profiles["full"] is None
    for profile in NARROWER:
        assert profiles[profile] == {
            n for n, d in declared.items() if d.carried_by(profile)
        }
    assert mcp_server.SEAT_PROFILE == profiles["seat"]
    assert mcp_server.CONDUCTOR_PROFILE == profiles["conductor"]
    for name, declaration in declared.items():
        assert mcp_server.is_read_only(name) is (declaration.hints is None)
        assert mcp_server.response_budget(name) == declaration.response_budget
        annotations = mcp_server.tool_annotations(name)
        assert annotations.readOnlyHint is (declaration.hints is None)
        if declaration.hints is not None:
            assert annotations.destructiveHint is declaration.hints.destructive
            assert annotations.idempotentHint is declaration.hints.idempotent


def test_the_derived_tables_are_read_only_views():
    with pytest.raises(TypeError):
        mcp_server.TOOL_SCOPES["my_desk"] = mcp_server.ANY_WRITE_SCOPE
    with pytest.raises(TypeError):
        mcp_server.PROFILES["seat"] = frozenset()
    with pytest.raises(TypeError):
        mcp_server.WRITE_HINTS["create_issue"] = mcp_server.SETS
    with pytest.raises(AttributeError):
        mcp_server.ADMIN_READS.add("my_desk")
    with pytest.raises(AttributeError):
        mcp_server.NOT_A_TABLE  # noqa: B018


def test_the_surface_sizes_are_unchanged():
    assert len(mcp_server.declared()) == 143
    assert len(mcp_server.PROFILES["seat"]) == 27
    assert len(mcp_server.PROFILES["seat+knowledge"]) == 38
    assert len(mcp_server.PROFILES["conductor"]) == 40
    assert mcp_server.RESPONSE_BUDGET_EXCEPTIONS == {"get_issue_work_context": 32_000}


# --- the profile composition rule -----------------------------------------------


def _declaration(profiles, **overrides) -> mcp_server.ToolDeclaration:
    return mcp_server.declare(
        "probe", scope=mcp_server.READ_ONLY, profiles=profiles, **overrides
    )


@pytest.mark.parametrize(
    ("profiles", "carried_by"),
    [
        (mcp_server.SEAT, {"seat", "seat+knowledge", "conductor"}),
        (mcp_server.KNOWLEDGE, {"seat+knowledge"}),
        (mcp_server.CONDUCTOR, {"conductor"}),
        ((mcp_server.KNOWLEDGE, mcp_server.CONDUCTOR), {"seat+knowledge", "conductor"}),
        (mcp_server.FULL_ONLY, set()),
    ],
)
def test_a_profile_carries_its_own_tools_and_those_of_its_base(profiles, carried_by):
    declaration = _declaration(profiles)
    assert {p for p in NARROWER if declaration.carried_by(p)} == carried_by
    # Budgeted exactly when a narrower profile carries it.
    expected = mcp_server.DEFAULT_RESPONSE_BUDGET if carried_by else None
    assert declaration.response_budget == expected


def test_every_profile_is_built_on_seat_and_full_carries_everything():
    assert dict(mcp_server.PROFILE_BASES) == {
        "seat": None,
        "seat+knowledge": "seat",
        "conductor": "seat",
    }
    profiles = mcp_server.PROFILES
    assert profiles["seat"] < profiles["seat+knowledge"]
    assert profiles["seat"] < profiles["conductor"]
    assert set(_listed(profile=None)) == set(mcp_server.declared())
    # link_issues is the one tool declared for two narrower profiles.
    both = {
        n
        for n, d in mcp_server.declared().items()
        if d.profiles == (mcp_server.KNOWLEDGE, mcp_server.CONDUCTOR)
    }
    assert both == {"link_issues"}
    assert "link_issues" not in profiles["seat"]


def test_a_declared_budget_is_carried_into_the_tool():
    declaration = _declaration(mcp_server.SEAT, budget=32_000)
    assert declaration.response_budget == 32_000
    assert mcp_server.response_budget("get_issue_work_context") == 32_000
    assert mcp_server.response_budget("not_a_tool") is None


# --- a declaration missing a part fails the build --------------------------------

_READ = {"scope": mcp_server.READ_ONLY}
_WRITE = {"scope": tokens.ISSUE_WRITE_SCOPE}
_ADMIN = {"scope": tokens.ADMIN_SCOPE}


@pytest.mark.parametrize(
    ("decorator", "declaration", "message"),
    [
        ("tool", {"profiles": mcp_server.SEAT}, "declares no scope"),
        ("tool", {"scope": "issues:write", "profiles": mcp_server.SEAT}, "scope"),
        ("tool", {**_READ}, "declares no profiles"),
        ("tool", {**_READ, "profiles": ()}, "say FULL_ONLY"),
        ("tool", {**_READ, "profiles": ["seat"]}, "give a profile name or a tuple"),
        ("tool", {**_READ, "profiles": "lite"}, "unknown profiles ['lite']"),
        (
            "tool",
            {**_READ, "profiles": (mcp_server.FULL_ONLY, mcp_server.SEAT)},
            "FULL_ONLY beside other profiles",
        ),
        (
            "tool",
            {**_READ, "profiles": (mcp_server.SEAT, mcp_server.SEAT)},
            "names a profile twice",
        ),
        (
            "tool",
            {**_READ, "profiles": (mcp_server.SEAT, mcp_server.CONDUCTOR)},
            "declare only the narrowest profiles",
        ),
        (
            "mutation_tool",
            {**_WRITE, "profiles": mcp_server.FULL_ONLY},
            "declares no hints",
        ),
        ("tool", {**_ADMIN, "profiles": mcp_server.FULL_ONLY}, "declares no hints"),
        (
            "mutation_tool",
            {**_WRITE, "profiles": mcp_server.FULL_ONLY, "hints": mcp_server.READS},
            "hints=READS with the write scope",
        ),
        (
            "tool",
            {**_READ, "profiles": mcp_server.FULL_ONLY, "hints": mcp_server.SETS},
            "scoped READ_ONLY but declares write hints",
        ),
        (
            "mutation_tool",
            {**_WRITE, "profiles": mcp_server.FULL_ONLY, "hints": (False, True)},
            "use READS, ADDS, SETS, ERASES or a WriteHint",
        ),
        (
            "mutation_tool",
            {**_READ, "profiles": mcp_server.FULL_ONLY},
            "registered with mutation_tool but only reads",
        ),
        (
            "tool",
            {**_READ, "profiles": mcp_server.FULL_ONLY, "budget": 32_000},
            "no budgeted profile carries it",
        ),
        (
            "tool",
            {**_READ, "profiles": mcp_server.SEAT, "budget": 8_000},
            "above DEFAULT_RESPONSE_BUDGET",
        ),
        (
            "tool",
            {**_READ, "profiles": mcp_server.SEAT, "budget": True},
            "above DEFAULT_RESPONSE_BUDGET",
        ),
        (
            "tool",
            {**_READ, "profiles": mcp_server.SEAT, "scopes": "read"},
            "unknown keys ['scopes']",
        ),
    ],
)
def test_a_tool_missing_a_part_of_its_declaration_fails_the_build(
    monkeypatch, decorator, declaration, message
):
    _with_synthetic(monkeypatch, decorator, **declaration)
    with pytest.raises(RuntimeError) as caught:
        # A seat build: the refusal does not depend on the session carrying it.
        mcp_server.build_server(_NoServer(), profile=mcp_server.PROFILES["seat"])
    assert "'synthetic_probe'" in str(caught.value)
    assert message in str(caught.value)


def test_a_tool_with_no_declaration_at_all_fails_the_build(monkeypatch):
    _with_bare_decorator(monkeypatch)
    with pytest.raises(RuntimeError) as caught:
        mcp_server.build_server(_NoServer())
    assert "'synthetic_probe' has no declaration" in str(caught.value)
    assert "@tool(scope=..., profiles=...)" in str(caught.value)


def test_a_tool_defined_twice_fails_the_build(monkeypatch):
    real = tools_pages.register_page_tools

    def register(tool, mutation_tool, client):
        real(tool, mutation_tool, client)

        @tool(scope=mcp_server.READ_ONLY, profiles=mcp_server.FULL_ONLY)
        def my_desk() -> dict:
            """A second my_desk."""
            return {}

    monkeypatch.setattr(tools_pages, "register_page_tools", register)
    with pytest.raises(RuntimeError, match="'my_desk' is defined twice"):
        mcp_server.build_server(_NoServer())
    with pytest.raises(RuntimeError, match="'my_desk' is defined twice"):
        mcp_server.declared.__wrapped__()


@pytest.mark.parametrize(
    ("decorator", "declaration", "read_only", "in_seat"),
    [
        ("tool", {**_READ, "profiles": mcp_server.SEAT}, True, True),
        (
            "tool",
            {**_ADMIN, "profiles": mcp_server.FULL_ONLY, "hints": mcp_server.READS},
            True,
            False,
        ),
        (
            "mutation_tool",
            {**_WRITE, "profiles": mcp_server.SEAT, "hints": mcp_server.ERASES},
            False,
            True,
        ),
    ],
)
def test_a_complete_declaration_registers_as_declared(
    monkeypatch, decorator, declaration, read_only, in_seat
):
    # The positive control for the refusals above: the same synthetic tool,
    # declared completely, builds and carries what it declared.
    _with_synthetic(monkeypatch, decorator, **declaration)
    full = _listed()
    probe = full["synthetic_probe"]
    assert probe.annotations.readOnlyHint is read_only
    pointer = mcp_server.RETRY_POINTER in (probe.description or "")
    assert pointer is (decorator == "mutation_tool")
    declared = mcp_server.declared.__wrapped__()["synthetic_probe"]
    assert declared.carried_by(mcp_server.SEAT) is in_seat
