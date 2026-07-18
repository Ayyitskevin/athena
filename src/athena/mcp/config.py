"""The launch contract for connecting an AI agent to Athena over MCP.

An operator connects an agent by handing their MCP client three facts: run
`athena-mcp`, point ATHENA_BASE_URL at the deployment, authenticate with
ATHENA_TOKEN. This module is that contract's one home — the demo CLI prints it
and POST /users/onboard_agent returns it, so both surfaces always describe the
same block (one fact, one place). Deliberately stdlib-only: core imports this
for the config *shape*; nothing here touches the MCP SDK or httpx.
"""

from __future__ import annotations


def claude_mcp_config(*, base_url: str, token: str) -> dict:
    """The ``mcpServers`` block an operator pastes into an MCP client's config
    (Claude Desktop, Claude Code, or any MCP-speaking agent runner)."""
    return {
        "mcpServers": {
            "athena": {
                "command": "athena-mcp",
                "env": {
                    "ATHENA_BASE_URL": base_url,
                    "ATHENA_TOKEN": token,
                },
            }
        }
    }
