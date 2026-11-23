"""A heartbeat's run id defaults from the session and a wrong one is refused (A12).

WHY: ``heartbeat_agent_run`` made the model type a run id the session already
holds (``current_run``), and accepted any other: a seat that copied the wrong id
refreshed a check-in for a run it was not running, and the operator read a run
as alive that nobody was reporting on.

"The session's run id" is the run a request is tagged with: the
``X-Athena-Run`` header on REST, which ``athena-mcp`` sends on every request
with the client's run id (``current_run``). The rule lives in the heartbeat
command, so both transports get it:

- no ``run_id`` in the body: the session's run id is used;
- a ``run_id`` that is not the session's: refused, 422 ``run_id_mismatch``;
- a ``run_id`` with no session run (a REST caller without the header): used,
  as before; neither: refused 422 ``validation_error``, as before.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from support import bearer, bootstrap, make_app, mcp_server_for, onboard_agent

from athena.core import db, refusal_codes

SEAT_SCOPES = ("read", "issue:write")


def _world(tmp_path) -> tuple[Any, TestClient, dict]:
    app, _ = make_app(tmp_path)
    client = TestClient(app)
    client.__enter__()
    bootstrap(client)
    seat = onboard_agent(client, scopes=SEAT_SCOPES)
    return app, client, seat


def test_the_code_is_registered():
    entry = refusal_codes.REFUSAL_CODES["run_id_mismatch"]
    assert entry.statuses == (422,)
    assert entry.next is not None and entry.next.tool == "current_run"


def test_rest_omitted_run_id_is_the_run_header(tmp_path):
    app, client, seat = _world(tmp_path)
    try:
        response = client.put(
            "/agent-runs/heartbeat", json={}, headers=bearer(seat["token"], run="run-a")
        )
        assert response.status_code == 200, response.text
        assert response.json()["run_id"] == "run-a"
    finally:
        client.__exit__(None, None, None)


def test_rest_a_run_id_that_is_not_the_headers_is_refused(tmp_path):
    app, client, seat = _world(tmp_path)
    try:
        refused = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-b"},
            headers=bearer(seat["token"], run="run-a"),
        )
        assert refused.status_code == 422, refused.text
        body = refused.json()
        assert body["code"] == "run_id_mismatch"
        assert "run-a" in body["detail"] and "run-b" in body["detail"]
        fine = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-a"},
            headers=bearer(seat["token"], run="run-a"),
        )
        assert fine.status_code == 200 and fine.json()["run_id"] == "run-a"
    finally:
        client.__exit__(None, None, None)


def test_rest_without_a_session_run_keeps_todays_answers(tmp_path):
    app, client, seat = _world(tmp_path)
    try:
        named = client.put(
            "/agent-runs/heartbeat",
            json={"run_id": "run-c"},
            headers=bearer(seat["token"]),
        )
        assert named.status_code == 200 and named.json()["run_id"] == "run-c"
        neither = client.put(
            "/agent-runs/heartbeat", json={}, headers=bearer(seat["token"])
        )
        assert neither.status_code == 422, neither.text
        assert neither.json()["code"] == "validation_error"
    finally:
        client.__exit__(None, None, None)


def test_a_run_header_the_server_would_drop_or_reshape_is_refused(tmp_path):
    """A header that was sent is never read as absent: a reserved namespace
    (dropped by the request path), one over 200 characters (cut short, so the
    session's run and current_run() would disagree) and one with a tab (kept,
    but no valid check-in key) each refuse run_header_invalid, with or without
    a body run_id, and record no check-in."""
    app, client, seat = _world(tmp_path)
    try:
        assert refusal_codes.REFUSAL_CODES["run_header_invalid"].statuses == (422,)
        for header, why in (
            ("automation:r1", "reserved"),
            ("L" * 300, "200"),
            ("a\tb", "printable"),
        ):
            for body in ({}, {"run_id": "anything"}, {"run_id": header}):
                refused = client.put(
                    "/agent-runs/heartbeat",
                    json=body,
                    headers=bearer(seat["token"], run=header),
                )
                assert refused.status_code == 422, (header[:20], body, refused.text)
                assert refused.json()["code"] == "run_header_invalid", refused.text
                assert why in refused.json()["detail"], refused.json()["detail"]
        conn = db.connect(tmp_path / "athena.db")  # make_app's default file
        try:
            checkins = conn.execute("SELECT COUNT(*) FROM agent_run_checkins")
            assert checkins.fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def _call(server, arguments: dict[str, Any]) -> tuple[bool, Any]:
    async def run():
        async with create_connected_server_and_client_session(
            server._mcp_server  # noqa: SLF001 — the protocol server under test
        ) as session:
            current = await session.call_tool("current_run", {})
            beat = await session.call_tool("heartbeat_agent_run", arguments)
            return json.loads(current.content[0].text), beat

    current, result = asyncio.run(run())
    return current, (result.isError, json.loads(result.content[0].text))


def test_mcp_heartbeat_takes_the_clients_run(tmp_path):
    app, client, seat = _world(tmp_path)
    try:
        server = mcp_server_for(app, seat, run_id="mcp-session-run")
        current, (error, answer) = _call(server, {})
        assert not error, answer
        assert current["run_id"] == "mcp-session-run"
        assert answer["run_id"] == "mcp-session-run"
        _, (error, refused) = _call(server, {"run_id": "some-other-run"})
        assert error and refused["code"] == "run_id_mismatch", refused
        assert refused["next"]["tool"] == "current_run"
        _, (error, same) = _call(server, {"run_id": "mcp-session-run"})
        assert not error and same["run_id"] == "mcp-session-run"
    finally:
        client.__exit__(None, None, None)
