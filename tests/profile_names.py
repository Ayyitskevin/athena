"""Which MCP profiles a paragraph of a document names, and which carry a tool.

A document is written for one profile's reader. A tool that profile does not
carry may still be named, beside the profile that does carry it ("the full
profile also offers `begin_run`"). Two checks read documents that way:
``tests/doc_examples.py`` for each call an entry document shows, and
``tests/test_mcp_docstring_references.py`` for each tool a document a seat
reads names. This module is what they share; it reads no document itself.
"""

from __future__ import annotations

import re

from athena.mcp import server as mcp_server

_PROFILE_NAMED = re.compile(
    r"`(seat\+knowledge|conductor|full|seat)`"
    r"|ATHENA_MCP_PROFILE=(seat\+knowledge|conductor|full|seat)\b"
    r"|\b(seat\+knowledge|conductor)\b"
    r"|\b(?:conductor/)?(full) profiles?\b"
)


def profiles_named(paragraph: str) -> set[str]:
    named: set[str] = set()
    for match in _PROFILE_NAMED.finditer(paragraph):
        named.update(group for group in match.groups() if group)
    return named


def carries(profile: str, tool: str) -> bool:
    members = mcp_server.PROFILES[profile]
    return members is None or tool in members
