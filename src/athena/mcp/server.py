"""An MCP server that exposes Athena to AI agents.

Run it with the `mcp` extra installed and a scoped Athena token:

    ATHENA_BASE_URL=http://127.0.0.1:8000 ATHENA_TOKEN=ath_... athena-mcp

It speaks MCP over stdio (how desktop/agent clients launch tool servers) and turns
each tool call into a REST call against a running Athena, authenticated by the
token. Because it goes through the API, an agent acting via MCP is bound by the
same scopes and leaves the same audit trail as any other client — "Grok closed
AEGIS-88" stays first-class history, whether Grok used the web UI, curl, or this.

The tool *descriptions* below are what the agent reads to decide what to call, so
they are written for that audience. The wiring imports the MCP SDK; the actual
HTTP work lives in client.py (SDK-free, unit-tested).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from athena.mcp.client import AthenaClient


def build_server(client: AthenaClient) -> FastMCP:
    """Build the MCP server, binding every tool to a pre-built Athena client.
    Separated from main() so tests can inject a TestClient-backed AthenaClient."""
    mcp = FastMCP("athena")

    # --- search & read ------------------------------------------------------

    @mcp.tool()
    def search(query: str, kind: str | None = None) -> list:
        """Full-text search across Aegis issues and Mentor pages. Optionally narrow
        to kind='issue' or kind='page'. Returns ranked hits with title + snippet."""
        return client.search(query, kind=kind)

    @mcp.tool()
    def list_issues(
        status: str | None = None,
        project: str | None = None,
        label: str | None = None,
        search: str | None = None,
    ) -> list:
        """List Aegis issues, optionally filtered by status (open/in_progress/done),
        project (id or 'none' for the backlog), label name, or a text substring."""
        return client.list_issues(
            status=status, project=project, label=label, search=search
        )

    @mcp.tool()
    def get_issue(ref: str) -> dict:
        """Get one issue by numeric id ('12') or project key ('ATH-12')."""
        return client.get_issue(ref)

    @mcp.tool()
    def recent_events(after: int | None = None, kind: str | None = None) -> dict:
        """Read the audit/event feed in order. Pass the last event id you saw as
        `after` to get only newer events; optionally filter by kind (issue/page).
        Returns {events, next_after, has_more}."""
        return client.recent_events(after=after, kind=kind)

    # --- issue writes -------------------------------------------------------

    @mcp.tool()
    def create_issue(
        title: str,
        body: str = "",
        priority: str = "medium",
        project_id: int | None = None,
    ) -> dict:
        """Create an Aegis issue. priority is one of low/medium/high/urgent. Bodies
        support Markdown and [[issue:N]]/[[page:N]] cross-links."""
        return client.create_issue(
            title=title, body=body, priority=priority, project_id=project_id
        )

    @mcp.tool()
    def update_issue(
        issue_id: int,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> dict:
        """Update an issue. Send only the fields to change. status is one of
        open/in_progress/done; priority is low/medium/high/urgent."""
        return client.update_issue(
            issue_id, title=title, body=body, status=status, priority=priority
        )

    @mcp.tool()
    def assign_issue(issue_id: int, assignee_id: int | None = None) -> dict:
        """Assign an issue to a user id, or pass no assignee_id to unassign it."""
        return client.assign_issue(issue_id, assignee_id)

    @mcp.tool()
    def comment_on_issue(issue_id: int, body: str) -> dict:
        """Add a comment to an issue, authored by the token's user."""
        return client.comment_on_issue(issue_id, body)

    # --- projects & users ---------------------------------------------------

    @mcp.tool()
    def list_projects() -> list:
        """List Aegis projects (id, key, name)."""
        return client.list_projects()

    @mcp.tool()
    def list_users() -> list:
        """List users (id, name, role) — useful for resolving an assignee."""
        return client.list_users()

    # --- pages (Mentor) -----------------------------------------------------

    @mcp.tool()
    def list_spaces() -> list:
        """List Mentor spaces (id, key, name)."""
        return client.list_spaces()

    @mcp.tool()
    def list_pages(space_id: int) -> list:
        """List the pages in a space."""
        return client.list_pages(space_id)

    @mcp.tool()
    def get_page(page_id: int) -> dict:
        """Get one Mentor page (title + Markdown body)."""
        return client.get_page(page_id)

    @mcp.tool()
    def create_page(
        space_id: int, title: str, body: str = "", parent_id: int | None = None
    ) -> dict:
        """Create a Mentor page in a space. Optionally nest it under parent_id (a
        page in the same space). Bodies support Markdown and cross-links."""
        return client.create_page(
            space_id=space_id, title=title, body=body, parent_id=parent_id
        )

    @mcp.tool()
    def update_page(
        page_id: int, title: str | None = None, body: str | None = None
    ) -> dict:
        """Update a page's title and/or body. Each edit is snapshotted into the
        page's version history automatically."""
        return client.update_page(page_id, title=title, body=body)

    return mcp


def main() -> None:
    """Entry point for the `athena-mcp` script. Reads ATHENA_BASE_URL and
    ATHENA_TOKEN from the environment and serves over stdio."""
    base_url = os.environ.get("ATHENA_BASE_URL", "http://127.0.0.1:8000")
    token = os.environ.get("ATHENA_TOKEN")
    client = AthenaClient(base_url=base_url, token=token)
    build_server(client).run()
