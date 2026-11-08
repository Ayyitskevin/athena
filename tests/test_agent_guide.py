"""docs/AGENT_GUIDE.md describes THIS build, and drift fails the gate.

An agent that reads the guide at session start is taken straight to the calls it
lists. So every MCP tool the guide names must be in the server registry, every HTTP
request example must match a registered route, every relative link must resolve, and
the guide must be linked from the README, AGENTS.md and the docs index.

Features that exist only in open PRs are written as prose in the "Pending in PR"
section, without a call signature, so this test cannot mistake them for shipped tools.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from athena.main import create_app
from athena.mcp.server import TOOL_SCOPES

REPO_ROOT = Path(__file__).resolve().parent.parent
GUIDE = REPO_ROOT / "docs" / "AGENT_GUIDE.md"

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCED = re.compile(r"```([a-z]*)\n(.*?)```", re.S)
_CALL = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*\(")
_HTTP_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S+)", re.M)


def _fenced_blocks(text: str, language: str) -> list[str]:
    return [body for lang, body in _FENCED.findall(text) if lang == language]


def _cited_tool_names(text: str) -> set[str]:
    fragments = _CODE_SPAN.findall(text) + [body for _, body in _FENCED.findall(text)]
    names: set[str] = set()
    for fragment in fragments:
        names.update(_CALL.findall(fragment))
    return names


def _http_requests(text: str) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    for block in _fenced_blocks(text, "http"):
        match = _HTTP_LINE.search(block)
        assert match, f"an http example has no request line:\n{block[:120]}"
        requests.append((match.group(1), match.group(2).split("?", 1)[0]))
    return requests


def _registered_routes(app) -> list[tuple[str, str]]:
    # Routers are wrapped, so app.routes is not the flat table; flatten the same way
    # tests/test_runtime_recipe.py does.
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
    return routes


def test_every_cited_tool_exists_in_the_mcp_registry():
    cited = _cited_tool_names(GUIDE.read_text())
    assert cited, "the guide cites no tools; the extractor is broken"
    unknown = sorted(cited - set(TOOL_SCOPES))
    assert not unknown, f"the guide names MCP tools that do not exist: {unknown}"


def test_every_http_request_example_is_a_registered_route(tmp_path):
    requests = _http_requests(GUIDE.read_text())
    assert len(requests) >= 7, (
        "the guide lost its request examples; check the extractor"
    )

    routes = _registered_routes(create_app(tmp_path / "guide-routes.db"))

    def matches(method: str, path: str) -> bool:
        for route_method, template in routes:
            if route_method != method:
                continue
            pattern = re.sub(r"\{[^}]+\}", "[^/]+", template)
            if re.fullmatch(pattern, path):
                return True
        return False

    missing = [(m, p) for m, p in requests if not matches(m, p)]
    assert not missing, (
        f"the guide shows requests to routes that do not exist: {missing}"
    )


def test_every_relative_link_resolves():
    links = re.findall(r"\]\((?!https?://)([^)#]+)", GUIDE.read_text())
    assert links, "the guide has no relative links; the extractor is broken"
    missing = [t for t in links if not (GUIDE.parent / t).resolve().exists()]
    assert not missing, f"the guide links to missing files: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        REPO_ROOT / "README.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "docs" / "INDEX.md",
    ],
    ids=lambda p: p.name,
)
def test_the_guide_is_linked_from_the_front_doors(path):
    assert "AGENT_GUIDE.md" in path.read_text()
