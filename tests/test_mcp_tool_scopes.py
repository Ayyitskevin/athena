"""Scope-filtered MCP tool registration.

An agent session used to carry all ~123 tools regardless of what its token could
do — a read-scoped agent hauled ~10k tokens of mutation docstrings whose only
possible answer was 403. ``build_server`` now registers only the tools the
session's token can use, driven by ``TOOL_SCOPES``: the whole policy on one
screen, audited against the REST layer's route dependencies.

Two properties these tests defend, and the order matters:

  * **This is presentation, not authorization.** The REST layer remains the
    boundary. A wrong map entry can show or hide a tool; it can never permit a
    call the server would refuse — which is why the probe FAILS OPEN: an
    unreachable Athena at MCP startup must not change what exists, only who
    answers 403 later.
  * **The map cannot fall behind the tool list.** Registering a tool with no
    entry raises at build time, so every server-building test in the suite is
    also a completeness check.
"""

import asyncio

from fastapi.testclient import TestClient
import pytest

from athena.core import tokens
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient


class _NoWhoamiStub:
    """A client with no reachable server — the probe must fail open."""


def _names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


def _build(scopes) -> set[str]:
    return _names(mcp_server.build_server(_NoWhoamiStub(), scopes=scopes))


# --- the map itself ----------------------------------------------------------


def test_every_registered_tool_is_mapped_and_nothing_stale_remains():
    # WHY: the completeness contract, asserted both directions. An unmapped tool
    # raises at build (tested below); a stale entry would silently rot.
    registered = _build(None)
    assert registered == set(mcp_server.TOOL_SCOPES)


def test_an_unmapped_tool_refuses_to_build(monkeypatch):
    trimmed = dict(mcp_server.TOOL_SCOPES)
    trimmed.pop("my_desk")
    monkeypatch.setattr(mcp_server, "TOOL_SCOPES", trimmed)
    with pytest.raises(RuntimeError) as caught:
        mcp_server.build_server(_NoWhoamiStub(), scopes=None)
    assert "my_desk" in str(caught.value)
    assert "TOOL_SCOPES" in str(caught.value)


def test_scope_vocabulary_is_closed():
    allowed = {
        mcp_server.READ_ONLY,
        mcp_server.ANY_WRITE_SCOPE,
        tokens.ISSUE_WRITE_SCOPE,
        tokens.DOCS_WRITE_SCOPE,
        tokens.ADMIN_SCOPE,
    }
    assert set(mcp_server.TOOL_SCOPES.values()) <= allowed


# --- what each token class sees ----------------------------------------------


def test_a_read_token_sees_reads_and_no_mutations():
    surface = _build(frozenset({"read"}))
    assert {
        "my_desk",
        "search_workspace",
        "get_issue",
        "whoami",
        "list_priority_notifications",
        "notification_priority_summary",
        "get_watch_preference",
    } <= surface
    # Not one mutation — module writes, personal state, or admin.
    assert not surface & {
        "create_issue",
        "create_page",
        "watch",
        "advance_desk_cursor",
        "start_playbook",
        "onboard_agent",
        "undo_action",
        "set_watch_preference",
        "clear_watch_preference",
    }


def test_an_issue_writer_gets_aegis_and_personal_but_not_mentor_or_admin():
    surface = _build(frozenset({"read", "issue:write"}))
    assert {
        "create_issue",
        "claim_issue",
        "start_playbook",
        "watch",
        "set_watch_preference",
        "clear_watch_preference",
    } <= surface
    assert not surface & {"create_page", "record_run_learning", "onboard_agent"}


def test_a_docs_writer_gets_mentor_and_personal_but_not_aegis_or_admin():
    surface = _build(frozenset({"read", "docs:write"}))
    assert {"create_page", "record_run_learning", "watch", "undo_action"} <= surface
    assert not surface & {"create_issue", "start_playbook", "set_agent_budget"}


def test_admin_implies_everything():
    # WHY: mirrors identity.token_has_scope exactly — one implication rule, not two.
    assert _build(frozenset({"admin"})) == _build(None)


def test_admin_gated_reads_are_hidden_from_non_admin_tokens():
    # WHY: a tool that can only answer 403 is not a capability, it is noise.
    surface = _build(frozenset({"read", "issue:write", "docs:write"}))
    assert not surface & {
        "list_users",
        "list_approvals",
        "agent_answerability",
        "list_security_events",
    }


# --- the probe ---------------------------------------------------------------


def test_probe_failure_fails_open_to_the_full_surface(capsys):
    # WHY: filtering is ergonomics; the REST layer is the boundary. Athena being
    # down at MCP startup must not change which tools exist.
    scopes = mcp_server.probe_scopes(_NoWhoamiStub())
    assert scopes is None  # None = unlimited = the full surface
    assert "presenting the full tool surface" in capsys.readouterr().err
    assert _names(mcp_server.build_server(_NoWhoamiStub(), scopes=scopes)) == set(
        mcp_server.TOOL_SCOPES
    )


def test_escape_hatch_env_skips_the_probe_entirely(monkeypatch):
    monkeypatch.setenv("ATHENA_MCP_ALL_TOOLS", "1")

    class ExplodingClient:
        def whoami(self):  # pragma: no cover — the hatch must short-circuit
            raise AssertionError("escape hatch must not probe")

    assert mcp_server.probe_scopes(ExplodingClient()) is None


def test_the_entry_point_wires_the_probe():
    # WHY: build_server deliberately does NOT probe (a hidden whoami would
    # prepend a surprise call to every harness), so the wiring in main() is the
    # only thing standing between the feature and it silently never running.
    import inspect

    source = inspect.getsource(mcp_server.main)
    assert "probe_scopes(client)" in source


def test_probe_reads_real_scopes_over_real_http(tmp_path):
    # WHY: the composed path — whoami over the wire deciding the surface. A
    # read-scoped token's session carries no mutations; the same instance's
    # admin token carries everything.
    with TestClient(create_app(tmp_path / "probe.db")) as tc:
        tc.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        # Mint both BEFORE attaching either bearer: once a client carries an
        # Authorization header it outranks the actor header, and a read token
        # cannot mint its own successor.
        read_raw = tc.post(
            "/tokens",
            json={"name": "reader", "scopes": ["read"]},
            headers={"X-Athena-Actor": "1"},
        ).json()["token"]
        admin_raw = tc.post(
            "/tokens",
            json={"name": "root", "scopes": ["admin"]},
            headers={"X-Athena-Actor": "1"},
        ).json()["token"]

        tc.headers.update({"Authorization": f"Bearer {read_raw}"})
        reader = AthenaClient(client=tc)
        reader_surface = _names(
            mcp_server.build_server(reader, scopes=mcp_server.probe_scopes(reader))
        )
        assert "my_desk" in reader_surface
        assert "create_issue" not in reader_surface

        tc.headers.update({"Authorization": f"Bearer {admin_raw}"})
        root = AthenaClient(client=tc)
        admin_surface = _names(
            mcp_server.build_server(root, scopes=mcp_server.probe_scopes(root))
        )
        assert admin_surface == set(mcp_server.TOOL_SCOPES)


def test_non_token_auth_sees_everything(tmp_path):
    # WHY: whoami returns scopes=null for auth that is not scope-limited (the
    # trusted actor header, a browser session) — null means unlimited, not empty.
    with TestClient(create_app(tmp_path / "unltd.db")) as tc:
        tc.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        tc.headers.update({"X-Athena-Actor": "1"})
        header_auth = AthenaClient(client=tc)
        surface = _names(
            mcp_server.build_server(
                header_auth, scopes=mcp_server.probe_scopes(header_auth)
            )
        )
        assert surface == set(mcp_server.TOOL_SCOPES)
