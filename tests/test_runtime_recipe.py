"""The desk-loop recipe describes THIS build, and drift fails the gate.

docs/RUNTIME_RECIPE.md and examples/desk_loop.md are the operator-side artifact
that closes the loop for a real user: paste this config, paste this prompt, watch
an agent finish a delegated issue. A recipe is worthless the moment it cites a
tool that was renamed, an endpoint that moved, or a doc that was deleted — and
worse than worthless, because the reader concludes the product is broken rather
than the docs.

So the recipe is pinned to the code it describes: every MCP tool it names must be
in the server's registry, every JSON config block must parse and match what
Athena actually prints, every HTTP endpoint it tells the reader to curl must be a
registered route, and every relative link must resolve.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from athena.main import create_app
from athena.mcp.config import claude_mcp_config
from athena.mcp.server import TOOL_SCOPES

REPO_ROOT = Path(__file__).resolve().parent.parent
RECIPE = REPO_ROOT / "docs" / "RUNTIME_RECIPE.md"
PROMPT = REPO_ROOT / "examples" / "desk_loop.md"
DOCS = (RECIPE, PROMPT)

# Words that appear as `name(` in these files without being MCP tools: prose and
# shell. Kept explicit and small — an unexpected identifier should FAIL rather
# than be silently forgiven, since that is exactly how a renamed tool would slip
# through.
NOT_TOOLS = frozenset({"athena_field_guide", "athena_demo", "athena_mcp"})

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCED = re.compile(r"```[a-z]*\n(.*?)```", re.S)
_CALL = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*\(")


def _cited_tool_names(text: str) -> set[str]:
    """Every snake_case `identifier(` inside a code span or fenced block."""
    fragments = _CODE_SPAN.findall(text) + _FENCED.findall(text)
    names: set[str] = set()
    for fragment in fragments:
        names.update(_CALL.findall(fragment))
    return {name for name in names if name not in NOT_TOOLS}


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_cited_tool_exists_in_the_mcp_registry(path):
    cited = _cited_tool_names(path.read_text())
    assert cited, f"{path.name} cites no tools; the extractor is broken"
    unknown = sorted(cited - set(TOOL_SCOPES))
    assert not unknown, (
        f"{path.name} names MCP tools that do not exist: {unknown}. "
        "Rename in the doc, or the recipe teaches a tool nobody has."
    )


def test_the_recipe_walks_the_whole_desk_loop():
    """The recipe's claim is that a reader reaches 'an agent completed a delegated
    issue and the trail shows it'. These are the tools that sentence requires; if
    the prompt stops naming one, the loop it teaches is no longer closed."""
    cited = _cited_tool_names(PROMPT.read_text())
    for required in (
        "my_desk",
        "begin_run",
        "heartbeat_agent_run",
        "get_issue_work_context",
        "claim_issue",
        "record_run_learning",
        "complete_claim",
        "acknowledge_run_control",
        "complete_run_control",
    ):
        assert required in cited, f"the desk-loop prompt no longer names {required}"


def test_the_printed_mcp_config_parses_and_matches_what_athena_emits():
    """The config block is copy-paste; if it does not match the shape Athena
    prints, the reader's client silently fails to connect."""
    blocks = [
        block
        for block in _FENCED.findall(RECIPE.read_text())
        if block.lstrip().startswith("{")
    ]
    assert blocks, "the recipe no longer shows an MCP config block"
    parsed = [json.loads(block) for block in blocks]
    reference = claude_mcp_config(base_url="http://127.0.0.1:8000", token="x")
    for config in parsed:
        assert set(config) == set(reference)
        server = config["mcpServers"]["athena"]
        expected = reference["mcpServers"]["athena"]
        assert server["command"] == expected["command"]
        assert set(server["env"]) == set(expected["env"])
        assert server["env"]["ATHENA_BASE_URL"] == "http://127.0.0.1:8000"


def test_every_curled_endpoint_is_a_real_route(tmp_path):
    """The recipe tells the reader to POST to specific paths. A moved endpoint
    turns the first five minutes into a 404."""
    text = RECIPE.read_text()
    calls = re.findall(r"curl[^\n]*-X\s+(\w+)[^`]*?http://127\.0\.0\.1:8000(\S+)", text)
    assert calls, "the recipe no longer shows a curl example"

    app = create_app(str(tmp_path / "routes.db"))
    # Routers are wrapped, so app.routes is not the route table — flatten through
    # _IncludedRouter exactly as scripts/check_template_routes.py does.
    routes = []
    for route in app.routes:
        inner = (
            route.original_router.routes
            if type(route).__name__ == "_IncludedRouter"
            else [route]
        )
        for entry in inner:
            path = getattr(entry, "path", None)
            for method in getattr(entry, "methods", None) or set():
                if path:
                    routes.append((method, path))

    def matches(method: str, path: str) -> bool:
        for route_method, template in routes:
            if route_method != method:
                continue
            pattern = re.sub(r"\{[^}]+\}", "[^/]+", template)
            if re.fullmatch(pattern, path):
                return True
        return False

    for method, path in calls:
        path = path.rstrip("\\'\" ")
        assert matches(method, path), f"{method} {path} is not a registered route"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_relative_link_resolves(path):
    links = re.findall(r"\]\((?!https?://)([^)#]+)", path.read_text())
    assert links, f"{path.name} has no relative links"
    missing = [
        target for target in links if not (path.parent / target).resolve().exists()
    ]
    assert not missing, f"{path.name} links to missing files: {missing}"


def test_the_readme_tour_points_at_the_recipe():
    """F-2.2's artifact is only useful if the front door mentions it."""
    readme = (REPO_ROOT / "README.md").read_text()
    assert "docs/RUNTIME_RECIPE.md" in readme
