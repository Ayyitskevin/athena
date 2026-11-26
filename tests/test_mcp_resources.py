"""An agent that reaches Athena only through `athena-mcp` can read its documents.

The boot block, the loop guide and the glossary were files in a repository an
MCP-only agent cannot open, while the server advertised a resources capability
and served nothing under it (A14). These tests hold the three documents as MCP
resources, read over the protocol, in every profile, at fixed `athena://`
URIs; hold the packaged copies byte-identical to `docs/`, which is not in the
installed wheel; and hold the wheel gate over those copies, so a resource that
lists cannot be one whose text is missing from an install.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tomllib

import pytest

from athena.mcp import server as mcp_server
from support import mcp_sessions, run_agents

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PACKAGED = ROOT / "src" / "athena" / "mcp" / "documents"
# The URIs are a contract with every client that bookmarked one: a rename here
# is a breaking change, so they are spelled out, not imported.
EXPECTED = {
    "athena://docs/boot-block": "AGENT_BOOT.md",
    "athena://docs/loop-guide": "AGENT_GUIDE.md",
    "athena://docs/glossary": "GLOSSARY.md",
    "athena://answers/glossary": "ANSWER_GLOSSARY.md",
}
PROFILES = ("full", "seat", "seat+knowledge", "conductor")


class _NoWhoamiStub:
    """No reachable Athena: a document read must not need one."""


def _server(profile_name: str):
    profile = None if profile_name == "full" else mcp_server.PROFILES[profile_name]
    return mcp_server.build_server(_NoWhoamiStub(), scopes=None, profile=profile)


@pytest.mark.parametrize("profile_name", PROFILES)
def test_every_profile_serves_its_documents_over_the_protocol(profile_name):
    async def play():
        async with mcp_sessions(_server(profile_name)) as (session,):
            listed = (await session.list_resources()).resources
            read = {}
            for resource in listed:
                contents = (await session.read_resource(resource.uri)).contents
                read[str(resource.uri)] = [
                    (getattr(item, "text", None), item.mimeType) for item in contents
                ]
            return listed, read

    listed, read = run_agents(play())
    assert {str(resource.uri) for resource in listed} == set(EXPECTED)
    for resource in listed:
        assert resource.mimeType == "text/markdown"
        assert resource.description, f"{resource.uri} says nothing about itself"
    for uri, filename in EXPECTED.items():
        text = (DOCS / filename).read_text(encoding="utf-8")
        assert read[uri] == [(text, "text/markdown")], (
            f"reading {uri} did not answer docs/{filename}"
        )


def test_the_server_advertises_only_what_it_serves():
    """Resources are served, so the capability is advertised; no prompt is
    served, so a client is not told there are prompts to list."""

    async def play():
        async with mcp_sessions(_server("seat")) as (session,):
            return session.get_server_capabilities()

    capabilities = run_agents(play())
    assert capabilities is not None
    assert capabilities.resources is not None
    assert capabilities.tools is not None
    assert capabilities.prompts is None


@pytest.mark.parametrize("filename", sorted(EXPECTED.values()))
def test_the_packaged_copy_is_the_document_in_docs(filename):
    """docs/ does not ship in the wheel, so the server reads a packaged copy.
    The copy is the document: edit docs/ and copy it over in the same change."""
    source = (DOCS / filename).read_bytes()
    packaged = PACKAGED / filename
    assert packaged.is_file(), f"src/athena/mcp/documents/{filename} is missing"
    assert packaged.read_bytes() == source, (
        f"src/athena/mcp/documents/{filename} differs from docs/{filename}; "
        f"copy it: cp docs/{filename} src/athena/mcp/documents/{filename}"
    )


def test_the_packaged_documents_are_exactly_the_served_ones():
    """Nothing else sits beside the copies: a stray file would ship unserved."""
    assert sorted(path.name for path in PACKAGED.iterdir()) == sorted(EXPECTED.values())


def test_a_document_missing_from_the_package_is_named_not_served_empty(
    tmp_path, monkeypatch
):
    """An install whose package data dropped a file answers a read with an
    error naming it, never with an empty document an agent would trust."""
    from athena.mcp import resources

    monkeypatch.setattr(resources, "DOCUMENTS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="GLOSSARY.md"):
        resources.read_document("GLOSSARY.md")


def test_the_wheel_carries_the_packaged_documents():
    """The package data declares the copies and the wheel gate compares them,
    the same two holds the field guide's pages have."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["athena"]
    assert "mcp/documents/*.md" in package_data

    script = ROOT / "scripts" / "verify_wheel.py"
    spec = importlib.util.spec_from_file_location("athena_verify_wheel", script)
    assert spec is not None and spec.loader is not None
    verify_wheel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify_wheel)
    assert "mcp/documents" in verify_wheel.RUNTIME_DIRS
    shipped = verify_wheel.source_runtime_files()
    assert {f"mcp/documents/{name}" for name in EXPECTED.values()} <= shipped
