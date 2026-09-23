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
import secrets
import sys
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import AfterValidator, Field

from athena.aegis import (
    automation,
    delegations,
    fleet_metrics,
    fleet_work,
    issue_narrative,
    issues,
    lease_commands,
)
from athena.core import (
    dispatch,
    notifications,
    run_context,
    run_control_commands,
    run_controls,
    tokens,
)
from athena.mcp.client import AthenaClient, AthenaError
from athena.workflows import workspace_search


IdempotencyKey = Annotated[
    str,
    Field(min_length=1, max_length=255, pattern=r"^[\x21-\x7E]+$"),
]
ClaimYieldNote = Annotated[
    str, Field(max_length=lease_commands.MAX_CLAIM_YIELD_NOTE_CHARS)
]
HandoffAttemptedWork = Annotated[
    str,
    Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_ATTEMPTED_WORK_CHARS,
    ),
]
HandoffEvidenceItem = Annotated[
    str,
    Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_EVIDENCE_ITEM_CHARS,
    ),
]
HandoffEvidence = Annotated[
    list[HandoffEvidenceItem],
    Field(max_length=lease_commands.MAX_HANDOFF_EVIDENCE_ITEMS),
]
HandoffBlockingQuestion = Annotated[
    str,
    Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_BLOCKING_QUESTION_CHARS,
    ),
]
HandoffResumeInstructions = Annotated[
    str,
    Field(
        min_length=1,
        max_length=lease_commands.MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS,
    ),
]
HandoffResumeNote = Annotated[
    str,
    Field(max_length=lease_commands.MAX_HANDOFF_RESUME_NOTE_CHARS),
]
HandoffToken = Annotated[
    str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
]
LeaseGeneration = Annotated[
    str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
]


RunId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        pattern=(
            r"^[^\x00-\x1F\x7F]*"
            r"[^\s\x00-\x1F\x7F]"
            r"[^\x00-\x1F\x7F]*$"
        ),
    ),
    AfterValidator(run_context.strict_run_id),
]

DelegationLimit = Annotated[int, Field(ge=1, le=delegations.MAX_LIMIT)]
DelegationOffset = Annotated[int, Field(ge=0, le=delegations.MAX_OFFSET)]
IssueFilterId = Annotated[
    int,
    Field(strict=True, ge=0, le=issues.MAX_SQLITE_INTEGER),
]
ProjectId = Annotated[int, Field(strict=True, ge=1, le=issues.MAX_SQLITE_INTEGER)]
SprintId = Annotated[int, Field(strict=True, ge=1, le=issues.MAX_SQLITE_INTEGER)]
PermanentDeleteConfirmation = Annotated[bool, Field(strict=True)]
FleetMetricId = Annotated[
    int, Field(strict=True, ge=1, le=fleet_metrics.MAX_SQLITE_INTEGER)
]
FleetActorLimit = Annotated[
    int, Field(strict=True, ge=1, le=fleet_metrics.MAX_ACTOR_LIMIT)
]
FleetMetricDate = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
FleetWorkLimit = Annotated[int, Field(strict=True, ge=1, le=fleet_work.MAX_LIMIT)]
IssueHistoryLimit = Annotated[
    int, Field(strict=True, ge=1, le=issue_narrative.MAX_LIMIT)
]
AttentionWindowHours = Annotated[int, Field(strict=True, ge=1, le=168)]
AttentionLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
FleetAgentId = Annotated[int, Field(strict=True, ge=1, le=issues.MAX_SQLITE_INTEGER)]
DispatchIssueId = Annotated[
    int, Field(strict=True, ge=1, le=dispatch.MAX_SQLITE_INTEGER)
]
DispatchLimit = Annotated[int, Field(strict=True, ge=1, le=dispatch.MAX_LIST_LIMIT)]
AutomationTriggerType = Literal["event", "schedule"]
AutomationScheduleAt = Annotated[
    str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
]
AutomationScheduleInterval = Annotated[
    int,
    Field(
        strict=True,
        ge=automation.MIN_SCHEDULE_INTERVAL_SECONDS,
        le=automation.MAX_SCHEDULE_INTERVAL_SECONDS,
    ),
]
RunControlKind = Literal["steer", "request_cancel", "request_fresh_context"]
RunControlStateFilter = Literal[
    "requested", "acknowledged", "completed", "declined", "expired", "open"
]
RunControlId = Annotated[int, Field(strict=True, ge=1, le=issues.MAX_SQLITE_INTEGER)]
RunControlPayload = Annotated[
    str, Field(max_length=run_control_commands.MAX_PAYLOAD_CHARS)
]
RunControlSummary = Annotated[
    str, Field(min_length=1, max_length=run_control_commands.MAX_RESULT_SUMMARY_CHARS)
]
RunControlTtl = Annotated[
    int,
    Field(
        strict=True,
        ge=run_control_commands.MIN_TTL_SECONDS,
        le=run_control_commands.MAX_TTL_SECONDS,
    ),
]
RunControlListLimit = Annotated[
    int, Field(strict=True, ge=1, le=run_controls.MAX_LIST_LIMIT)
]
# Spelled out rather than derived from notifications.WATCHABLE_KINDS because a
# Literal must be static for the type checker. test_mcp_client asserts the two are
# the same set, so adding a watchable kind without exposing it here fails CI — the
# drift guard is a test, not a runtime import.
WorkspaceSearchLimit = Annotated[
    int,
    Field(
        strict=True,
        ge=workspace_search.MIN_LIMIT_PER_KIND,
        le=workspace_search.MAX_LIMIT_PER_KIND,
    ),
]
WatchKind = Literal["issue", "page", "space"]
WatchTargetId = Annotated[int, Field(strict=True, ge=1, le=issues.MAX_SQLITE_INTEGER)]
NotificationPriority = Literal["low", "normal", "medium", "high", "urgent"]
NotificationLimit = Annotated[int, Field(strict=True, ge=1, le=200)]
WatchPreferencePriority = Literal["low", "medium", "high", "urgent"]
DigestWindowMinutes = Annotated[
    int,
    Field(
        strict=True,
        ge=notifications.MIN_DIGEST_MINUTES,
        le=notifications.MAX_DIGEST_MINUTES,
    ),
]
MuteUntil = Annotated[str, Field(max_length=notifications.MAX_MUTE_UNTIL_CHARS)]

# --- tool scopes -------------------------------------------------------------
#
# What each tool needs from the token, mirroring the REST layer's enforcement —
# this is the entire authorization POLICY on one screen, audited against the
# route dependencies (and, where a route checks inside its command, against
# that command: dispatch and the worker kill are the two).
#
# Vocabulary:
#   "read"  — any authenticated token (reads gate on authentication, not scope)
#   "write" — any write scope (the personal-state rule: a read-only token must
#             never mutate anything, its own rows included)
#   a scope constant — that scope, with admin implying all of them, exactly as
#             identity.token_has_scope decides
#
# This drives PRESENTATION, not authorization: build_server registers only the
# tools the session's token can actually use, so a read-scoped agent does not
# carry ~10k tokens of mutation docstrings it can never call. The REST layer
# remains the boundary — a wrong entry here shows or hides a tool; it can never
# permit a call the server would refuse.
#
# Fail-closed against drift: registering a tool with no entry here raises at
# build time, so a new tool cannot ship unmapped.
ANY_WRITE_SCOPE = "write"
READ_ONLY = "read"
TOOL_SCOPES: dict[str, str] = {
    # reads — any authenticated token
    "search": READ_ONLY,
    "search_workspace": READ_ONLY,
    "list_issues": READ_ONLY,
    "search_work": READ_ONLY,
    "count_work": READ_ONLY,
    "read_page_embeds": READ_ONLY,
    "resolve_embeds": READ_ONLY,
    "embed_help": READ_ONLY,
    "link_graph": READ_ONLY,
    "related_items": READ_ONLY,
    "project_timeline": READ_ONLY,
    "unlinked_mentions": READ_ONLY,
    "query_help": READ_ONLY,
    "list_my_delegated_work": READ_ONLY,
    "get_fleet_active_work": READ_ONLY,
    "get_issue": READ_ONLY,
    "get_issue_work_context": READ_ONLY,
    "get_issue_history": READ_ONLY,
    "get_attention_ranking": READ_ONLY,
    "get_fleet_metrics": READ_ONLY,
    "list_issue_comments": READ_ONLY,
    "get_issue_state": READ_ONLY,
    "recent_events": READ_ONLY,
    "whoami": READ_ONLY,
    "list_notifications": READ_ONLY,
    "list_priority_notifications": READ_ONLY,
    "notification_priority_summary": READ_ONLY,
    "get_watch_preference": READ_ONLY,
    "begin_run": READ_ONLY,
    "current_run": READ_ONLY,
    "get_agent_run_health": READ_ONLY,
    "list_automation_rules": READ_ONLY,
    "get_automation_rule": READ_ONLY,
    "list_automation_failures": READ_ONLY,
    "list_activity_runs": READ_ONLY,
    "list_run_events": READ_ONLY,
    "get_run_lineage": READ_ONLY,
    "get_run_replay": READ_ONLY,
    "get_run_fork_contract": READ_ONLY,
    "list_dispatches": READ_ONLY,
    "get_issue_runbook": READ_ONLY,
    "my_desk": READ_ONLY,
    "my_office": READ_ONLY,
    "get_project_floor": READ_ONLY,
    "activity_chain_status": READ_ONLY,
    "verify_activity_chain": READ_ONLY,
    "list_workers": READ_ONLY,
    "list_run_controls": READ_ONLY,
    "get_run_control": READ_ONLY,
    "get_agent_budget": READ_ONLY,
    "get_issue_lease": READ_ONLY,
    "list_subtasks": READ_ONLY,
    "list_issue_links": READ_ONLY,
    "list_sprints": READ_ONLY,
    "get_sprint": READ_ONLY,
    "list_labels": READ_ONLY,
    "list_projects": READ_ONLY,
    "list_spaces": READ_ONLY,
    "list_pages": READ_ONLY,
    "get_page": READ_ONLY,
    "find_pages_by_title": READ_ONLY,
    "page_backlinks": READ_ONLY,
    "page_outgoing_links": READ_ONLY,
    "list_page_versions": READ_ONLY,
    "get_page_version": READ_ONLY,
    # personal state and agent lifecycle — any write scope
    "mark_notifications_read": ANY_WRITE_SCOPE,
    "watch": ANY_WRITE_SCOPE,
    "unwatch": ANY_WRITE_SCOPE,
    "set_watch_preference": ANY_WRITE_SCOPE,
    "clear_watch_preference": ANY_WRITE_SCOPE,
    "advance_desk_cursor": ANY_WRITE_SCOPE,
    "heartbeat_agent_run": ANY_WRITE_SCOPE,
    "worker_heartbeat": ANY_WRITE_SCOPE,
    "acknowledge_run_control": ANY_WRITE_SCOPE,
    "decline_run_control": ANY_WRITE_SCOPE,
    "complete_run_control": ANY_WRITE_SCOPE,
    "undo_action": ANY_WRITE_SCOPE,
    # writes EITHER module depending on source_kind; the route still enforces
    # the precise scope, this only decides presentation
    "link_mention": ANY_WRITE_SCOPE,
    # Aegis writes
    "create_issue": tokens.ISSUE_WRITE_SCOPE,
    "update_issue": tokens.ISSUE_WRITE_SCOPE,
    "set_issue_placement": tokens.ISSUE_WRITE_SCOPE,
    "assign_issue": tokens.ISSUE_WRITE_SCOPE,
    "delegate_issue": tokens.ISSUE_WRITE_SCOPE,
    "claim_issue": tokens.ISSUE_WRITE_SCOPE,
    "yield_claim": tokens.ISSUE_WRITE_SCOPE,
    "resume_claim_handoff": tokens.ISSUE_WRITE_SCOPE,
    "complete_claim": tokens.ISSUE_WRITE_SCOPE,
    "decline_delegation": tokens.ISSUE_WRITE_SCOPE,
    "comment_on_issue": tokens.ISSUE_WRITE_SCOPE,
    "archive_issue": tokens.ISSUE_WRITE_SCOPE,
    "unarchive_issue": tokens.ISSUE_WRITE_SCOPE,
    "bulk_update_issues": tokens.ISSUE_WRITE_SCOPE,
    "set_issue_parent": tokens.ISSUE_WRITE_SCOPE,
    "link_issues": tokens.ISSUE_WRITE_SCOPE,
    "unlink_issues": tokens.ISSUE_WRITE_SCOPE,
    "create_sprint": tokens.ISSUE_WRITE_SCOPE,
    "update_sprint": tokens.ISSUE_WRITE_SCOPE,
    "start_sprint": tokens.ISSUE_WRITE_SCOPE,
    "complete_sprint": tokens.ISSUE_WRITE_SCOPE,
    "delete_sprint": tokens.ISSUE_WRITE_SCOPE,
    "set_issue_sprint": tokens.ISSUE_WRITE_SCOPE,
    "create_label": tokens.ISSUE_WRITE_SCOPE,
    "attach_label": tokens.ISSUE_WRITE_SCOPE,
    "detach_label": tokens.ISSUE_WRITE_SCOPE,
    "start_playbook": tokens.ISSUE_WRITE_SCOPE,
    "dispatch_to_icarus": tokens.ISSUE_WRITE_SCOPE,
    # Mentor writes
    "create_page": tokens.DOCS_WRITE_SCOPE,
    "update_page": tokens.DOCS_WRITE_SCOPE,
    "archive_page": tokens.DOCS_WRITE_SCOPE,
    "unarchive_page": tokens.DOCS_WRITE_SCOPE,
    "label_page": tokens.DOCS_WRITE_SCOPE,
    "unlabel_page": tokens.DOCS_WRITE_SCOPE,
    "restore_page_version": tokens.DOCS_WRITE_SCOPE,
    "record_run_learning": tokens.DOCS_WRITE_SCOPE,
    # operator/admin surfaces (admin-gated reads included, so a non-admin
    # session does not carry tools that can only answer 403)
    "list_security_events": tokens.ADMIN_SCOPE,
    "agent_answerability": tokens.ADMIN_SCOPE,
    "list_approvals": tokens.ADMIN_SCOPE,
    "list_users": tokens.ADMIN_SCOPE,
    "onboard_agent": tokens.ADMIN_SCOPE,
    "pause_agent": tokens.ADMIN_SCOPE,
    "resume_agent": tokens.ADMIN_SCOPE,
    "revoke_agent_tokens": tokens.ADMIN_SCOPE,
    "offboard_agent": tokens.ADMIN_SCOPE,
    "create_automation_rule": tokens.ADMIN_SCOPE,
    "set_automation_rule_enabled": tokens.ADMIN_SCOPE,
    "delete_automation_rule": tokens.ADMIN_SCOPE,
    "request_worker_kill": tokens.ADMIN_SCOPE,
    "cancel_worker_kill": tokens.ADMIN_SCOPE,
    "create_run_control": tokens.ADMIN_SCOPE,
    "decide_approval": tokens.ADMIN_SCOPE,
    "set_approval_policy": tokens.ADMIN_SCOPE,
    "set_agent_budget": tokens.ADMIN_SCOPE,
    "clear_agent_budget": tokens.ADMIN_SCOPE,
}

_WRITE_SCOPES = frozenset(
    {tokens.ISSUE_WRITE_SCOPE, tokens.DOCS_WRITE_SCOPE, tokens.ADMIN_SCOPE}
)


def _tool_visible(required: str, scopes: frozenset[str] | None) -> bool:
    """Whether a tool with this requirement belongs in this session's surface.

    Mirrors ``identity.token_has_scope``: admin implies every scope, and a None
    scope set (auth that is not scope-limited) sees everything."""
    if scopes is None or required == READ_ONLY:
        return True
    if tokens.ADMIN_SCOPE in scopes:
        return True
    if required == ANY_WRITE_SCOPE:
        return bool(scopes & _WRITE_SCOPES)
    return required in scopes


def probe_scopes(client: AthenaClient) -> frozenset[str] | None:
    """Ask Athena what the session's token may do, failing OPEN to the full
    surface. Filtering is ergonomics; the REST layer is the boundary — so an
    unreachable server at MCP startup must not change which tools exist, only
    who answers 403 later.

    Called by ``main()`` — the stdio entry point — and deliberately NOT by
    ``build_server`` itself: a probe hidden inside the builder would prepend a
    surprise whoami to every test harness and every embedding caller. The one
    place that wants the probe wires it."""
    if os.environ.get("ATHENA_MCP_ALL_TOOLS") == "1":
        return None
    try:
        scopes = client.whoami().get("scopes")
    except Exception:  # noqa: BLE001 — startup probe; the boundary is REST
        print(
            "athena-mcp: could not read token scopes at startup; "
            "presenting the full tool surface",
            file=sys.stderr,
        )
        return None
    return None if scopes is None else frozenset(scopes)


def build_server(
    client: AthenaClient, *, scopes: frozenset[str] | None = None
) -> FastMCP:
    """Build the MCP server, binding every tool to a pre-built Athena client.
    Separated from main() so tests can inject a TestClient-backed AthenaClient.

    ``scopes`` narrows registration to the tools that token class can use
    (``TOOL_SCOPES``): a read-scoped agent gets the read surface, not ~70
    mutation tools that could only answer 403. None — the default — registers
    everything, so embedding callers and tests get the full deterministic
    surface unless they opt in; ``main()`` opts in by wiring ``probe_scopes``."""
    token_scopes = scopes
    mcp = FastMCP("athena")

    idempotency_guidance = (
        "For retry-critical calls, choose a stable, non-secret idempotency_key "
        "containing 1-255 visible ASCII characters before the first attempt and "
        "reuse it only for the exact same call. For tools exposing if_match, first "
        "call the matching read (get_issue for an issue, get_page for a page) and "
        "copy its _etag exactly. If a write returns 412, refetch the resource, merge "
        "the intended change with its current state, and retry with the refreshed "
        "_etag plus a new idempotency_key because the changed precondition makes it a "
        "different call."
    )

    def preserve_athena_errors(function):
        """Keep REST error metadata machine-readable across the MCP boundary."""

        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except AthenaError as exc:
                error_json = json.dumps(
                    exc.as_dict(), separators=(",", ":"), ensure_ascii=False
                )
                raise RuntimeError(f"{exc}\nATHENA_ERROR_JSON={error_json}") from exc

        return guarded

    def _required_scope(function) -> str:
        """The tool's declared scope — refusing to register an unmapped tool.
        Fail-closed on purpose: a new tool with no ``TOOL_SCOPES`` entry breaks
        the build (and every test that builds a server), so the policy screen
        above cannot silently fall behind the tool list."""
        try:
            return TOOL_SCOPES[function.__name__]
        except KeyError:
            raise RuntimeError(
                f"MCP tool {function.__name__!r} has no TOOL_SCOPES entry; "
                "every tool must declare what its token needs"
            ) from None

    def tool(function):
        """Register a tool with the shared structured-error contract."""
        if not _tool_visible(_required_scope(function), token_scopes):
            return function
        return mcp.tool()(preserve_athena_errors(function))

    def mutation_tool(function):
        """Register a write tool with the shared retry-key contract."""
        if not _tool_visible(_required_scope(function), token_scopes):
            return function
        guarded = preserve_athena_errors(function)
        guarded.__doc__ = f"{function.__doc__.rstrip()}\n\n{idempotency_guidance}"
        return mcp.tool()(guarded)

    from athena.mcp.tools_read import register_read_tools
    from athena.mcp.tools_admin import register_admin_tools
    from athena.mcp.tools_work import register_work_tools
    from athena.mcp.tools_pages import register_page_tools

    register_read_tools(tool, mutation_tool, client)
    register_admin_tools(tool, mutation_tool, client)
    register_work_tools(tool, mutation_tool, client)
    register_page_tools(tool, mutation_tool, client)
    return mcp


def main() -> None:
    """Entry point for the `athena-mcp` script. Reads ATHENA_BASE_URL and
    ATHENA_TOKEN from the environment and serves over stdio.

    Every session gets a run identity: ATHENA_RUN_ID if set (with optional
    ATHENA_PARENT_RUN_ID), otherwise a fresh `mcp-<hex>` id minted here — so
    every write an agent performs through this server is attributed to a run in
    the activity trail BY DEFAULT, visible in Mission Control and replayable,
    rather than falling into the untagged time-gap heuristic. The agent can
    switch runs mid-session with the begin_run tool."""
    base_url = os.environ.get("ATHENA_BASE_URL", "http://127.0.0.1:8000")
    token = os.environ.get("ATHENA_TOKEN")
    run_id = os.environ.get("ATHENA_RUN_ID") or f"mcp-{secrets.token_hex(6)}"
    parent_run_id = os.environ.get("ATHENA_PARENT_RUN_ID") or None
    client = AthenaClient(
        base_url=base_url, token=token, run_id=run_id, parent_run_id=parent_run_id
    )
    # The session-defining call: one whoami decides which tools this token's
    # session carries. Fails open; ATHENA_MCP_ALL_TOOLS=1 skips entirely.
    build_server(client, scopes=probe_scopes(client)).run()
