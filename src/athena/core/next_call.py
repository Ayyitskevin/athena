"""The one shape of ``next``: the MCP tool call that follows an answer (K2.1).

An answer that tells an agent what to do next names the call, never a
sentence the agent has to parse: ``{tool, arguments, hint?}``. ``arguments``
holds what the answer already knows (an issue id, a handoff token); ``hint``
is one clause on when to make the call or what else it needs. The desk's
``next`` is a list of the same call with ``why`` in place of ``hint``, and a
refusal's ``next`` comes from the registry (``core/refusal_codes.py``) with
no arguments of its own.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def call(tool: str, hint: str, /, **arguments: Any) -> dict[str, Any]:
    """``{tool, arguments, hint}`` for an answer's ``next``."""
    return {"tool": tool, "arguments": arguments, "hint": hint}


class NextCallOut(BaseModel):
    """A ``next`` as a REST response model declares it."""

    tool: str
    arguments: dict[str, Any]
    hint: str
