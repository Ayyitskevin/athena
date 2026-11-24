"""The documents an MCP-only agent reads: the boot block, the loop guide and the
glossary, served as MCP resources at fixed ``athena://`` URIs.

An agent that reaches Athena only through ``athena-mcp`` cannot open the
repository's ``docs/``, and ``docs/`` is not in the installed wheel. So the
three documents travel as package data beside this module
(``mcp/documents/*.md``, pinned by the wheel-manifest gate the field guide's
pages use) and are byte-identical copies of their ``docs/`` sources, which a
test holds: edit ``docs/`` and copy the file over in the same change.

A resource is read on request, so it costs a session nothing until an agent
asks for it, unlike a tool description. Every profile serves all three.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from mcp.server.fastmcp import FastMCP

DOCUMENTS_DIR = Path(__file__).parent / "documents"
MIME_TYPE = "text/markdown"


class Document(NamedTuple):
    uri: str
    name: str
    filename: str
    description: str


DOCUMENTS: tuple[Document, ...] = (
    Document(
        uri="athena://docs/boot-block",
        name="boot-block",
        filename="AGENT_BOOT.md",
        description=(
            "The seat-boot block a seat's rules file carries, the wiring check, "
            "and the profiles a session can run with."
        ),
    ),
    Document(
        uri="athena://docs/loop-guide",
        name="loop-guide",
        filename="AGENT_GUIDE.md",
        description=(
            "The working loop: orient with the desk, find and claim work, show "
            "progress, complete or hand off, and what never to do."
        ),
    ),
    Document(
        uri="athena://docs/glossary",
        name="glossary",
        filename="GLOSSARY.md",
        description=(
            "Every word in a tool name, and the contract terms, with what each "
            "is not the same as."
        ),
    ),
)


def read_document(filename: str) -> str:
    """One packaged document's text. A missing file means the package data did
    not ship; the read fails naming it rather than answering empty."""
    path = DOCUMENTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"athena-mcp document missing from the package: {filename}"
        ) from exc


def _reader(filename: str) -> Callable[[], str]:
    # No parameters: FastMCP registers a function with any as a URI template.
    def read() -> str:
        return read_document(filename)

    return read


def register_resources(mcp: FastMCP) -> None:
    """Serve every document as a resource on ``mcp``."""
    for document in DOCUMENTS:
        mcp.resource(
            document.uri,
            name=document.name,
            description=document.description,
            mime_type=MIME_TYPE,
        )(_reader(document.filename))
