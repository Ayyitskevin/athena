"""The credential that sent an idempotency key can read its state (A11).

Before: a keyed write that timed out left its caller with a key and no answer.
A retry could itself be refused, 409 ``idempotency_in_progress`` or 409
``idempotency_indeterminate``, and no route or tool read a key's state, so the
caller could only retry and interpret the refusal. Now ``GET
/idempotency/{key}`` and the ``get_idempotency_key`` tool say what an
identical retry would meet, and change nothing.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient
import pytest

from support import (
    ADMIN,
    bearer,
    bootstrap,
    make_app,
    mint_token,
    onboard_agent,
    raw_token,
)

from athena.core import db, idempotency
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient


def _state(client: TestClient, who: dict, key: str):
    return client.get(f"/idempotency/{key}", headers=bearer(who))


def _rows(db_file) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT idempotency_key, state, status_code, failure_code, "
                "response_body IS NOT NULL AS has_body FROM idempotency_keys "
                "ORDER BY idempotency_key"
            )
        ]
    finally:
        conn.close()


def _stage(db_file, who: dict, key: str, **claim) -> idempotency.ClaimResult:
    """Claim ``key`` as ``who``'s credential, as a request that never finished."""
    conn = db.connect(db_file)
    try:
        return idempotency.claim_or_read(
            conn,
            key=key,
            identity=idempotency.token_identity(raw_token(who)),
            method="POST",
            path="/issues",
            request_fingerprint="staged",
            **{"lease_seconds": 60, **claim},
        )
    finally:
        conn.close()


def _keyed_create(client: TestClient, who: dict, key: str):
    return client.post(
        "/issues",
        json={"title": "keyed"},
        headers={**bearer(who), "Idempotency-Key": key},
    )


def test_a_key_never_sent_is_unused(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        answer = _state(client, sol, "never-sent")

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body == {
        "key": "never-sent",
        "state": "unused",
        "method": None,
        "path": None,
        "status_code": None,
        "retry": body["retry"],
    }
    assert body["retry"].startswith("executes")
    assert _rows(db_file) == []


def test_a_finished_write_reads_completed_and_the_read_changes_nothing(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        made = _keyed_create(client, sol, "run-7/step:2")
        before = _rows(db_file)
        answer = _state(client, sol, "run-7/step:2")
        after = _rows(db_file)
        again = _keyed_create(client, sol, "run-7/step:2")

    assert made.status_code == 201, made.text
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["state"], body["method"], body["path"], body["status_code"]) == (
        "completed",
        "POST",
        "/issues",
        201,
    )
    assert body["retry"].startswith("replays")
    assert before == after and len(after) == 1
    # What the read said an identical retry would meet is what it meets.
    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json()["id"] == made.json()["id"]


def test_only_the_credential_that_sent_the_key_reads_it(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        bo = onboard_agent(client, email="bo@e.com", name="Bo")
        second = mint_token(
            client, scopes=("read", "issue:write"), user_id=sol["user"]["id"]
        )
        assert _keyed_create(client, sol, "mine").status_code == 201
        states = {
            "sol": _state(client, sol, "mine").json()["state"],
            "another account": _state(client, bo, "mine").json()["state"],
            "sol's other token": _state(client, second, "mine").json()["state"],
            "the operator": client.get("/idempotency/mine", headers=ADMIN).json()[
                "state"
            ],
        }
        anonymous = client.get("/idempotency/mine")

    assert states == {
        "sol": "completed",
        "another account": "unused",
        "sol's other token": "unused",
        "the operator": "unused",
    }
    assert anonymous.status_code == 401


def test_a_request_still_running_reads_in_progress(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        assert _stage(db_file, sol, "running").kind == "owner"
        body = _state(client, sol, "running").json()

    assert (body["state"], body["method"], body["path"], body["status_code"]) == (
        "in_progress",
        "POST",
        "/issues",
        None,
    )
    assert "idempotency_in_progress" in body["retry"]


def test_a_request_whose_owner_went_away_reads_indeterminate(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        # Its lease ran out an hour ago and nothing published a result.
        _stage(db_file, sol, "abandoned", now=int(time.time()) - 3600)
        owner = _stage(db_file, sol, "marked")
        conn = db.connect(db_file)
        try:
            assert idempotency.mark_indeterminate(
                conn,
                key="marked",
                identity=idempotency.token_identity(raw_token(sol)),
                owner_token=owner.record["owner_token"],
                failure_code="response_too_large",
            )
        finally:
            conn.close()
        abandoned = _state(client, sol, "abandoned").json()
        marked = _state(client, sol, "marked").json()

    for body in (abandoned, marked):
        assert body["state"] == "indeterminate"
        assert (body["method"], body["path"]) == ("POST", "/issues")
        assert "idempotency_indeterminate" in body["retry"]


def test_a_key_fenced_by_a_change_of_authority_says_so_and_keeps_its_row(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue = client.post("/issues", json={"title": "slice"}, headers=ADMIN).json()
        handed = client.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": sol["user"]["id"]},
            headers=ADMIN,
        )
        assert handed.status_code == 201, handed.text
        keyed = {**bearer(sol), "Idempotency-Key": "edit-1"}
        edited = client.patch(
            f"/issues/{issue['id']}", json={"priority": "high"}, headers=keyed
        )
        assert edited.status_code == 200, edited.text
        fine = _state(client, sol, "edit-1").json()["state"]
        # The issue's assignment changes: what Sol may do there may have too.
        assigned = client.put(
            f"/issues/{issue['id']}/assignee", json={"assignee_id": 1}, headers=ADMIN
        )
        assert assigned.status_code == 200, assigned.text
        body = _state(client, sol, "edit-1").json()
        kept = _rows(db_file)
        retried = client.patch(
            f"/issues/{issue['id']}", json={"priority": "high"}, headers=keyed
        )
        # The retry purged the stored answer and left its marker; the read
        # says the same of the marker as it said of the receipt.
        marked = (_state(client, sol, "edit-1").json()["state"], _rows(db_file))

    assert marked[0] == "authorization_changed"
    assert [(row["state"], row["has_body"]) for row in marked[1]] == [
        ("indeterminate", 0)
    ]
    assert fine == "completed"
    assert body["state"] == "authorization_changed"
    assert body["status_code"] is None
    # The read purges nothing: the stored answer is still there for the retry
    # to fence, which is the write the retry owns.
    assert [(row["state"], row["has_body"]) for row in kept] == [("completed", 1)]
    assert retried.status_code == 409
    assert retried.json()["code"] == "idempotency_authorization_changed"


def test_an_expired_receipt_reads_unused_and_is_not_deleted(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        assert _keyed_create(client, sol, "old").status_code == 201
    conn = db.connect(db_file)
    try:
        identity = idempotency.token_identity(raw_token(sol))
        now = idempotency.key_state(conn, key="old", identity=identity)
        later = idempotency.key_state(
            conn, key="old", identity=identity, now=int(time.time()) + 10**9
        )
    finally:
        conn.close()

    assert (now["state"], later["state"]) == ("completed", "unused")
    assert len(_rows(db_file)) == 1


@pytest.mark.parametrize("key", ["x" * 256, "has%20space", "caf%C3%A9"])
def test_a_key_that_could_not_be_one_is_refused(tmp_path, key):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        refused = _state(client, sol, key)

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "idempotency_key_invalid"


def test_a_seat_reads_it_as_a_tool(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        assert _keyed_create(client, sol, "run-7/step:2").status_code == 201
        api = AthenaClient(client=TestClient(app, headers=bearer(sol)))
        server = mcp_server.build_server(api, profile=mcp_server.PROFILES["seat"])
        answer = asyncio.run(
            server.call_tool("get_idempotency_key", {"key": "run-7/step:2"})
        )

    body = json.loads(answer[0][0].text) if isinstance(answer, tuple) else answer
    assert (body["key"], body["state"], body["path"]) == (
        "run-7/step:2",
        "completed",
        "/issues",
    )
