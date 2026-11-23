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
from typing import Literal, get_args

# The console script name pyproject.toml installs for athena.mcp.server:main.
MCP_COMMAND = "athena-mcp"

# Profile names, spelled as athena.mcp.server.PROFILES spells them (a test holds
# the two together; importing the server here would pull in the MCP SDK).
#: The profiles onboarding may be asked to print: a closed set, spelled here
#: once. The REST body and the MCP tool take this type; every other spelling
#: derives from it. `full` is not one: an unset ATHENA_MCP_PROFILE already means
#: it, and no printed config should hand an agent every tool its token allows.
PrintableProfile = Literal["seat", "seat+knowledge", "conductor"]
PRINTABLE_PROFILES: tuple[str, ...] = get_args(PrintableProfile)
SEAT_PROFILE, SEAT_KNOWLEDGE_PROFILE, CONDUCTOR_PROFILE = PRINTABLE_PROFILES
# The scope that decides between them, and the one that implies every scope
# (identity.token_has_scope's rule), spelled as core.tokens spells them.
_ISSUE_WRITE_SCOPE = "issue:write"
_DOCS_WRITE_SCOPE = "docs:write"
_ADMIN_SCOPE = "admin"
#: The scopes under which every tool of a profile registers: fewer, and the
#: server silently starts the profile without some of its tools (the profile is
#: intersected with the token's scopes). tests/test_mcp_profiles.py holds this
#: table to what athena.mcp.server registers.
PROFILE_SCOPES: dict[str, frozenset[str]] = {
    SEAT_PROFILE: frozenset({_ISSUE_WRITE_SCOPE}),
    SEAT_KNOWLEDGE_PROFILE: frozenset({_ISSUE_WRITE_SCOPE, _DOCS_WRITE_SCOPE}),
    CONDUCTOR_PROFILE: frozenset({_ISSUE_WRITE_SCOPE}),
}


def scopes_missing_for(profile: str, scopes: Iterable[str]) -> list[str]:
    """The scopes a token lacks for every tool of ``profile`` to register, in a
    stable order; empty when it has them (an ``admin`` token has every scope)."""
    granted = set(scopes)
    if _ADMIN_SCOPE in granted:
        return []
    return sorted(PROFILE_SCOPES[profile] - granted)


def printed_profile(requested: str | None, scopes: Iterable[str]) -> str:
    """The profile a printed config names: ``requested`` when given, checked
    against the token's scopes; else :func:`seat_profile_for`, unchecked.

    Only an explicitly requested profile is checked: one outside
    ``PRINTABLE_PROFILES``, or one some of whose tools the scopes would hide,
    raises ``ValueError`` naming why. The default is today's: the widest seat
    profile the scopes suggest (``seat+knowledge`` with ``docs:write`` or
    ``admin``, else ``seat``), printed even for a read-only token."""
    granted = list(scopes)
    if requested is None:
        return seat_profile_for(granted)
    if requested not in PRINTABLE_PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PRINTABLE_PROFILES)}")
    missing = scopes_missing_for(requested, granted)
    if missing:
        raise ValueError(
            f"the {requested} profile needs the scope(s) {', '.join(missing)}; "
            "grant them or print another profile"
        )
    return requested


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
