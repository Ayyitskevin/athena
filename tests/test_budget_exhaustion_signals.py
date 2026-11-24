"""A budget exhausted mid-task says what to do, and tells who answers for the
work once per window (F13, FLEET-13).

Before: the 429 named no next step, the conductor that delegated the work was
told nothing, and the operator's trail and attention list gained one row for
every refused request. Now the refusal names the leases the seat holds, so it
can yield them with reason ``capacity``; the first refusal of a budget window
records the exhaustion once and tells each held issue's assignee and the
account that delegated it; a later refusal in the same window records nothing.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from support import ADMIN, bearer, bootstrap, login, make_app, onboard_agent

from athena.aegis import fleet_attention
from athena.core import budget_commands, db, refusal_codes
from athena.core._util import utc_now

HOLDER_VERB = "holder_budget_exhausted"


def _events(db_file, verb: str) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT id, actor_id, target_kind, target_id, detail FROM activity "
                "WHERE verb = ? ORDER BY id",
                (verb,),
            )
        ]
    finally:
        conn.close()


def _inbox_rows(db_file, verb: str) -> list[int]:
    """Who has an inbox row for an event of ``verb``, as stored: the inbox read
    hides a row on an issue its owner cannot see, so a row that should never
    have been written is checked here."""
    conn = db.connect(db_file)
    try:
        return [
            row["user_id"]
            for row in conn.execute(
                "SELECT n.user_id FROM notifications n "
                "JOIN activity a ON a.id = n.event_id WHERE a.verb = ? "
                "ORDER BY n.user_id",
                (verb,),
            )
        ]
    finally:
        conn.close()


def _inbox(client: TestClient, who: dict, verb: str) -> list[dict]:
    rows = client.get("/notifications", headers=bearer(who)).json()
    rows = rows if isinstance(rows, list) else rows["items"]
    return [row for row in rows if row["verb"] == verb]


def _world(client: TestClient) -> dict:
    """A conductor delegates one issue to a seat, which claims it and has a
    budget of two metered writes a day."""
    bootstrap(client)
    cora = onboard_agent(client, email="cora@e.com", name="Cora Conductor")
    sol = onboard_agent(client, email="sol@e.com", name="Sol Seat")
    made = client.post("/issues", json={"title": "slice"}, headers=bearer(cora))
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    handed = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": sol["user"]["id"]},
        headers=bearer(cora),
    )
    assert handed.status_code == 201, handed.text
    # The seat is the assignee too: one of the two accounts a held issue's
    # signals go to, and the one that must not be told about its own refusal.
    assigned = client.put(
        f"/issues/{issue_id}/assignee",
        json={"assignee_id": sol["user"]["id"]},
        headers=ADMIN,
    )
    assert assigned.status_code == 200, assigned.text
    tag = client.get(f"/issues/{issue_id}", headers=bearer(sol)).headers["ETag"]
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        json={},
        headers={**bearer(sol), "If-Match": tag},
    )
    assert claimed.status_code == 201, claimed.text
    budget = client.put(
        f"/users/{sol['user']['id']}/budget",
        json={"window": "day", "action_limit": 2},
        headers=ADMIN,
    )
    assert budget.status_code == 200, budget.text
    return {
        "cora": cora,
        "sol": sol,
        "issue_id": issue_id,
        "generation": claimed.json()["generation"],
    }


def _write(client: TestClient, world: dict, priority: str):
    return client.patch(
        f"/issues/{world['issue_id']}",
        json={"priority": priority},
        headers=bearer(world["sol"]),
    )


def _spend(client: TestClient, world: dict) -> None:
    for priority in ("high", "low"):
        assert _write(client, world, priority).status_code == 200


def test_the_refusal_names_the_leases_to_yield(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _spend(client, world)
        refused = _write(client, world, "urgent")

    assert refused.status_code == 429, refused.text
    body = refused.json()
    assert body["code"] == "agent_budget_exhausted"
    assert body["held_leases"] == [
        {"issue_id": world["issue_id"], "generation": world["generation"]}
    ]
    assert body["retry_after_seconds"] == int(refused.headers["Retry-After"])
    registered = refusal_codes.REFUSAL_CODES["agent_budget_exhausted"]
    assert registered.next is not None and registered.next.tool == "yield_claim"
    assert "capacity" in registered.next.hint


def test_the_yield_the_refusal_names_is_not_metered_and_hands_the_work_back(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _spend(client, world)
        refused = _write(client, world, "urgent").json()
        lease = refused["held_leases"][0]
        yielded = client.post(
            f"/issues/{lease['issue_id']}/yield",
            json={
                "generation": lease["generation"],
                "reason": "capacity",
                "attempted_work": "Two priority changes; the third was refused.",
                "evidence": ["the 429 named this lease"],
                "blocking_question": "Who continues while my budget is spent?",
                "resume_instructions": "Continue from the last comment.",
            },
            headers=bearer(world["sol"]),
        )
        after = _write(client, world, "urgent").json()

    assert yielded.status_code == 201, yielded.text
    assert after["code"] == "agent_budget_exhausted"
    assert after["held_leases"] == []


def test_the_first_refusal_of_a_window_tells_who_answers_for_the_work(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _spend(client, world)
        for priority in ("urgent", "medium", "urgent"):
            assert _write(client, world, priority).status_code == 429
        cora_told = _inbox(client, world["cora"], HOLDER_VERB)
        sol_told = _inbox(client, world["sol"], HOLDER_VERB)

    # One exhaustion on the trail for three refused requests.
    exhausted = _events(db_file, "agent_budget_exhausted")
    assert [e["actor_id"] for e in exhausted] == [world["sol"]["user"]["id"]]
    # One event on the held issue, by the holder, naming the generation.
    held = _events(db_file, HOLDER_VERB)
    assert [(e["actor_id"], e["target_kind"], e["target_id"]) for e in held] == [
        (world["sol"]["user"]["id"], "issue", world["issue_id"])
    ]
    assert held[0]["detail"].startswith(f"generation {world['generation']};")
    # The conductor that delegated the work is told once; the seat, which was
    # refused to its face, is not told again.
    assert [row["target_id"] for row in cora_told] == [world["issue_id"]]
    assert sol_told == []
    assert _inbox_rows(db_file, HOLDER_VERB) == [world["cora"]["user"]["id"]]


def test_the_operators_attention_has_one_row_per_account_and_window(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _spend(client, world)
        for priority in ("urgent", "medium", "urgent", "medium"):
            assert _write(client, world, priority).status_code == 429
        ranking = client.get("/attention/ranking", headers=ADMIN)
        assert ranking.status_code == 200, ranking.text
    conn = db.connect(db_file)
    try:
        rollup = fleet_attention.build_attention(conn)
    finally:
        conn.close()

    rows = [
        item
        for item in ranking.json()["items"]
        if item["signal"] == "budget_exhaustion"
    ]
    assert [row["owner_id"] for row in rows] == [world["sol"]["user"]["id"]]
    assert "2/2" in rows[0]["reason"] and "1 lease" in rows[0]["reason"]
    counts = {signal["key"]: signal["count"] for signal in rollup["signals"]}
    assert counts["budget_exhaustions"] == 1


def test_a_new_window_is_a_new_exhaustion(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _spend(client, world)
        assert _write(client, world, "urgent").status_code == 429
        # The stored window ended: the next charge rolls it and spends again.
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE agent_budgets SET window_started_at = '2020-01-01T00:00:00Z'"
        )
        conn.execute(
            "UPDATE activity SET created_at = '2020-01-01 00:00:05' "
            "WHERE verb IN ('agent_budget_exhausted', ?)",
            (HOLDER_VERB,),
        )
        conn.commit()
        conn.close()
        _spend(client, world)
        for priority in ("urgent", "medium"):
            assert _write(client, world, priority).status_code == 429

    assert len(_events(db_file, "agent_budget_exhausted")) == 2
    assert len(_events(db_file, HOLDER_VERB)) == 2


def test_a_frozen_account_past_its_stored_window_is_still_one_exhaustion(
    tmp_path, monkeypatch
):
    """A limit of zero refuses every write and never charges, so nothing ever
    stores the window the refusals fall in. The recorder stores it, or every
    refusal would open a window of its own and be told again."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        frozen = client.put(
            f"/users/{world['sol']['user']['id']}/budget",
            json={"window": "hour", "action_limit": 0},
            headers=ADMIN,
        )
        assert frozen.status_code == 200, frozen.text
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE agent_budgets SET window_started_at = '2020-01-01T00:00:00Z'"
        )
        conn.commit()
        conn.close()
        assert _write(client, world, "urgent").status_code == 429
        # Later in the same hour, by the recorder's clock.
        later = utc_now() + timedelta(minutes=10)
        monkeypatch.setattr(budget_commands, "utc_now", lambda: later)
        for priority in ("medium", "urgent"):
            assert _write(client, world, priority).status_code == 429

    assert len(_events(db_file, "agent_budget_exhausted")) == 1
    assert len(_events(db_file, HOLDER_VERB)) == 1


def test_an_account_that_no_longer_sees_the_issue_is_not_told(tmp_path):
    """The delegating account lost sight of the issue. It is not a watcher, so
    the only row it could get is the one this signal writes for an accountable
    account, and that is written only for one that can still see the issue.
    (A watcher's row is the notification owner's business: it is stored, and
    hidden by the inbox read while the issue is.)"""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        unwatched = client.delete(
            f"/watches/issue/{world['issue_id']}", headers=bearer(world["cora"])
        )
        assert unwatched.status_code == 204, unwatched.text
        project = client.post(
            "/projects", json={"name": "Closed", "key": "CLS"}, headers=ADMIN
        ).json()
        client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": world["sol"]["user"]["id"]},
            headers=ADMIN,
        )
        tag = client.get(f"/issues/{world['issue_id']}", headers=ADMIN).headers["ETag"]
        moved = client.patch(
            f"/issues/{world['issue_id']}",
            json={"project_id": project["id"]},
            headers={**ADMIN, "If-Match": tag},
        )
        assert moved.status_code == 200, moved.text
        hidden = client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        assert hidden.status_code == 200, hidden.text
        _spend(client, world)
        assert _write(client, world, "urgent").status_code == 429
        cora_told = _inbox(client, world["cora"], HOLDER_VERB)

    assert len(_events(db_file, HOLDER_VERB)) == 1
    assert cora_told == []
    # No row was written for her, not merely hidden by the inbox read.
    assert _inbox_rows(db_file, HOLDER_VERB) == []


def test_the_issue_history_says_it_in_words(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _spend(client, world)
        assert _write(client, world, "urgent").status_code == 429
        login(client, "a@e.com")
        page = client.get(f"/aegis/issues/{world['issue_id']}").text

    assert "ran out of budget while holding this lease" in page
    # The event's name is for programs; the page never shows it raw.
    assert f">{HOLDER_VERB}<" not in page and f" {HOLDER_VERB} " not in page
