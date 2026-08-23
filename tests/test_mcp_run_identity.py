"""MCP writes carry run identity — the transport agents actually use is attributed.

Athena's run machinery (lineage, replay, forking, Mission Control) keys off the
X-Athena-Run header family, but the MCP transport never sent them: every write an
agent made over MCP fell into the untagged time-gap heuristic, and the fork
contract returned headers no MCP tool could transmit. These tests pin the fix:

  * run identity is AthenaClient state — set at construction or via set_run —
    and every subsequent write lands in the activity trail with that run id;
  * set_run replaces the WHOLE identity (a new run never inherits the previous
    run's parent or fork point) and set_run(None) returns to untagged;
  * a fork contract's values fed to set_run actually continue the fork
    (parent + forked_from_event stamped on subsequent events);
  * the MCP server exposes begin_run/current_run, and its entrypoint defaults
    to a fresh mcp-<hex> run id so sessions are attributed BY DEFAULT.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app
from athena.mcp.client import AthenaClient


def _harness(tmp_path, name="run_identity.db", **client_kwargs):
    """A TestClient-backed AthenaClient authenticated as the bootstrap admin,
    plus the db path for direct trail inspection."""
    db_file = tmp_path / name
    tc = TestClient(create_app(db_file))
    tc.__enter__()
    tc.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    raw = tc.post(
        "/tokens",
        json={"name": "agent", "scopes": ["admin"]},
        headers={"X-Athena-Actor": "1"},
    ).json()["token"]
    tc.headers.update({"Authorization": f"Bearer {raw}"})
    return tc, AthenaClient(client=tc, **client_kwargs), db_file


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=100)
    conn.close()
    return rows


def test_constructor_run_id_tags_every_write(tmp_path):
    tc, ath, db_file = _harness(tmp_path, run_id="mcp-boot-1")
    try:
        issue = ath.create_issue(title="tagged from birth")
    finally:
        tc.__exit__(None, None, None)
    ev = [e for e in _events(db_file, "created") if e["target_id"] == issue["id"]]
    assert len(ev) == 1
    assert ev[0]["run_id"] == "mcp-boot-1"
    assert ev[0]["parent_run_id"] is None


def test_set_run_switches_and_never_leaks_previous_context(tmp_path):
    tc, ath, db_file = _harness(tmp_path)
    try:
        # The parent must be a run that was actually written — record() drops a
        # parent naming a run nobody ran, so fabricated ancestry stays out of the
        # lineage tree.
        ath.set_run("run-a")
        ath.create_issue(title="under run-a")
        # First run carries a parent; the event records both.
        ath.set_run("run-b", parent_run_id="run-a")
        first = ath.create_issue(title="under run-b")
        # Switching runs must CLEAR the old parent, not inherit it.
        ath.set_run("run-c")
        second = ath.create_issue(title="under run-c")
        # Clearing the identity returns to untagged writes.
        ath.set_run(None)
        third = ath.create_issue(title="untagged")
    finally:
        tc.__exit__(None, None, None)

    by_target = {e["target_id"]: e for e in _events(db_file, "created")}
    assert by_target[first["id"]]["run_id"] == "run-b"
    assert by_target[first["id"]]["parent_run_id"] == "run-a"
    assert by_target[second["id"]]["run_id"] == "run-c"
    assert by_target[second["id"]]["parent_run_id"] is None
    assert by_target[third["id"]]["run_id"] is None


def test_fork_contract_values_fed_to_set_run_continue_the_fork(tmp_path):
    tc, ath, db_file = _harness(tmp_path)
    try:
        ath.set_run("parent-run")
        anchor = ath.create_issue(title="fork anchor")
        anchor_event = [
            e for e in _events(db_file, "created") if e["target_id"] == anchor["id"]
        ][0]

        contract = ath.get_run_fork_contract(
            "parent-run",
            fork_from_event_id=anchor_event["id"],
            fork_run_id="child-run",
        )
        ath.set_run(
            contract["fork_run_id"],
            parent_run_id=contract["parent_run_id"],
            fork_from_event_id=contract["fork_from_event_id"],
        )
        forked = ath.create_issue(title="on the fork")
    finally:
        tc.__exit__(None, None, None)

    ev = [e for e in _events(db_file, "created") if e["target_id"] == forked["id"]][0]
    assert ev["run_id"] == "child-run"
    assert ev["parent_run_id"] == "parent-run"
    assert ev["forked_from_event_id"] == anchor_event["id"]


def test_current_run_reports_the_active_identity(tmp_path):
    tc, ath, _ = _harness(tmp_path)
    try:
        assert ath.current_run() == {
            "run_id": None,
            "parent_run_id": None,
            "fork_from_event_id": None,
        }
        state = ath.set_run("r-1", parent_run_id="r-0", fork_from_event_id=7)
        assert state == {
            "run_id": "r-1",
            "parent_run_id": "r-0",
            "fork_from_event_id": 7,
        }
        assert ath.current_run() == state
    finally:
        tc.__exit__(None, None, None)


def test_reads_are_also_stamped_but_recorded_nowhere(tmp_path):
    # The header rides on every request; only writes create events, so tagging
    # reads is harmless — and it means an agent's whole session shares one run.
    tc, ath, db_file = _harness(tmp_path, run_id="mcp-session")
    try:
        ath.list_issues()
        assert _events(db_file, "created") == []  # reads created no events
    finally:
        tc.__exit__(None, None, None)


# --- MCP server surface -----------------------------------------------------


def test_server_begin_run_and_current_run_tools(tmp_path):
    import asyncio

    from athena.mcp.server import build_server

    tc, ath, db_file = _harness(tmp_path)
    try:
        server = build_server(ath)
        # Write the parent run first: record() keeps a parent only when that run
        # was actually run, so the operator goal must exist before a tool run
        # claims descent from it.
        ath.set_run("goal-run")
        ath.create_issue(title="operator goal")
        asyncio.run(
            server.call_tool(
                "begin_run", {"run_id": "tool-run", "parent_run_id": "goal-run"}
            )
        )
        issue = ath.create_issue(title="via tool-run")
        state = asyncio.run(server.call_tool("current_run", {}))
    finally:
        tc.__exit__(None, None, None)

    ev = [e for e in _events(db_file, "created") if e["target_id"] == issue["id"]][0]
    assert ev["run_id"] == "tool-run" and ev["parent_run_id"] == "goal-run"
    # call_tool returns a content list; the tool's dict rides as JSON text.
    import json

    payload = json.loads(state[0].text)
    assert payload["run_id"] == "tool-run" and payload["parent_run_id"] == "goal-run"


def test_entrypoint_defaults_to_a_fresh_mcp_run_id(monkeypatch):
    # main() must attribute sessions BY DEFAULT: ATHENA_RUN_ID wins when set,
    # otherwise a fresh mcp-<hex> id is minted. Intercept AthenaClient and the
    # server runner so no network or stdio happens.
    from athena.mcp import server as server_module

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _FakeServer:
        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(server_module, "AthenaClient", _FakeClient)
    # main() also probes the token's scopes to size the tool surface; the fake
    # has no whoami, so the probe fails open — accept the keyword and ignore it.
    monkeypatch.setattr(
        server_module, "build_server", lambda client, **_: _FakeServer()
    )

    monkeypatch.delenv("ATHENA_RUN_ID", raising=False)
    monkeypatch.delenv("ATHENA_PARENT_RUN_ID", raising=False)
    server_module.main()
    assert captured["run_id"].startswith("mcp-") and len(captured["run_id"]) > 6
    assert captured["parent_run_id"] is None

    monkeypatch.setenv("ATHENA_RUN_ID", "assigned-run")
    monkeypatch.setenv("ATHENA_PARENT_RUN_ID", "operator-goal")
    server_module.main()
    assert captured["run_id"] == "assigned-run"
    assert captured["parent_run_id"] == "operator-goal"
