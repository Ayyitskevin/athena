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

from functools import wraps
import json
import os
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from athena.mcp.client import AthenaClient, AthenaError


IdempotencyKey = Annotated[
    str,
    Field(min_length=1, max_length=255, pattern=r"^[\x21-\x7E]+$"),
]


def build_server(client: AthenaClient) -> FastMCP:
    """Build the MCP server, binding every tool to a pre-built Athena client.
    Separated from main() so tests can inject a TestClient-backed AthenaClient."""
    mcp = FastMCP("athena")

    idempotency_guidance = (
        "For retry-critical calls, choose a stable, non-secret idempotency_key "
        "containing 1-255 visible ASCII characters before the first attempt and "
        "reuse it only for the exact same call. For tools exposing if_match, first "
        "call get_issue and copy its _etag exactly. If a write returns 412, refetch "
        "the issue, merge the intended change with its current state, and retry with "
        "the refreshed _etag plus a new idempotency_key because the changed "
        "precondition makes it a different call."
    )

    def mutation_tool(function):
        """Register a write tool with the shared retry-key contract."""

        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except AthenaError as exc:
                error_json = json.dumps(
                    exc.as_dict(), separators=(",", ":"), ensure_ascii=False
                )
                raise RuntimeError(f"{exc}\nATHENA_ERROR_JSON={error_json}") from exc

        guarded.__doc__ = f"{function.__doc__.rstrip()}\n\n{idempotency_guidance}"
        return mcp.tool()(guarded)

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
        include_archived: bool = False,
    ) -> list:
        """List Aegis issues, optionally filtered by status (open/in_progress/done),
        project (id or 'none' for the backlog), label name, or a text substring.
        Archived issues are hidden by default; pass include_archived=true to see them."""
        return client.list_issues(
            status=status,
            project=project,
            label=label,
            search=search,
            include_archived=include_archived,
        )

    @mcp.tool()
    def get_issue(ref: str) -> dict:
        """Get one issue by numeric id ('12') or project key ('ATH-12'). The
        response includes the server's opaque ETag as _etag; copy it exactly into
        if_match on a guarded update."""
        return client.get_issue(ref)

    @mcp.tool()
    def get_issue_state(issue_id: int, as_of_event_id: int | None = None) -> dict:
        """Reconstruct an issue's lifecycle state from the activity log. Pass
        as_of_event_id to time-travel to the state at that activity checkpoint; omit
        it for the current lifecycle state. Content fields are intentionally absent."""
        return client.get_issue_state(issue_id, as_of_event_id=as_of_event_id)

    @mcp.tool()
    def recent_events(after: int | None = None, kind: str | None = None) -> dict:
        """Read the audit/event feed in order. Pass the last event id you saw as
        `after` to get only newer events; optionally filter by kind (issue/page).
        Returns {events, next_after, has_more}."""
        return client.recent_events(after=after, kind=kind)


    @mcp.tool()
    def get_agent_run_health(agent_id: int | None = None) -> dict:
        """Read the admin-only fleet cockpit rollup: each agent's bounded recent
        runs, tagged/heuristic replay posture, lineage counts, and fleet totals.
        Activity proves recent actions, not that an external agent is running now."""
        return client.get_agent_run_health(agent_id=agent_id)

    @mcp.tool()
    def list_automation_failures() -> list:
        """Read the admin-only exception list of automation rules whose actions have
        failed. Failure counts are cumulative; inspect the rule before intervening."""
        return client.list_automation_failures()

    @mcp.tool()
    def list_activity_runs(
        actor_id: int, gap_seconds: int = 1800, limit: int = 200
    ) -> list:
        """Reconstruct one actor's recent activity into runs. Explicit X-Athena-Run
        ids are authoritative; untagged work falls back to a time-gap heuristic."""
        return client.list_activity_runs(
            actor_id=actor_id, gap_seconds=gap_seconds, limit=limit
        )

    @mcp.tool()
    def get_run_lineage(run_id: str) -> dict:
        """Read a tagged run's causal tree: ancestors, the focal run's replayable
        events, and descendant runs spawned from it."""
        return client.get_run_lineage(run_id)

    @mcp.tool()
    def get_run_fork_contract(
        run_id: str, fork_from_event_id: int, fork_run_id: str
    ) -> dict:
        """Validate a fork point inside a parent run and return the child-run
        headers to use on subsequent writes, plus the visible shared-prefix events.
        This creates no state; the child run begins when later writes use the headers."""
        return client.get_run_fork_contract(
            run_id,
            fork_from_event_id=fork_from_event_id,
            fork_run_id=fork_run_id,
        )

    # --- issue writes -------------------------------------------------------

    @mutation_tool
    def create_issue(
        title: str,
        body: str = "",
        status: str | None = None,
        priority: str = "medium",
        project_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create an Aegis issue. Omit status to use the target project's default;
        otherwise status must belong to that project. priority is one of
        low/medium/high/urgent. Bodies support Markdown and [[issue:N]]/[[page:N]]
        cross-links."""
        return client.create_issue(
            title=title,
            body=body,
            status=status,
            priority=priority,
            project_id=project_id,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def update_issue(
        issue_id: int,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Update an issue. Send only the fields to change. status is one of
        open/in_progress/done; priority is low/medium/high/urgent."""
        return client.update_issue(
            issue_id,
            title=title,
            body=body,
            status=status,
            priority=priority,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def set_issue_placement(
        issue_id: int,
        project_id: int | None,
        sprint_id: int | None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Atomically set an issue's project and sprint. Always provide BOTH
        project_id and sprint_id: use null project_id to move the issue out of a
        project, and null sprint_id for no sprint. A non-null sprint must belong to
        the supplied project. The pair is validated and committed as one transition,
        so use this tool instead of separate project and sprint moves."""
        return client.set_issue_placement(
            issue_id,
            project_id=project_id,
            sprint_id=sprint_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def assign_issue(
        issue_id: int,
        assignee_id: int | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Assign an issue to a user id, or pass no assignee_id to unassign it."""
        return client.assign_issue(
            issue_id,
            assignee_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def delegate_issue(
        issue_id: int,
        agent_user_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> list:
        """Delegate an issue to an agent user. The human assignee remains accountable;
        the agent is added as a contributor and a delegated audit event is recorded.
        Use list_users to resolve agent user ids."""
        return client.delegate_issue(
            issue_id, agent_user_id, idempotency_key=idempotency_key
        )

    @mutation_tool
    def comment_on_issue(
        issue_id: int, body: str, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Add a comment to an issue, authored by the token's user."""
        return client.comment_on_issue(issue_id, body, idempotency_key=idempotency_key)

    @mutation_tool
    def archive_issue(
        issue_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Archive (soft-delete) an issue: it's hidden from the default lists but the
        row and its history are kept, and it can be restored. Returns the issue."""
        return client.archive_issue(issue_id, idempotency_key=idempotency_key)

    @mutation_tool
    def unarchive_issue(
        issue_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Restore a previously archived issue to the active lists. Returns the issue."""
        return client.unarchive_issue(issue_id, idempotency_key=idempotency_key)

    @mutation_tool
    def bulk_update_issues(
        ids: list[int],
        status: str | None = None,
        priority: str | None = None,
        assignee_id: int | None = None,
        sprint_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Apply the same change to MANY issues at once (one call instead of N).
        Set any of status (open/in_progress/done), priority (low/medium/high/urgent),
        assignee_id, or sprint_id; only the fields you pass are touched. Best-effort:
        each issue is authorized and validated on its own, so the result reports
        {updated, failed, results:[{id, ok, error}]} — one issue's failure doesn't
        stop the rest. (To CLEAR an assignee or sprint, use the per-issue tools.)"""
        return client.bulk_update_issues(
            ids,
            status=status,
            priority=priority,
            assignee_id=assignee_id,
            sprint_id=sprint_id,
            idempotency_key=idempotency_key,
        )

    # --- hierarchy (epics & sub-tasks) --------------------------------------

    @mutation_tool
    def set_issue_parent(
        issue_id: int,
        parent_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Nest an issue under a parent issue (making it a sub-task), or pass no
        parent_id to move it back to the top level. The parent must not create a
        cycle. Returns the updated issue."""
        return client.set_issue_parent(
            issue_id, parent_id, idempotency_key=idempotency_key
        )

    @mcp.tool()
    def list_subtasks(issue_id: int) -> list:
        """List the direct child issues (sub-tasks) nested under this issue."""
        return client.list_subtasks(issue_id)

    # --- dependencies (blocks / relates) ------------------------------------

    @mcp.tool()
    def list_issue_links(issue_id: int) -> dict:
        """Read an issue's dependency links — what it blocks, what blocks it, and
        what it relates to. Returns {blocks, blocked_by, relates}."""
        return client.list_issue_links(issue_id)

    @mutation_tool
    def link_issues(
        issue_id: int,
        target_ref: str,
        relation: str,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Declare a dependency FROM this issue to another (by id or key, e.g.
        'ATH-15'). relation is one of: 'blocks', 'blocked_by', 'relates'. Returns
        the issue's updated link summary."""
        return client.link_issues(
            issue_id, target_ref, relation, idempotency_key=idempotency_key
        )

    @mutation_tool
    def unlink_issues(
        issue_id: int,
        relation: str,
        target_id: int,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Remove a dependency from this issue: the same relation used to add it
        ('blocks'/'blocked_by'/'relates') and the other issue's numeric id."""
        return client.unlink_issues(
            issue_id, relation, target_id, idempotency_key=idempotency_key
        )

    # --- sprints ------------------------------------------------------------

    @mcp.tool()
    def list_sprints(project_id: int, state: str | None = None) -> list:
        """List a project's sprints, optionally filtered by state
        (planned/active/completed)."""
        return client.list_sprints(project_id, state=state)

    @mutation_tool
    def set_issue_sprint(
        issue_id: int,
        sprint_id: int | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Put an issue into a sprint (which must belong to the issue's own
        project), or pass no sprint_id to move it back to the backlog. Returns the
        updated issue."""
        return client.set_issue_sprint(
            issue_id,
            sprint_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    # --- labels -------------------------------------------------------------

    @mcp.tool()
    def list_labels() -> list:
        """List the shared label vocabulary (id, name, color)."""
        return client.list_labels()

    @mutation_tool
    def create_label(
        name: str,
        color: str = "#6b7280",
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create a label in the shared vocabulary. color is a #RRGGBB hex string.
        Fails if a label with that name already exists (names are case-insensitive)."""
        return client.create_label(name, color=color, idempotency_key=idempotency_key)

    @mutation_tool
    def attach_label(
        issue_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Attach an existing label (by id — see list_labels) to an issue.
        Idempotent. Returns the updated issue."""
        return client.attach_label(issue_id, label_id, idempotency_key=idempotency_key)

    @mutation_tool
    def detach_label(
        issue_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Remove a label from an issue. Returns the updated issue."""
        return client.detach_label(issue_id, label_id, idempotency_key=idempotency_key)

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

    @mutation_tool
    def create_page(
        space_id: int,
        title: str,
        body: str = "",
        parent_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create a Mentor page in a space. Optionally nest it under parent_id (a
        page in the same space). Bodies support Markdown and cross-links."""
        return client.create_page(
            space_id=space_id,
            title=title,
            body=body,
            parent_id=parent_id,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def update_page(
        page_id: int,
        title: str | None = None,
        body: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Update a page's title and/or body. Each edit is snapshotted into the
        page's version history automatically."""
        return client.update_page(
            page_id,
            title=title,
            body=body,
            idempotency_key=idempotency_key,
        )

    return mcp


def main() -> None:
    """Entry point for the `athena-mcp` script. Reads ATHENA_BASE_URL and
    ATHENA_TOKEN from the environment and serves over stdio."""
    base_url = os.environ.get("ATHENA_BASE_URL", "http://127.0.0.1:8000")
    token = os.environ.get("ATHENA_TOKEN")
    client = AthenaClient(base_url=base_url, token=token)
    build_server(client).run()
