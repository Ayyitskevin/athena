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
import socket

from fastapi.testclient import TestClient
import httpx
import pytest

from athena.core import tokens
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient, AthenaError


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


# --- a rejected token stops the server; an unreachable Athena does not ----------


class _RefusingClient:
    """Athena reached and answering the whoami with a refusal status."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail

    def whoami(self):
        raise AthenaError(
            method="GET", path="/users/me", status_code=self.status, detail=self.detail
        )


@pytest.mark.parametrize(
    ("status", "detail"),
    [(401, "invalid or revoked token"), (403, "account is paused")],
)
def test_a_token_athena_refuses_is_not_presented_as_a_working_session(
    status, detail, capsys
):
    # WHY: a 401/403 is Athena ANSWERING that this credential cannot act. Every
    # tool would then fail the same way, and the old stderr warning was invisible
    # to the model, which saw 140 tools and a wall of 401s.
    with pytest.raises(mcp_server.TokenRejected) as rejected:
        mcp_server.probe_scopes(_RefusingClient(status, detail))
    message = str(rejected.value)
    assert "rejected ATHENA_TOKEN" in message
    assert str(status) in message and detail in message
    assert "ATHENA_BASE_URL" in message and "revoked" in message
    assert "presenting the full tool surface" not in capsys.readouterr().err


def test_a_server_error_still_fails_open(capsys):
    # WHY: a 5xx says nothing about the token; the REST layer is the boundary.
    def respond(request):
        return httpx.Response(503, json={"detail": "down for maintenance"})

    with httpx.Client(
        base_url="http://athena.test", transport=httpx.MockTransport(respond)
    ) as http:
        assert mcp_server.probe_scopes(AthenaClient(client=http)) is None
    assert "presenting the full tool surface" in capsys.readouterr().err


def test_an_unreachable_athena_still_fails_open_to_the_full_surface(capsys):
    # A port nothing listens on: connection refused, the case that must not
    # change which tools exist.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = AthenaClient(base_url=f"http://127.0.0.1:{port}", token="x", timeout=2)
    scopes = mcp_server.probe_scopes(client)
    assert scopes is None
    assert "presenting the full tool surface" in capsys.readouterr().err
    assert _names(mcp_server.build_server(client, scopes=scopes)) == set(
        mcp_server.TOOL_SCOPES
    )


def test_the_escape_hatch_still_skips_a_probe_that_would_be_refused(monkeypatch):
    monkeypatch.setenv("ATHENA_MCP_ALL_TOOLS", "1")
    assert mcp_server.probe_scopes(_RefusingClient(401, "invalid")) is None


def _run_entry_point(monkeypatch, tmp_path, *, valid: bool):
    """Run main() against a real app over its TestClient, recording what it
    would build instead of serving stdio."""
    built: list[frozenset[str] | None] = []

    class _Recorder:
        def run(self) -> None:
            pass

    def record_build(client, *, scopes=None, profile=None):
        built.append(scopes)
        return _Recorder()

    with TestClient(create_app(tmp_path / "entry.db")) as tc:
        tc.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        raw = tc.post(
            "/tokens",
            json={"name": "seat", "scopes": ["read", "issue:write"]},
            headers={"X-Athena-Actor": "1"},
        ).json()["token"]
        bearer = raw if valid else "ath_mistyped"
        tc.headers.update({"Authorization": f"Bearer {bearer}"})
        monkeypatch.setenv("ATHENA_TOKEN", bearer)
        monkeypatch.setenv("ATHENA_BASE_URL", "http://athena.test")
        monkeypatch.delenv("ATHENA_MCP_ALL_TOOLS", raising=False)
        monkeypatch.delenv("ATHENA_MCP_PROFILE", raising=False)
        monkeypatch.setattr(
            mcp_server,
            "AthenaClient",
            lambda **kwargs: AthenaClient(client=tc, run_id=kwargs["run_id"]),
        )
        monkeypatch.setattr(mcp_server, "build_server", record_build)
        mcp_server.main()
    return built


def test_the_entry_point_exits_on_a_rejected_token_and_registers_nothing(
    monkeypatch, tmp_path, capsys
):
    with pytest.raises(SystemExit) as stop:
        _run_entry_point(monkeypatch, tmp_path, valid=False)
    assert stop.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("athena-mcp: Athena rejected ATHENA_TOKEN at startup (401")
    assert "ATHENA_BASE_URL=http://athena.test" in err
    assert "ath_mistyped" not in err  # the credential is never echoed
    assert "presenting the full tool surface" not in err


def test_the_entry_point_with_a_valid_token_is_unchanged(monkeypatch, tmp_path, capsys):
    # The positive control: the same path with the real token builds once, with
    # the token's scopes, and says nothing on stderr.
    built = _run_entry_point(monkeypatch, tmp_path, valid=True)
    assert built == [frozenset({"read", "issue:write"})]
    assert capsys.readouterr().err == ""
