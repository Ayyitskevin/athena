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

# The audit's core 24: the tools its real run of the loop needed.
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
    }
)
# Named in the seat profile ahead of their branches merging; until then they
# are not registered and the intersection ignores them.
PENDING = frozenset({"claim_next_ready", "wait_for_events"})
# The audit's eleven for knowledge work.
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
    assert mcp_server.PROFILES["seat"] == SEAT | PENDING
    registered = set(mcp_server.TOOL_SCOPES)
    assert SEAT <= registered
    # Both additions are still pending here; when their branches merge this
    # line is what changes, and the surface below grows by exactly those names.
    assert mcp_server.PROFILES["seat"] - registered <= PENDING
    assert _names(SEAT_SCOPES, mcp_server.PROFILES["seat"]) == SEAT | (
        PENDING & registered
    )


def test_the_knowledge_profile_adds_exactly_the_audits_eleven():
    assert mcp_server.PROFILES["seat+knowledge"] == SEAT | PENDING | KNOWLEDGE
    registered = set(mcp_server.TOOL_SCOPES)
    assert KNOWLEDGE <= registered
    assert _names(SEAT_SCOPES, mcp_server.PROFILES["seat+knowledge"]) == (
        SEAT | KNOWLEDGE | (PENDING & registered)
    )


def test_the_three_profiles_are_the_whole_vocabulary():
    assert set(mcp_server.PROFILES) == {"seat", "seat+knowledge", "full"}
    assert mcp_server.PROFILES["full"] is None


@pytest.mark.parametrize(
    "profile_name,scopes,expected",
    [
        ("seat", SEAT_SCOPES, 24),
        ("seat+knowledge", SEAT_SCOPES, 35),
        ("seat", ADMIN, 24),
        ("seat+knowledge", ADMIN, 35),
        ("seat", ISSUE_ONLY, 24),
        # the eleven minus the three docs:write tools
        ("seat+knowledge", ISSUE_ONLY, 32),
        # the ten reads among the core: my_desk, list_my_delegated_work,
        # search_work, get_issue, get_issue_work_context, list_issue_comments,
        # get_issue_lease, current_run, list_notifications, list_run_controls
        ("seat", READ_TOKEN, 10),
    ],
)
def test_exact_counts_per_profile_and_token_class(profile_name, scopes, expected):
    registered_pending = PENDING & set(mcp_server.TOOL_SCOPES)
    assert registered_pending == frozenset(), (
        "a pending addition merged: recount this table once it is registered"
    )
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
    for profile_name in ("seat", "seat+knowledge"):
        profile = mcp_server.PROFILES[profile_name]
        for scopes in (SEAT_SCOPES, ADMIN, None):
            assert "mark_notifications_read" not in _names(scopes, profile)
        assert "mark_notification_read" in _names(SEAT_SCOPES, profile)
    assert "mark_notifications_read" in _names(SEAT_SCOPES, mcp_server.PROFILES["full"])


# --- the environment knob -----------------------------------------------------------


def test_profile_from_env_names_the_three_and_defaults_to_full():
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


def test_an_unknown_profile_is_refused_not_silently_widened():
    # WHY: a typo must not hand a seat the full surface by accident.
    with pytest.raises(ValueError) as refused:
        mcp_server.profile_from_env({"ATHENA_MCP_PROFILE": "lite"})
    message = str(refused.value)
    assert "ATHENA_MCP_PROFILE" in message
    for name in ("full", "seat", "seat+knowledge"):
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
