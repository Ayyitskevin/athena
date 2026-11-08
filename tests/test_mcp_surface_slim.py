"""The MCP surface says each shared rule once, hides what a seat cannot call, and
takes the argument every write tool already takes.

WHY: a seat's session carried 115 tools and about 26.6k tokens of descriptions and
schemas before it did anything, and a quarter of that was one retry-key/ETag
paragraph appended to 63 tools. Seven admin-only reads were registered for every
seat and could only answer 403. The two issue reads took `ref` while every write
tool takes `issue_id`, so a naive first call after `my_desk()` was a validation
error. A repeated claim answered 428 without saying that the caller already held
the issue or what to send. Each of those is pinned here, and every hiding or
refusal rule has a positive control beside it, so a harness that cannot see the
thing it hides cannot pass by accident.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from athena.core import tokens
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}
SEAT_SCOPES = frozenset({"read", "issue:write", "docs:write"})
# The seven reads whose REST routes carry the admin dependency
# (core/activity_api.py chain, chain/verify, agent-runs; aegis/automation_api.py
# rules; aegis/fleet_work_api.py active-work) but which were mapped `read`.
ADMIN_ONLY_READS = frozenset(
    {
        "activity_chain_status",
        "verify_activity_chain",
        "get_agent_run_health",
        "get_automation_rule",
        "list_automation_rules",
        "list_automation_failures",
        "get_fleet_active_work",
    }
)
# The paragraph that used to be appended to every mutation tool, by its opening.
OLD_APPEND = "choose a stable, non-secret idempotency_key"
POINTER = "see the server instructions"
DOCS = Path(__file__).resolve().parents[1] / "docs"


class _NoServer:
    """No reachable Athena: listing tools must not need one."""


def _server(scopes):
    return mcp_server.build_server(_NoServer(), scopes=scopes)


def _tools(scopes=None) -> dict:
    return {tool.name: tool for tool in asyncio.run(_server(scopes).list_tools())}


def _visible(scope_map: dict[str, str], scopes) -> set[str]:
    return {
        name
        for name, required in scope_map.items()
        if mcp_server._tool_visible(required, scopes)
    }


def _call(server, name: str, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result[0].text)


def _athena_error(exc: BaseException) -> dict | None:
    """The structured REST error a tool failure carries, or None when the failure
    happened before any request was made (a validation error, for example)."""
    text = str(exc)
    marker = "ATHENA_ERROR_JSON="
    if marker not in text:
        return None
    return json.loads(text.split(marker, 1)[1])


# --- 1. the shared guidance, stated once ----------------------------------------


def test_the_retry_and_etag_guidance_is_server_instructions_not_63_appends():
    server = _server(None)
    instructions = server.instructions or ""
    assert "idempotency_key" in instructions
    assert "if_match" in instructions
    assert "412" in instructions
    # The fold: one issue ETag with two names, and the work-context packet's own
    # tag is not it.
    assert "issue_etag" in instructions
    assert "_etag" in instructions
    assert "same value" in instructions
    descriptions = {
        tool.name: tool.description or "" for tool in asyncio.run(server.list_tools())
    }
    still_carrying = sorted(n for n, d in descriptions.items() if OLD_APPEND in d)
    assert still_carrying == []
    assert OLD_APPEND not in instructions  # restated in our own words, once


def test_every_write_tool_keeps_a_one_line_pointer_and_nothing_else():
    tools = _tools(None)
    pointing = {n for n, t in tools.items() if POINTER in (t.description or "")}
    takes_key_or_tag = {
        n
        for n, t in tools.items()
        if {"idempotency_key", "if_match"}
        & set((t.inputSchema or {}).get("properties", {}))
    }
    assert takes_key_or_tag <= pointing, sorted(takes_key_or_tag - pointing)
    for name in pointing:
        description = tools[name].description or ""
        assert description.count(POINTER) == 1, name
        pointer_line = next(
            line for line in description.splitlines() if POINTER in line
        )
        assert len(pointer_line.strip()) <= 80, (name, pointer_line)
    reads = {
        n for n, req in mcp_server.TOOL_SCOPES.items() if req == mcp_server.READ_ONLY
    }
    assert not pointing & reads, sorted(pointing & reads)


# --- 2. the seven admin reads ----------------------------------------------------


def test_admin_only_reads_are_hidden_from_a_seat_and_shown_to_an_admin():
    seat = set(_tools(SEAT_SCOPES))
    admin = set(_tools(frozenset({tokens.ADMIN_SCOPE})))
    assert not seat & ADMIN_ONLY_READS, sorted(seat & ADMIN_ONLY_READS)
    assert ADMIN_ONLY_READS <= admin  # positive control: hidden, not deleted
    assert {"get_issue", "my_desk", "claim_issue", "create_page"} <= seat
    for name in sorted(ADMIN_ONLY_READS):
        assert mcp_server.TOOL_SCOPES[name] == tokens.ADMIN_SCOPE, name


_ADMIN_WORDS = re.compile(r"admin[- ]only|\(admin|admin role|operator[- ]only", re.I)


def test_no_read_scoped_tool_describes_itself_as_admin_only():
    # WHY: the drift guard that would have caught the seven on the day they were
    # mapped. The description is what the agent reads; if it says admin-only, the
    # map must say so too.
    tools = _tools(None)
    hits = sorted(
        name
        for name, tool in tools.items()
        if mcp_server.TOOL_SCOPES[name] == mcp_server.READ_ONLY
        and _ADMIN_WORDS.search(tool.description or "")
    )
    assert hits == []


def test_the_admin_word_guard_matches_how_the_seven_describe_themselves():
    # The negative self-test: the guard fails on a violation, it does not pass
    # because the regex never matches anything.
    assert _ADMIN_WORDS.search("Read the admin-only fleet cockpit rollup")
    assert _ADMIN_WORDS.search("Where the chain stands (admin only).")
    assert not _ADMIN_WORDS.search("Admin tokens see every project.")


# The map as it stood at f7308d5, before this change. A tool absent from both this
# snapshot and the live map is ignored; a tool in both must not be visible to any
# token class it was hidden from then. That is the intersection: now <= then.
_SCOPES_AT_F7308D5: dict[str, str] = {
    "acknowledge_run_control": mcp_server.ANY_WRITE_SCOPE,
    "activity_chain_status": mcp_server.READ_ONLY,
    "advance_desk_cursor": mcp_server.ANY_WRITE_SCOPE,
    "agent_answerability": tokens.ADMIN_SCOPE,
    "archive_issue": tokens.ISSUE_WRITE_SCOPE,
    "archive_page": tokens.DOCS_WRITE_SCOPE,
    "assign_issue": tokens.ISSUE_WRITE_SCOPE,
    "attach_label": tokens.ISSUE_WRITE_SCOPE,
    "begin_run": mcp_server.READ_ONLY,
    "bulk_update_issues": tokens.ISSUE_WRITE_SCOPE,
    "cancel_worker_kill": tokens.ADMIN_SCOPE,
    "claim_issue": tokens.ISSUE_WRITE_SCOPE,
    "clear_agent_budget": tokens.ADMIN_SCOPE,
    "clear_watch_preference": mcp_server.ANY_WRITE_SCOPE,
    "comment_on_issue": tokens.ISSUE_WRITE_SCOPE,
    "complete_claim": tokens.ISSUE_WRITE_SCOPE,
    "complete_run_control": mcp_server.ANY_WRITE_SCOPE,
    "complete_sprint": tokens.ISSUE_WRITE_SCOPE,
    "count_work": mcp_server.READ_ONLY,
    "create_automation_rule": tokens.ADMIN_SCOPE,
    "create_issue": tokens.ISSUE_WRITE_SCOPE,
    "create_label": tokens.ISSUE_WRITE_SCOPE,
    "create_page": tokens.DOCS_WRITE_SCOPE,
    "create_run_control": tokens.ADMIN_SCOPE,
    "create_sprint": tokens.ISSUE_WRITE_SCOPE,
    "current_run": mcp_server.READ_ONLY,
    "decide_approval": tokens.ADMIN_SCOPE,
    "decline_delegation": tokens.ISSUE_WRITE_SCOPE,
    "decline_run_control": mcp_server.ANY_WRITE_SCOPE,
    "delegate_issue": tokens.ISSUE_WRITE_SCOPE,
    "delete_automation_rule": tokens.ADMIN_SCOPE,
    "delete_sprint": tokens.ISSUE_WRITE_SCOPE,
    "detach_label": tokens.ISSUE_WRITE_SCOPE,
    "dispatch_to_icarus": tokens.ISSUE_WRITE_SCOPE,
    "embed_help": mcp_server.READ_ONLY,
    "find_pages_by_title": mcp_server.READ_ONLY,
    "get_agent_budget": mcp_server.READ_ONLY,
    "get_agent_run_health": mcp_server.READ_ONLY,
    "get_attention_ranking": mcp_server.READ_ONLY,
    "get_automation_rule": mcp_server.READ_ONLY,
    "get_fleet_active_work": mcp_server.READ_ONLY,
    "get_fleet_metrics": mcp_server.READ_ONLY,
    "get_issue": mcp_server.READ_ONLY,
    "get_issue_history": mcp_server.READ_ONLY,
    "get_issue_lease": mcp_server.READ_ONLY,
    "get_issue_runbook": mcp_server.READ_ONLY,
    "get_issue_state": mcp_server.READ_ONLY,
    "get_issue_work_context": mcp_server.READ_ONLY,
    "get_page": mcp_server.READ_ONLY,
    "get_page_version": mcp_server.READ_ONLY,
    "get_project_floor": mcp_server.READ_ONLY,
    "get_run_control": mcp_server.READ_ONLY,
    "get_run_fork_contract": mcp_server.READ_ONLY,
    "get_run_lineage": mcp_server.READ_ONLY,
    "get_run_replay": mcp_server.READ_ONLY,
    "get_sprint": mcp_server.READ_ONLY,
    "get_watch_preference": mcp_server.READ_ONLY,
    "heartbeat_agent_run": mcp_server.ANY_WRITE_SCOPE,
    "label_page": tokens.DOCS_WRITE_SCOPE,
    "link_graph": mcp_server.READ_ONLY,
    "link_issues": tokens.ISSUE_WRITE_SCOPE,
    "link_mention": mcp_server.ANY_WRITE_SCOPE,
    "list_activity_runs": mcp_server.READ_ONLY,
    "list_approvals": tokens.ADMIN_SCOPE,
    "list_automation_failures": mcp_server.READ_ONLY,
    "list_automation_rules": mcp_server.READ_ONLY,
    "list_dispatches": mcp_server.READ_ONLY,
    "list_issue_comments": mcp_server.READ_ONLY,
    "list_issue_links": mcp_server.READ_ONLY,
    "list_issues": mcp_server.READ_ONLY,
    "list_labels": mcp_server.READ_ONLY,
    "list_my_delegated_work": mcp_server.READ_ONLY,
    "list_notifications": mcp_server.READ_ONLY,
    "list_page_versions": mcp_server.READ_ONLY,
    "list_pages": mcp_server.READ_ONLY,
    "list_priority_notifications": mcp_server.READ_ONLY,
    "list_projects": mcp_server.READ_ONLY,
    "list_run_controls": mcp_server.READ_ONLY,
    "list_run_events": mcp_server.READ_ONLY,
    "list_security_events": tokens.ADMIN_SCOPE,
    "list_spaces": mcp_server.READ_ONLY,
    "list_sprints": mcp_server.READ_ONLY,
    "list_subtasks": mcp_server.READ_ONLY,
    "list_users": tokens.ADMIN_SCOPE,
    "list_workers": mcp_server.READ_ONLY,
    "mark_notification_read": mcp_server.ANY_WRITE_SCOPE,
    "mark_notifications_read": mcp_server.ANY_WRITE_SCOPE,
    "my_desk": mcp_server.READ_ONLY,
    "my_office": mcp_server.READ_ONLY,
    "notification_priority_summary": mcp_server.READ_ONLY,
    "offboard_agent": tokens.ADMIN_SCOPE,
    "onboard_agent": tokens.ADMIN_SCOPE,
    "page_backlinks": mcp_server.READ_ONLY,
    "page_outgoing_links": mcp_server.READ_ONLY,
    "pause_agent": tokens.ADMIN_SCOPE,
    "project_timeline": mcp_server.READ_ONLY,
    "query_help": mcp_server.READ_ONLY,
    "read_page_embeds": mcp_server.READ_ONLY,
    "recent_events": mcp_server.READ_ONLY,
    "record_run_learning": tokens.DOCS_WRITE_SCOPE,
    "related_items": mcp_server.READ_ONLY,
    "request_worker_kill": tokens.ADMIN_SCOPE,
    "resolve_embeds": mcp_server.READ_ONLY,
    "restore_page_version": tokens.DOCS_WRITE_SCOPE,
    "resume_agent": tokens.ADMIN_SCOPE,
    "resume_claim_handoff": tokens.ISSUE_WRITE_SCOPE,
    "revoke_agent_tokens": tokens.ADMIN_SCOPE,
    "search": mcp_server.READ_ONLY,
    "search_work": mcp_server.READ_ONLY,
    "search_workspace": mcp_server.READ_ONLY,
    "set_agent_budget": tokens.ADMIN_SCOPE,
    "set_approval_policy": tokens.ADMIN_SCOPE,
    "set_automation_rule_enabled": tokens.ADMIN_SCOPE,
    "set_issue_parent": tokens.ISSUE_WRITE_SCOPE,
    "set_issue_placement": tokens.ISSUE_WRITE_SCOPE,
    "set_issue_sprint": tokens.ISSUE_WRITE_SCOPE,
    "set_watch_preference": mcp_server.ANY_WRITE_SCOPE,
    "start_playbook": tokens.ISSUE_WRITE_SCOPE,
    "start_sprint": tokens.ISSUE_WRITE_SCOPE,
    "unarchive_issue": tokens.ISSUE_WRITE_SCOPE,
    "unarchive_page": tokens.DOCS_WRITE_SCOPE,
    "undo_action": mcp_server.ANY_WRITE_SCOPE,
    "unlabel_page": tokens.DOCS_WRITE_SCOPE,
    "unlink_issues": tokens.ISSUE_WRITE_SCOPE,
    "unlinked_mentions": mcp_server.READ_ONLY,
    "unwatch": mcp_server.ANY_WRITE_SCOPE,
    "update_issue": tokens.ISSUE_WRITE_SCOPE,
    "update_page": tokens.DOCS_WRITE_SCOPE,
    "update_sprint": tokens.ISSUE_WRITE_SCOPE,
    "verify_activity_chain": mcp_server.READ_ONLY,
    "watch": mcp_server.ANY_WRITE_SCOPE,
    "whoami": mcp_server.READ_ONLY,
    "worker_heartbeat": mcp_server.ANY_WRITE_SCOPE,
    "yield_claim": tokens.ISSUE_WRITE_SCOPE,
}


def test_no_tool_is_visible_to_a_token_class_it_was_hidden_from_before():
    # WHY: the map is presentation, but a widened entry would advertise a tool
    # that can only answer 403, which is the defect this change removes. So the
    # change may only narrow: for every token class, visible-now is a subset of
    # visible-then, and the difference is exactly the seven.
    shared = set(_SCOPES_AT_F7308D5) & set(mcp_server.TOOL_SCOPES)
    assert ADMIN_ONLY_READS <= shared
    then_map = {name: _SCOPES_AT_F7308D5[name] for name in shared}
    now_map = {name: mcp_server.TOOL_SCOPES[name] for name in shared}
    classes = (
        frozenset({"read"}),
        frozenset({"read", "issue:write"}),
        frozenset({"read", "docs:write"}),
        SEAT_SCOPES,
    )
    for scopes in classes:
        now, then = _visible(now_map, scopes), _visible(then_map, scopes)
        assert now == now & then, (sorted(scopes), sorted(now - then))
        assert then - now == ADMIN_ONLY_READS, (sorted(scopes), sorted(then - now))
    admin = frozenset({tokens.ADMIN_SCOPE})
    assert _visible(now_map, admin) == _visible(then_map, admin) == shared


# --- the live board: a seat, a second contributor, one issue, one page -----------


@pytest.fixture
def board(tmp_path):
    app = create_app(tmp_path / "slim.db")
    with TestClient(app) as admin:
        admin.post(
            "/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"}
        )
        admin.headers.update(H1)

        def onboard(email, name, scopes):
            response = admin.post(
                "/users/onboard_agent",
                json={"email": email, "name": name, "scopes": sorted(scopes)},
            )
            assert response.status_code == 201, response.text
            return response.json()

        seat = onboard("sol@e.com", "Sol", SEAT_SCOPES)
        other = onboard("ray@e.com", "Ray", {"read", "issue:write"})
        project = admin.post("/projects", json={"name": "Slim", "key": "SLM"})
        assert project.status_code == 201, project.text
        project = project.json()
        issue = admin.post(
            "/issues", json={"title": "trim the surface", "project_id": project["id"]}
        )
        assert issue.status_code == 201, issue.text
        issue = issue.json()
        space = admin.post("/spaces", json={"key": "DOC", "name": "Docs"})
        assert space.status_code == 201, space.text
        space = space.json()
        page = admin.post(
            f"/spaces/{space['id']}/pages", json={"title": "Notes", "body": "hello"}
        )
        assert page.status_code == 201, page.text
        page = page.json()
        for agent in (seat, other):
            delegated = admin.post(
                f"/issues/{issue['id']}/delegate", json={"user_id": agent["user"]["id"]}
            )
            assert delegated.status_code == 201, delegated.text

        def session(onboarded, *, scopes="probe"):
            """An MCP server bound to this agent's bearer token: the surface its
            session really gets (probed), or the full surface for a control."""
            http = TestClient(
                app, headers={"Authorization": f"Bearer {onboarded['token']['token']}"}
            )
            client = AthenaClient(client=http)
            if scopes == "probe":
                scopes = mcp_server.probe_scopes(client)
            return mcp_server.build_server(client, scopes=scopes)

        def http(onboarded):
            """The raw REST client for this agent's token, for the one check
            that must bypass the MCP layer's own argument validation."""
            return TestClient(
                app, headers={"Authorization": f"Bearer {onboarded['token']['token']}"}
            )

        yield {
            "seat": seat,
            "other": other,
            "project": project,
            "issue": issue,
            "space": space,
            "page": page,
            "session": session,
            "http": http,
        }


def _read_arguments(board) -> dict[str, dict]:
    """One argument set per read-scoped tool, enough to reach the route. A row
    that does not exist (a sprint, a run control) answers 404, which proves the
    same thing a 200 does: authentication passed and no admin gate stood in the
    way. What this map must never produce is a validation error, because that
    never reaches the server."""
    issue_id = board["issue"]["id"]
    page_id = board["page"]["id"]
    space_id = board["space"]["id"]
    project_id = board["project"]["id"]
    seat_id = board["seat"]["user"]["id"]
    return {
        "begin_run": {"run_id": "slim-run"},
        "count_work": {"q": "surface"},
        "current_run": {},
        "embed_help": {},
        "find_pages_by_title": {"title": "Notes"},
        "get_agent_budget": {"user_id": seat_id},
        "get_attention_ranking": {},
        "get_fleet_metrics": {},
        "get_issue": {"issue_id": issue_id},
        "get_issue_history": {"issue_id": issue_id},
        "get_issue_lease": {"issue_id": issue_id},
        "get_issue_runbook": {"issue_id": issue_id},
        "get_issue_state": {"issue_id": issue_id},
        "get_issue_work_context": {"issue_id": issue_id},
        "get_page": {"page_id": page_id},
        "get_page_version": {"page_id": page_id, "version": 1},
        "get_project_floor": {"project_id": project_id},
        "get_run_control": {"control_id": 1},
        "get_run_fork_contract": {
            "run_id": "slim-run",
            "fork_from_event_id": 1,
            "fork_run_id": "slim-child",
        },
        "get_run_lineage": {"run_id": "slim-run"},
        "get_run_replay": {"run_id": "slim-run"},
        "get_sprint": {"sprint_id": 1},
        "get_watch_preference": {"target_kind": "issue", "target_id": issue_id},
        "link_graph": {"kind": "issue", "id": issue_id},
        "list_activity_runs": {"actor_id": seat_id},
        "list_dispatches": {},
        "list_issue_comments": {"issue_id": issue_id},
        "list_issue_links": {"issue_id": issue_id},
        "list_issues": {},
        "list_labels": {},
        "list_my_delegated_work": {},
        "list_notifications": {},
        "list_page_versions": {"page_id": page_id},
        "list_pages": {"space_id": space_id},
        "list_priority_notifications": {},
        "list_projects": {},
        "list_run_controls": {},
        "list_run_events": {"run_id": "slim-run"},
        "list_spaces": {},
        "list_sprints": {"project_id": project_id},
        "list_subtasks": {"issue_id": issue_id},
        "list_workers": {},
        "my_desk": {},
        "my_office": {},
        "notification_priority_summary": {},
        "page_backlinks": {"page_id": page_id},
        "page_outgoing_links": {"page_id": page_id},
        "project_timeline": {"project_id": project_id},
        "query_help": {},
        "read_page_embeds": {"page_id": page_id},
        "recent_events": {},
        "related_items": {"kind": "issue", "id": issue_id},
        "resolve_embeds": {"text": "nothing to resolve"},
        "search": {"query": "surface"},
        "search_work": {"q": "surface"},
        "search_workspace": {"query": "surface"},
        "unlinked_mentions": {"kind": "issue", "id": issue_id},
        "whoami": {},
    }


_ADMIN_READ_ARGUMENTS = {
    "activity_chain_status": {},
    "verify_activity_chain": {},
    "get_agent_run_health": {},
    "get_automation_rule": {"rule_id": 1},
    "list_automation_rules": {},
    "list_automation_failures": {},
    "get_fleet_active_work": {},
}


def test_every_read_scoped_tool_answers_a_seat_without_a_403(board):
    # WHY: the check that was missing. The map promises that a registered read
    # can be called; this walks every one with a real non-admin token.
    server = board["session"](board["seat"])
    read_tools = {
        name
        for name, required in mcp_server.TOOL_SCOPES.items()
        if required == mcp_server.READ_ONLY
    }
    arguments = _read_arguments(board)
    assert set(arguments) == read_tools, (
        "every read-scoped tool needs an argument set in this test: "
        f"missing {sorted(read_tools - set(arguments))}, "
        f"stale {sorted(set(arguments) - read_tools)}"
    )
    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    assert read_tools <= registered
    refused: dict[str, str] = {}
    never_sent: dict[str, str] = {}
    for name in sorted(read_tools):
        try:
            asyncio.run(server.call_tool(name, arguments[name]))
        except ToolError as exc:
            error = _athena_error(exc)
            if error is None:
                never_sent[name] = str(exc)[:160]
            elif error["status_code"] == 403:
                refused[name] = error["message"]
    assert never_sent == {}
    assert refused == {}


def test_the_sweep_would_see_a_403_the_seven_refuse_the_same_seat(board):
    # Positive control for the sweep above: the same token, the full surface
    # (what an unprobed or ATHENA_MCP_ALL_TOOLS=1 session carries), and the
    # seven answer 403 through exactly the path the sweep inspects.
    server = board["session"](board["seat"], scopes=None)
    statuses = {}
    for name in sorted(ADMIN_ONLY_READS):
        with pytest.raises(ToolError) as caught:
            asyncio.run(server.call_tool(name, _ADMIN_READ_ARGUMENTS[name]))
        statuses[name] = (_athena_error(caught.value) or {}).get("status_code")
    assert statuses == {name: 403 for name in ADMIN_ONLY_READS}


# --- 3. one argument name for an issue -----------------------------------------


def test_both_issue_reads_take_issue_id_as_well_as_ref(board):
    server = board["session"](board["seat"])
    issue_id = board["issue"]["id"]
    by_id = _call(server, "get_issue", issue_id=issue_id)
    by_ref = _call(server, "get_issue", ref=str(issue_id))
    by_key = _call(server, "get_issue", ref=board["issue"]["key"])
    assert by_id == by_ref == by_key
    assert by_id["id"] == issue_id
    context_by_id = _call(server, "get_issue_work_context", issue_id=issue_id)
    context_by_ref = _call(server, "get_issue_work_context", ref=str(issue_id))
    assert context_by_id["issue"]["id"] == issue_id
    assert context_by_ref["issue"]["id"] == issue_id
    # The fold the instructions state: one issue ETag under two names.
    assert context_by_id["issue_etag"] == by_id["_etag"]
    assert context_by_id["_etag"] != by_id["_etag"]  # the packet's own tag is not it


def test_an_issue_read_needs_exactly_one_of_ref_and_issue_id(board):
    server = board["session"](board["seat"])
    issue_id = board["issue"]["id"]
    for name in ("get_issue", "get_issue_work_context"):
        with pytest.raises(ToolError, match="ref or issue_id"):
            asyncio.run(server.call_tool(name, {}))
        with pytest.raises(ToolError, match="ref or issue_id"):
            asyncio.run(
                server.call_tool(name, {"ref": str(issue_id), "issue_id": issue_id})
            )


def test_the_issue_read_schemas_offer_both_names_and_require_neither():
    tools = _tools(None)
    for name in ("get_issue", "get_issue_work_context"):
        schema = tools[name].inputSchema
        assert set(schema["properties"]) == {"ref", "issue_id"}, name
        assert "ref" not in schema.get("required", []), name
        assert "issue_id" in (tools[name].description or ""), name


# --- 5. a repeated claim says what to do ----------------------------------------


def test_a_repeated_claim_tells_the_holder_it_is_a_renewal_and_names_the_generation(
    board,
):
    server = board["session"](board["seat"])
    issue_id = board["issue"]["id"]
    etag = _call(server, "get_issue", issue_id=issue_id)["_etag"]
    lease = _call(server, "claim_issue", issue_id=issue_id, if_match=etag)
    generation = lease["generation"]
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool("claim_issue", {"issue_id": issue_id, "if_match": etag})
        )
    error = _athena_error(caught.value)
    assert error is not None
    assert error["status_code"] == 428  # unchanged
    assert error["code"] == "lease_generation_required"  # unchanged
    said = json.dumps(error)
    assert "already hold" in said
    assert "renew" in said
    assert f"generation={generation}" in said
    # What it tells you to do works.
    renewed = _call(
        server, "claim_issue", issue_id=issue_id, if_match=etag, generation=generation
    )
    assert renewed["holder_id"] == board["seat"]["user"]["id"]
    assert renewed["active"] is True


def test_the_generation_is_named_to_the_holder_only(board):
    # Positive control for "to the holder only": a second eligible contributor
    # who repeats the claim is refused, and the refusal carries no generation.
    holder = board["session"](board["seat"])
    rival = board["session"](board["other"])
    issue_id = board["issue"]["id"]
    etag = _call(holder, "get_issue", issue_id=issue_id)["_etag"]
    generation = _call(holder, "claim_issue", issue_id=issue_id, if_match=etag)[
        "generation"
    ]
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            rival.call_tool("claim_issue", {"issue_id": issue_id, "if_match": etag})
        )
    error = _athena_error(caught.value)
    assert error is not None
    assert error["status_code"] == 409
    assert generation not in json.dumps(error)
    assert "generation=" not in json.dumps(error)


def test_other_generation_required_refusals_keep_their_general_wording(board):
    # Only the repeated claim learned to explain itself. A completion without a
    # generation is still the generic 428: the caller has not said which row it
    # means, so there is nothing to name. The MCP tool makes generation a required
    # argument, so this goes through REST, where the field is optional precisely
    # so the command can answer 428 instead of a validation error.
    server = board["session"](board["seat"])
    issue_id = board["issue"]["id"]
    etag = _call(server, "get_issue", issue_id=issue_id)["_etag"]
    _call(server, "claim_issue", issue_id=issue_id, if_match=etag)
    response = board["http"](board["seat"]).post(
        f"/issues/{issue_id}/complete", json={}
    )
    assert response.status_code == 428, response.text
    assert "generation=" not in response.text
    assert "already hold" not in response.text


# --- 4. the docstrings an agent reads first --------------------------------------


def test_whoami_no_longer_tells_every_agent_to_call_it_first():
    description = _tools(None)["whoami"].description or ""
    assert "Call this FIRST" not in description
    assert "my_desk" in description
    assert "run" in description


def test_complete_claim_leads_with_what_it_does_not_do():
    description = _tools(None)["complete_claim"].description or ""
    first_line = description.strip().splitlines()[0]
    assert "NOT" in first_line
    assert "update_issue" in first_line
    assert "docs/CLOSEOUT_RECIPE.md" in description
    # The report-format prose lives in the doc now.
    assert "Refs are inert data" not in description
    assert "Refs are inert data" in (DOCS / "CLOSEOUT_RECIPE.md").read_text()
    # What the registered tool must still define where the agent reads it
    # (tests/test_completion_reports.py pins the same two sentences).
    assert "not_assessed means the condition was not evaluated" in description


def test_claim_issue_explains_renewal_and_points_at_the_roots_doc():
    description = _tools(None)["claim_issue"].description or ""
    assert "renew" in description.lower()
    assert "generation" in description
    assert "same value" in description  # one ETag, two names
    assert "docs/ACTIVE_WORK.md" in description
    # The paths/coordination_root paragraph moved; the doc explains renewal.
    assert "Unknown nonempty origins" not in description
    roots = (DOCS / "ACTIVE_WORK.md").read_text()
    assert "Unknown nonempty origins" in roots
    assert "new possession" in roots
    assert "retains" in roots and "issue-only" in roots


def test_update_issue_says_if_match_is_optional():
    description = _tools(None)["update_issue"].description or ""
    assert "if_match" in description
    assert "optional" in description.lower()


def test_decline_delegation_says_an_empty_list_is_success():
    description = _tools(None)["decline_delegation"].description or ""
    assert "empty list" in description.lower()
    assert "success" in description.lower()
