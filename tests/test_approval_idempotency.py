"""An approval-gated write is retryable under its own Idempotency-Key.

`AGENT_GUIDE.md` tells a gated agent to "wait for the decision, then retry the
same write", and `OPERATIONS.md` tells every retry-critical caller to keep ONE
key for one logical request. Before A1 those two instructions contradicted each
other: the 202 `approval_required` was stored as the key's 24-hour receipt, so
after the operator approved, the same key replayed the stale 202 and the close
never applied (review finding MCP-1 / DATA-3).

The contract these pin: a response that reports the write was NOT applied is
never a receipt. The key is released exactly as for a 4xx, so the same request
under the same key runs again; once the operator approves, that retry applies
once and its applied result becomes the receipt. The pending ask itself stays
idempotent through `approval_commands.open_request`, so retrying while waiting never
opens a second request.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from fastapi.testclient import TestClient
from support import replayed_fields

from athena.aegis import issue_commands
from athena.core import db, idempotency
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client):
    onboarded = client.post(
        "/users/onboard_agent",
        json={"email": "sol@e.com", "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()
    return onboarded["user"]["id"], onboarded["token"]["token"]


def _gated_issue(client):
    """An agent gated on issue.close and one open issue it may close."""
    _bootstrap(client)
    agent_id, raw = _agent(client)
    gated = client.put(
        f"/approvals/policies/{agent_id}",
        json={"action_kind": "issue.close"},
        headers=H1,
    )
    assert gated.status_code == 200, gated.text
    bearer = {"Authorization": f"Bearer {raw}"}
    issue = client.post("/issues", json={"title": "gated"}, headers=bearer).json()
    return agent_id, raw, bearer, issue["id"]


def _close(client, issue_id, headers):
    return client.patch(f"/issues/{issue_id}", json={"status": "done"}, headers=headers)


def _decide(client, request_id, decision, note=None):
    body = {"decision": decision}
    if note is not None:
        body["note"] = note
    decided = client.post(f"/approvals/{request_id}/decision", json=body, headers=H1)
    assert decided.status_code == 200, decided.text
    return decided.json()


def _status(client, issue_id):
    return client.get(f"/issues/{issue_id}", headers=H1).json()["status"]


def _count(db_file, sql, *params):
    conn = db.connect(db_file)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _key_record(db_file, key):
    """The durable idempotency rows stored under this key (none once released)."""
    conn = db.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT state, status_code FROM idempotency_keys WHERE idempotency_key = ?",
            (key,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def test_gated_write_retried_under_its_key_applies_after_approval(tmp_path):
    db_file = tmp_path / "gate-key.db"
    with TestClient(create_app(db_file)) as client:
        _, _, bearer, issue_id = _gated_issue(client)
        keyed = {**bearer, "Idempotency-Key": "close-gated-1"}

        asked = _close(client, issue_id, keyed)
        assert asked.status_code == 202
        assert asked.json()["code"] == "approval_required"
        assert asked.headers.get("idempotent-replay") is None
        request_id = asked.json()["approval"]["id"]
        # The write was not applied, so nothing is stored under the key.
        assert _key_record(db_file, "close-gated-1") == []

        # Retrying while still pending runs again and re-reads the SAME ask: the
        # agent sees the current pending state, and the operator's queue does not grow.
        waiting = _close(client, issue_id, keyed)
        assert waiting.status_code == 202
        assert waiting.headers.get("idempotent-replay") is None
        assert waiting.json()["approval"]["id"] == request_id
        assert waiting.json()["approval"]["state"] == "pending"
        assert _count(db_file, "SELECT COUNT(*) FROM approval_requests") == 1
        assert _status(client, issue_id) == "open"

        _decide(client, request_id, "approve", "reviewed")

        # The SAME key now applies the close — the guide's "retry the same write".
        applied = _close(client, issue_id, keyed)
        assert applied.status_code == 200, applied.text
        assert applied.headers.get("idempotent-replay") is None
        assert applied.json()["status"] == "done"
        assert _status(client, issue_id) == "done"

        # The applied result, and only it, is the receipt.
        replay = _close(client, issue_id, keyed)
        assert replay.status_code == 200
        assert replay.headers["idempotent-replay"] == "true"
        assert replay.json() == applied.json()
        assert replay.headers.get("etag") == applied.headers.get("etag")
        assert _key_record(db_file, "close-gated-1") == [
            {"state": "completed", "status_code": 200}
        ]

    assert (
        _count(
            db_file,
            "SELECT COUNT(*) FROM activity WHERE verb = 'approval_consumed'",
        )
        == 1
    )
    assert _count(db_file, "SELECT COUNT(*) FROM approval_requests") == 1


def test_rejected_gate_retried_under_its_key_returns_the_rejection(tmp_path):
    db_file = tmp_path / "gate-reject.db"
    with TestClient(create_app(db_file)) as client:
        _, _, bearer, issue_id = _gated_issue(client)
        keyed = {**bearer, "Idempotency-Key": "close-gated-2"}

        request_id = _close(client, issue_id, keyed).json()["approval"]["id"]
        _decide(client, request_id, "reject", "not this one")

        # The answer reaches the agent under its original key instead of a stale 202.
        answer = _close(client, issue_id, keyed)
        assert answer.status_code == 409
        assert answer.headers.get("idempotent-replay") is None
        body = answer.json()
        assert body["code"] == "approval_rejected"
        assert "not this one" in body["detail"]
        assert body["approval"]["id"] == request_id
        assert body["approval"]["decision_note"] == "not this one"
        assert _status(client, issue_id) == "open"


def test_concurrent_same_key_retries_after_approval_apply_once(tmp_path, monkeypatch):
    db_file = tmp_path / "gate-concurrent.db"
    app = create_app(db_file, idempotency_wait_seconds=10)
    owner_entered = threading.Event()
    release = threading.Event()
    waiter_seen = threading.Event()
    lock = threading.Lock()
    calls = {"count": 0}

    real_update = issue_commands.update_issue

    def observed_update(*args, **kwargs):
        # Hold the owner BEFORE its write transaction opens, so the duplicate's
        # claim can reach the idempotency table instead of waiting on SQLite.
        with lock:
            calls["count"] += 1
        owner_entered.set()
        assert release.wait(timeout=30), "duplicate never observed the owner"
        return real_update(*args, **kwargs)

    real_claim = idempotency.claim_or_read

    def observe_claim(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        if result.kind == "in_progress":
            waiter_seen.set()
            release.set()
        return result

    with TestClient(app) as client:
        _, _, bearer, issue_id = _gated_issue(client)
        keyed = {**bearer, "Idempotency-Key": "close-gated-3"}
        request_id = _close(client, issue_id, keyed).json()["approval"]["id"]
        _decide(client, request_id, "approve")

        monkeypatch.setattr(issue_commands, "update_issue", observed_update)
        monkeypatch.setattr(idempotency, "claim_or_read", observe_claim)
        with ThreadPoolExecutor(max_workers=1) as pool:
            owner_future = pool.submit(_close, client, issue_id, keyed)
            owner_future.add_done_callback(lambda _: owner_entered.set())
            try:
                assert owner_entered.wait(timeout=15), "owner did not enter the route"
                if owner_future.done():
                    early = owner_future.result()
                    raise AssertionError(
                        f"owner returned HTTP {early.status_code} before the duplicate"
                    )
                waiter = _close(client, issue_id, keyed)
            finally:
                release.set()
            owner = owner_future.result(timeout=15)
        monkeypatch.undo()
        status = _status(client, issue_id)

    assert waiter_seen.is_set(), "duplicate never observed the in-progress claim"
    assert calls["count"] == 1
    assert owner.status_code == waiter.status_code == 200
    assert owner.headers.get("idempotent-replay") is None
    assert waiter.headers["idempotent-replay"] == "true"
    assert owner.json() == waiter.json()
    assert status == "done"
    assert (
        _count(
            db_file,
            "SELECT COUNT(*) FROM activity WHERE verb = 'approval_consumed'",
        )
        == 1
    )
    assert (
        _count(
            db_file,
            "SELECT COUNT(*) FROM approval_requests WHERE consumed_at IS NOT NULL",
        )
        == 1
    )


def test_mcp_update_issue_retried_under_its_key_applies_after_approval(tmp_path):
    db_file = tmp_path / "gate-mcp.db"
    app = create_app(db_file)
    with TestClient(app) as admin:
        _, raw, _, issue_id = _gated_issue(admin)
        # The agent's own transport: its bearer is the default header, as athena-mcp
        # sends it. The outer client already ran the app lifespan.
        agent_http = TestClient(app, headers={"Authorization": f"Bearer {raw}"})
        mcp = mcp_server.build_server(
            AthenaClient(client=agent_http),
            scopes=frozenset({"read", "issue:write"}),
            profile=mcp_server.PROFILES["seat"],
        )
        arguments = {
            "issue_id": issue_id,
            "status": "done",
            "idempotency_key": "mcp-close-1",
        }

        def call():
            content, _ = asyncio.run(mcp.call_tool("update_issue", arguments))
            return json.loads(content[0].text)

        asked = call()
        assert asked["code"] == "approval_required"
        again = call()
        assert again["approval"]["id"] == asked["approval"]["id"]
        _decide(admin, asked["approval"]["id"], "approve")

        applied = call()
        assert applied["status"] == "done"
        assert _status(admin, issue_id) == "done"
        assert replayed_fields(call()) == applied
