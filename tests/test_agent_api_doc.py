"""docs/AGENT_API.md is generated — this test is the drift gate.

The doc maps every MCP tool to its required scope and the REST call(s) it
performs. Because it is derived (``scripts/generate_agent_api.py``), the only
way it stays true is if regeneration is enforced: this test fails whenever the
committed file no longer matches what the code produces, which is exactly the
moment someone added, removed, renamed, or re-scoped a tool without
regenerating.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_agent_api", REPO / "scripts" / "generate_agent_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["generate_agent_api"] = module
    spec.loader.exec_module(module)
    return module


def test_agent_api_doc_matches_the_code():
    """The committed doc must be byte-identical to a fresh regeneration —
    a drifted doc is a lying doc, and the fix is one command."""
    generator = _load_generator()
    committed = (REPO / "docs" / "AGENT_API.md").read_text()
    assert committed == generator.render(), (
        "docs/AGENT_API.md is stale — regenerate with "
        "`python scripts/generate_agent_api.py`"
    )


def test_agent_api_doc_is_not_vacuous():
    """Canaries: the doc must actually carry the surface it claims to map.
    A regression that empties a table (an AST pass silently matching nothing)
    must fail here rather than produce a well-formed empty document."""
    generator = _load_generator()
    content = generator.render()

    # The one-call orientation read and its route — the boot block's first verb.
    assert "| `my_desk` |" in content and "`GET /desk`" in content
    # A mutation tool mapped through _mutate, with its scope group present.
    assert "| `claim_issue` |" in content
    assert "Aegis write tools — scope: `issue:write`" in content
    # The scope-filtering contract is stated, not implied.
    assert "scope-filtered" in content

    # Every tool in the authoritative registry appears exactly once as a row.
    sys.path.insert(0, str(REPO / "src"))
    from athena.mcp.server import TOOL_SCOPES

    for name in TOOL_SCOPES:
        assert content.count(f"| `{name}` |") == 1, name

    # The REST column is populated for the overwhelming majority of tools:
    # if the client-path extraction broke, rows degrade to "(client-side)"
    # and this bound catches the collapse.
    client_side = content.count("*(client-side)*")
    assert client_side <= len(TOOL_SCOPES) // 10, (
        f"{client_side} of {len(TOOL_SCOPES)} tools have no extracted REST "
        "call — the client.py path extraction has probably broken"
    )
