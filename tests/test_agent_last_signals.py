"""What an agent was last refused and when it last found nothing ready is
readable by the operator (F15, FLEET-11).

Before: a refusal is an answer and "nothing ready" is an answer, so neither
left anything behind, and an operator looking at an idle agent could not tell
one refused on every call from one with no work. Now the newest of each is
kept per agent account and read with the agent's run health.
"""

from __future__ import annotations

import asyncio
import re

from fastapi.testclient import TestClient
import pytest

from support import (
    ADMIN,
    bearer,
    bootstrap,
    login,
    make_app,
    mint_token,
    onboard_agent,
)

from athena.core import agent_signal_commands, db, packet_time
from athena.middleware import RefusalTraceMiddleware


def _signals(client: TestClient, agent: dict) -> dict:
    health = client.get(
        "/activity/agent-runs", params={"agent_id": agent["user"]["id"]}, headers=ADMIN
    )
    assert health.status_code == 200, health.text
    (row,) = health.json()["agents"]
    return {
        "last_refusal": row["last_refusal"],
        "last_nothing_ready_at": row["last_nothing_ready_at"],
    }


def _stored(db_file) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM agent_last_signals")]
    finally:
        conn.close()


def _issue(client: TestClient, title: str = "slice") -> int:
    made = client.post("/issues", json={"title": title}, headers=ADMIN)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _claim(client: TestClient, agent: dict, issue_id: int):
    tag = client.get(f"/issues/{issue_id}", headers=bearer(agent)).headers["ETag"]
    return client.post(
        f"/issues/{issue_id}/claim",
        json={},
        headers={**bearer(agent), "If-Match": tag},
    )


def test_an_agent_never_refused_reads_as_null(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        assert client.get("/issues", headers=bearer(sol)).status_code == 200
        signals = _signals(client, sol)

    assert signals == {"last_refusal": None, "last_nothing_ready_at": None}
    assert _stored(db_file) == []


def test_the_newest_refusal_is_kept_and_replaces_the_one_before(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        missing = client.patch(
            "/issues/999", json={"priority": "high"}, headers=bearer(sol)
        )
        assert missing.status_code == 404, missing.text
        first = _signals(client, sol)["last_refusal"]
        invalid = client.post("/issues", json={"title": ""}, headers=bearer(sol))
        assert invalid.status_code == 422, invalid.text
        second = _signals(client, sol)["last_refusal"]
        # An answer that is not a refusal leaves the kept one as it is.
        assert client.get("/issues", headers=bearer(sol)).status_code == 200
        after_success = _signals(client, sol)["last_refusal"]

    assert first is not None
    assert {k: first[k] for k in ("code", "status_code", "request")} == {
        "code": missing.json()["code"],
        "status_code": 404,
        "request": "PATCH /issues/999",
    }
    assert re.fullmatch(packet_time.PATTERN, first["at"])
    assert {k: second[k] for k in ("code", "status_code", "request")} == {
        "code": "validation_error",
        "status_code": 422,
        "request": "POST /issues",
    }
    assert after_success == second
    # One row per agent account, replaced in place: not a log.
    assert [row["user_id"] for row in _stored(db_file)] == [sol["user"]["id"]]


def _scope_denied(client: TestClient, tmp) -> tuple:
    reader = onboard_agent(client, email="r@e.com", name="Reader", scopes=("read",))
    return reader, client.post("/issues", json={"title": "x"}, headers=bearer(reader))


def _not_eligible(client: TestClient, tmp) -> tuple:
    # A command error the route answers as a response it builds, not one it
    # raises: an issue neither assigned nor delegated to the caller.
    issue_id = _issue(client)
    stranger = onboard_agent(client, email="f@e.com", name="Stranger")
    return stranger, _claim(client, stranger, issue_id)


def _budget_spent(client: TestClient, tmp) -> tuple:
    issue_id = _issue(client)
    spender = onboard_agent(client, email="b@e.com", name="Spender")
    set_budget = client.put(
        f"/users/{spender['user']['id']}/budget",
        json={"window": "day", "action_limit": 0},
        headers=ADMIN,
    )
    assert set_budget.status_code == 200, set_budget.text
    return spender, client.patch(
        f"/issues/{issue_id}", json={"priority": "high"}, headers=bearer(spender)
    )


def _stale_tag(client: TestClient, tmp) -> tuple:
    issue_id = _issue(client)
    agent = onboard_agent(client, email="t@e.com", name="Tagger")
    return agent, client.post(
        f"/issues/{issue_id}/claim",
        json={},
        headers={**bearer(agent), "If-Match": 'W/"issue-0-stale"'},
    )


@pytest.mark.parametrize(
    "refuse", [_scope_denied, _not_eligible, _budget_spent, _stale_tag]
)
def test_every_handlers_refusal_is_kept_and_its_answer_is_unchanged(tmp_path, refuse):
    """Refusals are written by several handlers (the shared one, the scope
    denial's, the budget's, a command error answered as a response). Each
    leaves through the same place, and what the agent receives is the same
    answer, byte for byte, as an account that is not traced receives."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent, refused = refuse(client, tmp_path)
        kept = _signals(client, agent)["last_refusal"]

    assert 400 <= refused.status_code <= 499, refused.text
    body = refused.json()
    assert kept is not None
    assert kept["code"] == body["code"]
    assert kept["status_code"] == refused.status_code
    assert kept["request"] == f"{refused.request.method} {refused.request.url.path}"
    assert int(refused.headers["content-length"]) == len(refused.content)
    if refused.status_code == 429:
        assert refused.headers["Retry-After"] == str(body["retry_after_seconds"])


def test_a_persons_refusal_is_not_kept(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        person = mint_token(client, scopes=("read", "issue:write"))
        refused = client.patch(
            "/issues/999", json={"priority": "high"}, headers=bearer(person)
        )

    assert refused.status_code == 404, refused.text
    assert _stored(db_file) == []


def test_a_failure_to_keep_it_never_changes_the_answer(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the trace is down")

    monkeypatch.setattr(agent_signal_commands, "record_refusal", broken)
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        refused = client.patch(
            "/issues/999", json={"priority": "high"}, headers=bearer(sol)
        )

    assert refused.status_code == 404, refused.text
    assert refused.json()["code"] == "not_found"
    assert _stored(db_file) == []


def test_a_pickup_that_finds_nothing_leaves_the_time(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        empty = client.post("/issues/claim-next", json={}, headers=bearer(sol))
        assert empty.status_code == 200, empty.text
        assert empty.json()["reason"] == "nothing_ready"
        signals = _signals(client, sol)

    assert signals["last_refusal"] is None
    assert re.fullmatch(packet_time.PATTERN, signals["last_nothing_ready_at"])


def test_a_pickup_that_claims_or_already_holds_leaves_no_time(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue_id = _issue(client)
        assigned = client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": sol["user"]["id"]},
            headers=ADMIN,
        )
        assert assigned.status_code == 200, assigned.text
        taken = client.post("/issues/claim-next", json={}, headers=bearer(sol))
        assert taken.json()["claimed"] is True, taken.text
        holding = client.post("/issues/claim-next", json={}, headers=bearer(sol))
        assert holding.json()["reason"] == "already_holding", holding.text
        signals = _signals(client, sol)

    assert signals == {"last_refusal": None, "last_nothing_ready_at": None}


def test_a_paused_agents_refusal_is_kept(tmp_path):
    """The pause is refused where the account is resolved, before any route:
    the refusal is attributed all the same."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        paused = client.put(
            f"/users/{sol['user']['id']}/paused", json={"paused": True}, headers=ADMIN
        )
        assert paused.status_code == 200, paused.text
        refused = client.get("/issues", headers=bearer(sol))
        kept = _signals(client, sol)["last_refusal"]

    assert refused.status_code == 403, refused.text
    assert kept is not None
    assert (kept["code"], kept["request"]) == ("account_paused", "GET /issues")


def test_only_a_refusal_of_an_agent_is_written(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
    conn = db.connect(db_file)
    try:
        fact = {"code": "not_found", "method": "GET", "path": "/issues/9"}
        server_error = agent_signal_commands.record_refusal(
            conn, user_id=sol["user"]["id"], status_code=500, **fact
        )
        person = agent_signal_commands.record_refusal(
            conn, user_id=1, status_code=404, **fact
        )
        long_path = agent_signal_commands.record_refusal(
            conn,
            user_id=sol["user"]["id"],
            status_code=404,
            code="not_found",
            method="GET",
            path="/" + "x" * 5000,
        )
    finally:
        conn.close()

    assert (server_error, person, long_path) == (False, False, True)
    (row,) = _stored(db_file)
    assert len(row["last_refusal_request"]) == 300


def test_the_operators_page_shows_both(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        login(client, "a@e.com")
        before = client.get("/admin/agents/runs").text
        client.cookies.clear()
        client.patch("/issues/999", json={"priority": "high"}, headers=bearer(sol))
        client.post("/issues/claim-next", json={}, headers=bearer(sol))
        login(client, "a@e.com")
        after = client.get("/admin/agents/runs").text

    assert "Last refused" not in before and "nothing ready" not in before
    assert "Last refused: <code>not_found</code> on PATCH /issues/999" in after
    assert "Last found nothing ready: " in after


def _drive(app_messages: list[dict], *, db_path) -> tuple[list[dict], list[int]]:
    """Run the trace around an app that sends ``app_messages`` for a request an
    account authenticated; returns what went out and, for each message the app
    sent, how many had gone out by the time it sent the next."""
    sent: list[dict] = []
    forwarded_before: list[int] = []

    async def app(scope, receive, send):
        scope.setdefault("state", {})["refusal_actor_id"] = 2
        for message in app_messages:
            forwarded_before.append(len(sent))
            await send(message)

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/issues", "headers": []}
    asyncio.run(RefusalTraceMiddleware(app, db_path=db_path)(scope, receive, send))
    return sent, forwarded_before


def test_an_answer_that_is_not_a_refusal_is_never_held(tmp_path):
    """Only a 4xx is held while its row is written. A success streams through
    as the app sends it, message for message."""
    app_messages = [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"first", "more_body": True},
        {"type": "http.response.body", "body": b"second", "more_body": False},
    ]
    sent, forwarded_before = _drive(app_messages, db_path=tmp_path / "unused.db")

    assert sent == app_messages
    assert forwarded_before == [0, 1, 2]


def test_a_refusal_too_large_to_hold_goes_out_whole_and_untraced(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        onboard_agent(client)
    chunk = b"x" * (40 * 1024)
    sent, _ = _drive(
        [
            {"type": "http.response.start", "status": 404, "headers": []},
            {"type": "http.response.body", "body": chunk, "more_body": True},
            {"type": "http.response.body", "body": chunk, "more_body": True},
            {"type": "http.response.body", "body": chunk, "more_body": False},
        ],
        db_path=db_file,
    )

    assert sent[0] == {"type": "http.response.start", "status": 404, "headers": []}
    assert b"".join(m["body"] for m in sent[1:]) == chunk * 3
    assert sent[-1]["more_body"] is False
    assert _stored(db_file) == []
