"""The launch contract for connecting an AI agent to Athena over MCP.

An operator connects an agent by handing their MCP client four facts: run the
`athena-mcp` command, point ATHENA_BASE_URL at the deployment, authenticate with
ATHENA_TOKEN, and carry the ATHENA_MCP_PROFILE tool set. This module is that
contract's one home — the demo CLI prints it, POST /users/onboard_agent returns
it and the cockpit's onboarding page renders it, so every surface describes the
same block (one fact, one place). Deliberately stdlib-only: core imports this
for the config *shape*; nothing here touches the MCP SDK or httpx.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import sys

# The console script name pyproject.toml installs for athena.mcp.server:main.
MCP_COMMAND = "athena-mcp"

# Profile names, spelled as athena.mcp.server.PROFILES spells them (a test holds
# the two together; importing the server here would pull in the MCP SDK).
SEAT_PROFILE = "seat"
SEAT_KNOWLEDGE_PROFILE = "seat+knowledge"
# The scope that decides between them, and the one that implies every scope
# (identity.token_has_scope's rule), spelled as core.tokens spells them.
_DOCS_WRITE_SCOPE = "docs:write"
_ADMIN_SCOPE = "admin"


def seat_profile_for(scopes: Iterable[str]) -> str:
    """The profile a printed config names for a token with these scopes:
    ``seat+knowledge`` when the token may write pages, ``seat`` otherwise.

    An unset ATHENA_MCP_PROFILE still means ``full`` at runtime (decision D1);
    this only decides what Athena *prints*, so a new agent starts on the seat
    surface instead of every tool its token allows."""
    granted = set(scopes)
    if _DOCS_WRITE_SCOPE in granted or _ADMIN_SCOPE in granted:
        return SEAT_KNOWLEDGE_PROFILE
    return SEAT_PROFILE


def athena_mcp_command() -> str:
    """The absolute path of the ``athena-mcp`` script installed beside the
    running interpreter, or the bare name when there is none.

    A virtualenv's scripts are not on PATH unless it is activated, so a bare
    name fails in an MCP client that launches it later. The interpreter path is
    deliberately not resolved: a venv's python is a symlink to the base
    interpreter, and resolving it would leave the venv."""
    bin_dir = Path(sys.executable).absolute().parent
    for name in (MCP_COMMAND, f"{MCP_COMMAND}.exe"):
        candidate = bin_dir / name
        if candidate.is_file():
            return str(candidate)
    return MCP_COMMAND


def claude_mcp_config(*, base_url: str, token: str, profile: str, command: str) -> dict:
    """The ``mcpServers`` block an operator pastes into an MCP client's config
    (Claude Desktop, Claude Code, or any MCP-speaking agent runner).

    ``profile`` is required so that no printed config falls back to ``full``
    by omission; callers pass ``seat_profile_for(token_scopes)`` and
    ``athena_mcp_command()``."""
    return {
        "mcpServers": {
            "athena": {
                "command": command,
                "env": {
                    "ATHENA_BASE_URL": base_url,
                    "ATHENA_TOKEN": token,
                    "ATHENA_MCP_PROFILE": profile,
                },
            }
        }
    }
