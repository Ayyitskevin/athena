"""A tool profile carries fewer tools, never more.

WHY: after part A a normal seat's session still carries 108 tools (an admin's
134), and the audit's run of the whole loop used about two dozen. Every surplus
tool is context the agent reads before each turn. `ATHENA_MCP_PROFILE` names a
smaller set; the profile is intersected with the scope filter at registration,
so it can only hide a tool the token could use, never show one it could not.
`full` (the default) is today's behaviour, and a positive control pins that it
equals the scope filter alone for every token class.

K2 (never bulk-acknowledge notifications) is why `mark_notifications_read` is in
no seat profile: a seat under a profile cannot even see the bulk tool.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from athena.core import tokens
from athena.mcp import server as mcp_server

# The audit's core loop, including the two additions now merged on main.
SEAT = frozenset(
    {
        "my_desk",
        "advance_desk_cursor",
        "list_my_delegated_work",
        "search_work",
        "get_issue",
        "get_issue_work_context",
        "list_issue_comments",
        "get_issue_lease",
        "claim_issue",
        "yield_claim",
        "resume_claim_handoff",
        "complete_claim",
        "decline_delegation",
        "update_issue",
        "comment_on_issue",
        "create_issue",
        "heartbeat_agent_run",
        "current_run",
        "list_notifications",
        "mark_notification_read",
        "list_run_controls",
        "acknowledge_run_control",
        "complete_run_control",
        "decline_run_control",
        "claim_next_ready",
        "wait_for_events",
        # A11: a seat makes keyed writes, so it is the seat that has to be
        # able to ask what became of one.
        "get_idempotency_key",
        # H6: a comment the list cuts is read whole by its id, so the seat that
        # reads the thread carries the read.
        "get_issue_comment",
        # AD10 (D32): a seat continues the run my_desk names under
        # identity.last_run.
        "begin_run",
    }
)
# The audit's eleven for knowledge work, and the notebook tool (G8).
KNOWLEDGE = frozenset(
    {
        "search",
        "get_page",
        "list_pages",
        "create_page",
        "update_page",
        "record_run_learning",
        "get_issue_runbook",
        "list_projects",
        "attach_label",
        "list_labels",
        "link_issues",
        # G8: the desk tells a seat how to open its notebook, so the profile
        # that edits pages carries the tool that makes one.
        "open_my_notebook",
        # AD11 (D33): list_pages takes a space_id, so the profile lists spaces.
        "list_spaces",
    }
)
CONDUCTOR_ADDITIONS = frozenset(
    {
        "get_attention_ranking",
        "get_project_floor",
        "get_project_brief",
        "get_project_review_queue",
        "get_issue_history",
        "assign_issue",
        "request_candidate_review",
        "delegate_issue",
        "set_issue_parent",
        "list_subtasks",
        "link_issues",
        # F9: the accounts assignment and delegation take on one issue.
        "list_eligible_accounts",
        # F10: the text one edit replaced, which the history points at.
        "get_issue_prior_text",
        # Decision 8: hand a slice to a declared seat by slug, and radio it.
        "assign_issue_to_seat",
        # AD11 (D33): a conductor places slices in projects and learns the
        # seat slugs assign_issue_to_seat takes.
        "list_projects",
        "list_fleet_seats",
    }
)

READ_TOKEN = frozenset({"read"})
ISSUE_ONLY = frozenset({"read", "issue:write"})
DOCS_ONLY = frozenset({"read", "docs:write"})
SEAT_SCOPES = frozenset({"read", "issue:write", "docs:write"})
ADMIN = frozenset({tokens.ADMIN_SCOPE})
CLASSES = (None, READ_TOKEN, ISSUE_ONLY, DOCS_ONLY, SEAT_SCOPES, ADMIN)


class _NoServer:
    """No reachable Athena: registration must not need one."""


def _names(scopes, profile) -> set[str]:
    server = mcp_server.build_server(_NoServer(), scopes=scopes, profile=profile)
    return {tool.name for tool in asyncio.run(server.list_tools())}


# --- each profile's exact tool set ---------------------------------------------


def test_the_seat_profile_is_the_audits_core_set_plus_the_two_named_additions():
    assert mcp_server.PROFILES["seat"] == SEAT
    registered = set(mcp_server.TOOL_SCOPES)
    assert SEAT <= registered
    assert _names(SEAT_SCOPES, mcp_server.PROFILES["seat"]) == SEAT


def test_the_knowledge_profile_adds_exactly_the_audits_eleven_and_the_notebook():
    assert mcp_server.PROFILES["seat+knowledge"] == SEAT | KNOWLEDGE
    registered = set(mcp_server.TOOL_SCOPES)
    assert KNOWLEDGE <= registered
    assert _names(SEAT_SCOPES, mcp_server.PROFILES["seat+knowledge"]) == (
        SEAT | KNOWLEDGE
    )


def test_the_conductor_adds_exactly_the_coordination_tools():
    assert SEAT.isdisjoint(CONDUCTOR_ADDITIONS)
    assert mcp_server.PROFILES["conductor"] == SEAT | CONDUCTOR_ADDITIONS
    assert _names(SEAT_SCOPES, mcp_server.PROFILES["conductor"]) == (
        SEAT | CONDUCTOR_ADDITIONS
    )


def test_the_four_profiles_are_the_whole_vocabulary():
    assert set(mcp_server.PROFILES) == {"seat", "seat+knowledge", "conductor", "full"}
    assert mcp_server.PROFILES["full"] is None


@pytest.mark.parametrize(
    "profile_name,scopes,expected",
    [
        # H6 added get_issue_comment, a read in `seat`: every count rose by one.
        # AD10 added begin_run to `seat`, and AD11 list_spaces to
        # `seat+knowledge` and list_projects and list_fleet_seats to
        # `conductor`, all three reads.
        ("seat", SEAT_SCOPES, 29),
        ("seat+knowledge", SEAT_SCOPES, 42),
        ("seat", ADMIN, 29),
        ("seat+knowledge", ADMIN, 42),
        ("seat", ISSUE_ONLY, 29),
        # the thirteen minus the four docs:write tools
        ("seat+knowledge", ISSUE_ONLY, 38),
        # Decision 8 added assign_issue_to_seat, an issue:write conductor tool.
        ("conductor", SEAT_SCOPES, 45),
        ("conductor", ISSUE_ONLY, 45),
        ("conductor", ADMIN, 45),
        ("conductor", READ_TOKEN, 24),
        # docs:write permits six inherited any-write tools (cursor, heartbeat,
        # targeted notification ack and own controls), but no issue writes.
        ("conductor", DOCS_ONLY, 30),
        # the thirteen reads among the core: my_desk, list_my_delegated_work,
        # search_work, get_issue, get_issue_work_context, list_issue_comments,
        # get_issue_comment (H6),
        # get_issue_lease, current_run, list_notifications, list_run_controls,
        # wait_for_events, get_idempotency_key, and begin_run (AD10)
        ("seat", READ_TOKEN, 14),
    ],
)
def test_exact_counts_per_profile_and_token_class(profile_name, scopes, expected):
    assert len(_names(scopes, mcp_server.PROFILES[profile_name])) == expected


# --- a profile only hides ---------------------------------------------------------


def test_full_is_todays_surface_for_every_token_class():
    # The positive control: the default profile changes nothing, so every
    # existing scope test keeps meaning what it meant.
    for scopes in CLASSES:
        assert _names(scopes, mcp_server.PROFILES["full"]) == _names(scopes, None)
    assert _names(None, None) == set(mcp_server.TOOL_SCOPES)


def test_a_profile_never_exposes_a_tool_the_scope_filter_hides():
    for profile_name, profile in mcp_server.PROFILES.items():
        for scopes in CLASSES:
            under_profile = _names(scopes, profile)
            scope_only = _names(scopes, None)
            assert under_profile <= scope_only, (profile_name, sorted(scopes or []))
    # The sharp case: the seat profile lists claim_issue, and a read token still
    # does not get it; the same token class with issue:write does.
    assert "claim_issue" not in _names(READ_TOKEN, mcp_server.PROFILES["seat"])
    assert "claim_issue" in _names(ISSUE_ONLY, mcp_server.PROFILES["seat"])


def test_bulk_acknowledgement_is_in_no_seat_profile():
    # WHY: K2. A seat under a profile cannot see the bulk tool at all; the
    # targeted one stays. Under `full` nothing changes, which is the control.
    for profile_name in ("seat", "seat+knowledge", "conductor"):
        profile = mcp_server.PROFILES[profile_name]
        for scopes in (SEAT_SCOPES, ADMIN, None):
            assert "mark_notifications_read" not in _names(scopes, profile)
        assert "mark_notification_read" in _names(SEAT_SCOPES, profile)
    assert "mark_notifications_read" in _names(SEAT_SCOPES, mcp_server.PROFILES["full"])


def test_conductor_assignment_and_delegation_still_require_issue_write():
    profile = mcp_server.PROFILES["conductor"]
    writes = {
        "assign_issue",
        "assign_issue_to_seat",
        "delegate_issue",
        "set_issue_parent",
        "link_issues",
        "request_candidate_review",
    }
    for scopes in (READ_TOKEN, DOCS_ONLY):
        assert writes.isdisjoint(_names(scopes, profile))
    assert writes <= _names(ISSUE_ONLY, profile)
    assert _names(READ_TOKEN, profile) == {
        "get_idempotency_key",
        "my_desk",
        "list_my_delegated_work",
        "search_work",
        "get_issue",
        "get_issue_work_context",
        "list_issue_comments",
        "get_issue_comment",
        "get_issue_lease",
        "current_run",
        "list_notifications",
        "list_run_controls",
        "wait_for_events",
        "get_attention_ranking",
        "get_project_floor",
        "get_project_brief",
        "get_project_review_queue",
        "get_issue_history",
        "get_issue_prior_text",
        "list_subtasks",
        "list_eligible_accounts",
        "begin_run",
        "list_projects",
        # A read the route answers only for issue:write (AD11); listing it to a
        # read token costs a refusal, not a disclosure.
        "list_fleet_seats",
    }
    server = mcp_server.build_server(_NoServer(), scopes=READ_TOKEN, profile=profile)
    for tool in asyncio.run(server.list_tools()):
        assert tool.annotations is None or tool.annotations.readOnlyHint is not False


def test_conductor_never_carries_admin_tools_even_with_admin_credentials():
    admin_tools = {
        name
        for name, scope in mcp_server.TOOL_SCOPES.items()
        if scope == tokens.ADMIN_SCOPE
    }
    assert admin_tools  # This control must exercise a nonempty exclusion set.
    for scopes in (SEAT_SCOPES, ADMIN, None):
        assert admin_tools.isdisjoint(_names(scopes, mcp_server.PROFILES["conductor"]))


def test_the_onboarding_scope_table_is_what_registration_does():
    """Onboarding refuses to print a profile the token's scopes cannot fully use
    (F9). Its table (athena.mcp.config.PROFILE_SCOPES) must say exactly what the
    server does: for every token class, the table finds nothing missing iff
    every tool of the profile registers under those scopes."""
    from itertools import combinations

    from athena.mcp import config as mcp_config

    assert set(mcp_config.PRINTABLE_PROFILES) == set(mcp_server.PROFILES) - {"full"}
    vocabulary = ("read", "issue:write", "docs:write", tokens.ADMIN_SCOPE)
    classes = [
        frozenset(chosen)
        for size in range(len(vocabulary) + 1)
        for chosen in combinations(vocabulary, size)
    ]
    for profile_name in mcp_config.PRINTABLE_PROFILES:
        profile = mcp_server.PROFILES[profile_name]
        for scopes in classes:
            whole = _names(scopes, profile) == profile
            covered = mcp_config.scopes_missing_for(profile_name, scopes) == []
            assert whole == covered, (profile_name, sorted(scopes))


# --- the environment knob -----------------------------------------------------------


def test_profile_from_env_names_the_four_and_defaults_to_full():
    assert mcp_server.profile_from_env({}) is None
    assert mcp_server.profile_from_env({"ATHENA_MCP_PROFILE": ""}) is None
    assert mcp_server.profile_from_env({"ATHENA_MCP_PROFILE": "full"}) is None
    assert (
        mcp_server.profile_from_env({"ATHENA_MCP_PROFILE": "seat"})
        == mcp_server.PROFILES["seat"]
    )
    assert (
        mcp_server.profile_from_env({"ATHENA_MCP_PROFILE": "seat+knowledge"})
        == mcp_server.PROFILES["seat+knowledge"]
    )
    assert (
        mcp_server.profile_from_env({"ATHENA_MCP_PROFILE": " conductor "})
        == mcp_server.PROFILES["conductor"]
    )


def test_an_unknown_profile_is_refused_not_silently_widened():
    # WHY: a typo must not hand a seat the full surface by accident.
    with pytest.raises(ValueError) as refused:
        mcp_server.profile_from_env({"ATHENA_MCP_PROFILE": "lite"})
    message = str(refused.value)
    assert "ATHENA_MCP_PROFILE" in message
    for name in ("full", "seat", "seat+knowledge", "conductor"):
        assert name in message


def test_the_entry_point_reads_the_profile_before_it_probes(monkeypatch, capsys):
    source = inspect.getsource(mcp_server.main)
    assert "profile_from_env(" in source
    assert "profile=" in source
    # And an unknown value stops the entry point loudly, before any client is
    # built or any probe is made.
    monkeypatch.setenv("ATHENA_MCP_PROFILE", "lite")
    with pytest.raises(SystemExit) as stop:
        mcp_server.main()
    assert stop.value.code == 2
    assert "ATHENA_MCP_PROFILE" in capsys.readouterr().err
