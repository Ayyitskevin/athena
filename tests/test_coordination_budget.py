"""Comments, claims and delegations count against a second budget counter
(C7, decision D6).

Before: the budget metered issue and page writes only. A budget of 3 an hour
did not stop 5 comments, and nothing an operator could set bounded how much an
agent talked, claimed or delegated. Now a budget may carry a coordination
ceiling, counted apart from the action ceiling in the same window. Renewals,
yields and completions are never metered.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, bearer, bootstrap, login, make_app, onboard_agent

from athena.aegis import fleet_attention
from athena.core import budgets, db
from athena.core._util import utc_now

CODE = "agent_coordination_budget_exhausted"


def _count(db_file, sql: str, params: tuple = ()) -> int:
    conn = db.connect(db_file)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _events(db_file, verb: str) -> list[str]:
    conn = db.connect(db_file)
    try:
        return [
            row["detail"]
            for row in conn.execute(
                "SELECT detail FROM activity WHERE verb = ? ORDER BY id", (verb,)
            )
        ]
    finally:
        conn.close()


def _budget(client: TestClient, agent: dict, **limits):
    body = {"window": "hour", "action_limit": 100, **limits}
    return client.put(f"/users/{agent['user']['id']}/budget", json=body, headers=ADMIN)


def _issue(client: TestClient, agent: dict, title: str = "slice") -> int:
    """An issue the agent may claim: made by the operator, delegated to it."""
    made = client.post("/issues", json={"title": title}, headers=ADMIN)
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    handed = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    assert handed.status_code == 201, handed.text
    return issue_id


def _comment(client: TestClient, agent: dict, issue_id: int):
    return client.post(
        f"/issues/{issue_id}/comments", json={"body": "a note"}, headers=bearer(agent)
    )


def _claim(client: TestClient, agent: dict, issue_id: int, **body):
    headers = bearer(agent)
    if "generation" not in body:
        tag = client.get(f"/issues/{issue_id}", headers=headers).headers["ETag"]
        headers = {**headers, "If-Match": tag}
    return client.post(f"/issues/{issue_id}/claim", json=body, headers=headers)


def test_without_a_coordination_ceiling_comments_are_not_limited(tmp_path):
    """The baseline, unchanged for every budget set before D6: an action budget
    of 3 does not stop 5 comments."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue_id = _issue(client, sol)
        assert _budget(client, sol, action_limit=3).status_code == 200
        answers = [_comment(client, sol, issue_id).status_code for _ in range(5)]
        read = client.get(f"/users/{sol['user']['id']}/budget", headers=ADMIN).json()

    assert answers == [201] * 5
    assert (read["coordination_limit"], read["coordination_remaining"]) == (None, None)
    assert read["action_used"] == 0 and read["coordination_used"] == 0
    assert _events(db_file, "agent_budget_set") == ["3 actions per hour"]


def test_the_ceiling_refuses_the_comment_past_it_and_applies_nothing(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue_id = _issue(client, sol)
        assert _budget(client, sol, coordination_limit=3).status_code == 200
        answers = [_comment(client, sol, issue_id) for _ in range(5)]
        me = client.get("/users/me", headers=bearer(sol)).json()

    assert [a.status_code for a in answers] == [201, 201, 201, 429, 429]
    refused = answers[3]
    body = refused.json()
    assert body["code"] == CODE
    assert body["budget"]["coordination_limit"] == 3
    assert body["budget"]["coordination_used"] == 3
    assert body["budget"]["coordination_remaining"] == 0
    assert body["retry_after_seconds"] == int(refused.headers["Retry-After"]) > 0
    # Refused to its face only: no lease is named and nobody is told.
    assert "held_leases" not in body
    assert _count(db_file, "SELECT COUNT(*) FROM comments") == 3
    assert me["budget"]["coordination_remaining"] == 0
    # Once per window, under its own verb, however many requests were refused.
    assert _events(db_file, budgets.VERB_COORDINATION_EXHAUSTED) == [
        f"3/3 coordination writes this hour; first refused: POST /issues/{issue_id}/comments"
    ]
    assert _events(db_file, budgets.VERB_BUDGET_EXHAUSTED) == []
    assert _events(db_file, "holder_budget_exhausted") == []
    assert _events(db_file, "agent_budget_set") == [
        "100 actions per hour; 3 coordination writes per hour"
    ]


def test_the_two_counters_are_spent_apart(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue_id = _issue(client, sol)
        assert (
            _budget(client, sol, action_limit=1, coordination_limit=1).status_code
            == 200
        )
        first_comment = _comment(client, sol, issue_id)
        # Coordination is spent; a write that changes the work still goes.
        edit = client.patch(
            f"/issues/{issue_id}", json={"priority": "high"}, headers=bearer(sol)
        )
        second_comment = _comment(client, sol, issue_id)
        second_edit = client.patch(
            f"/issues/{issue_id}", json={"priority": "low"}, headers=bearer(sol)
        )
        read = client.get(f"/users/{sol['user']['id']}/budget", headers=ADMIN).json()
        conn = db.connect(db_file)
        try:
            counts = fleet_attention.needs_you(conn)
        finally:
            conn.close()

    assert (first_comment.status_code, edit.status_code) == (201, 200)
    assert second_comment.json()["code"] == CODE
    assert second_edit.json()["code"] == "agent_budget_exhausted"
    assert (read["action_used"], read["coordination_used"]) == (1, 1)
    # Each ceiling's first refusal of the window is recorded; one being spent
    # does not silence the other, and the operator's card counts both.
    assert len(_events(db_file, budgets.VERB_COORDINATION_EXHAUSTED)) == 1
    assert len(_events(db_file, budgets.VERB_BUDGET_EXHAUSTED)) == 1
    (signal,) = [s for s in counts["signals"] if s["key"] == "budget_exhaustions"]
    assert signal["count"] == 2


def test_a_claim_is_counted_and_keeping_or_ending_one_is_not(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        first, second = _issue(client, sol, "one"), _issue(client, sol, "two")
        assert _budget(client, sol, coordination_limit=1).status_code == 200
        claimed = _claim(client, sol, first)
        refused = _claim(client, sol, second)
        generation = claimed.json()["generation"]
        renewed = _claim(client, sol, first, generation=generation)
        done = client.post(
            f"/issues/{first}/complete",
            json={"generation": generation},
            headers=bearer(sol),
        )

    assert claimed.status_code == 201, claimed.text
    assert refused.status_code == 429 and refused.json()["code"] == CODE
    assert (
        _count(
            db_file, "SELECT COUNT(*) FROM issue_leases WHERE issue_id = ?", (second,)
        )
        == 0
    )
    # A renewal and a completion are the way to keep and to end what is held.
    assert renewed.status_code in (200, 201), renewed.text
    assert done.status_code == 200, done.text


def test_pickup_is_a_claim(tmp_path):
    """Pickup acquires by the same command, so it is refused the same way."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        _issue(client, sol)
        assert _budget(client, sol, coordination_limit=0).status_code == 200
        refused = client.post("/issues/claim-next", json={}, headers=bearer(sol))
        assert _budget(client, sol, coordination_limit=1).status_code == 200
        taken = client.post("/issues/claim-next", json={}, headers=bearer(sol))

    assert refused.status_code == 429 and refused.json()["code"] == CODE
    assert taken.json()["claimed"] is True, taken.text
    assert _count(db_file, "SELECT COUNT(*) FROM issue_leases") == 1


def test_a_yield_is_not_counted(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue_id = _issue(client, sol)
        assert _budget(client, sol, coordination_limit=1).status_code == 200
        generation = _claim(client, sol, issue_id).json()["generation"]
        yielded = client.post(
            f"/issues/{issue_id}/yield",
            json={
                "generation": generation,
                "reason": "capacity",
                "attempted_work": "nothing yet",
                "evidence": [],
                "blocking_question": "who takes it?",
                "resume_instructions": "start over",
            },
            headers=bearer(sol),
        )

    assert yielded.status_code == 201, yielded.text


def test_a_delegation_is_counted_once_it_is_made(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        cora = onboard_agent(client, email="cora@e.com", name="Cora Conductor")
        sol = onboard_agent(client, email="sol@e.com", name="Sol Seat")
        bo = onboard_agent(client, email="bo@e.com", name="Bo Seat")
        made = client.post("/issues", json={"title": "slice"}, headers=bearer(cora))
        issue_id = made.json()["id"]
        assert _budget(client, cora, coordination_limit=1).status_code == 200

        def delegate(to: dict):
            return client.post(
                f"/issues/{issue_id}/delegate",
                json={"user_id": to["user"]["id"]},
                headers=bearer(cora),
            )

        first, again, second = delegate(sol), delegate(sol), delegate(bo)
        read = client.get(f"/users/{cora['user']['id']}/budget", headers=ADMIN).json()

    assert first.status_code == 201, first.text
    # Repeating a delegation that stands records nothing and spends nothing.
    assert again.status_code in (200, 201), again.text
    assert second.status_code == 429 and second.json()["code"] == CODE
    assert read["coordination_used"] == 1
    assert (
        _count(
            db_file,
            "SELECT COUNT(*) FROM issue_contributors WHERE issue_id = ?",
            (issue_id,),
        )
        == 1
    )


def test_a_page_comment_is_counted_too(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, scopes=("read", "issue:write", "docs:write"))
        space = client.post(
            "/spaces", json={"name": "Notes", "key": "NOTE"}, headers=ADMIN
        ).json()
        page = client.post(
            f"/spaces/{space['id']}/pages", json={"title": "A page"}, headers=ADMIN
        ).json()
        assert _budget(client, sol, coordination_limit=1).status_code == 200
        answers = [
            client.post(
                f"/pages/{page['id']}/comments",
                json={"body": "a note"},
                headers=bearer(sol),
            )
            for _ in range(2)
        ]

    assert answers[0].status_code == 201, answers[0].text
    assert answers[1].status_code == 429 and answers[1].json()["code"] == CODE


def test_a_new_window_gives_both_counters_back(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue_id = _issue(client, sol)
        assert _budget(client, sol, coordination_limit=1).status_code == 200
        assert _comment(client, sol, issue_id).status_code == 201
        assert _comment(client, sol, issue_id).status_code == 429
        later = utc_now() + timedelta(hours=2)
        monkeypatch.setattr(budgets, "utc_now", lambda: later)
        again = _comment(client, sol, issue_id)
        read = client.get(f"/users/{sol['user']['id']}/budget", headers=ADMIN).json()

    assert again.status_code == 201, again.text
    assert (read["action_used"], read["coordination_used"]) == (0, 1)


@pytest.mark.parametrize("limit", [-1, 1.5, "many"])
def test_a_ceiling_that_is_not_a_count_is_refused(tmp_path, limit):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        refused = _budget(client, sol, coordination_limit=limit)

    assert refused.status_code == 422, refused.text


def test_setting_a_budget_without_one_removes_it(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        issue_id = _issue(client, sol)
        # Added to a budget that had none, then removed again: both are
        # replacements of a stored budget.
        assert _budget(client, sol).status_code == 200
        assert _budget(client, sol, coordination_limit=0).status_code == 200
        frozen = _comment(client, sol, issue_id)
        replaced = _budget(client, sol)
        freed = _comment(client, sol, issue_id)

    assert frozen.status_code == 429 and frozen.json()["code"] == CODE
    assert replaced.json()["coordination_limit"] is None
    assert freed.status_code == 201, freed.text
    assert _events(db_file, "agent_budget_set") == [
        "100 actions per hour",
        "100 actions per hour; 0 coordination writes per hour",
        "100 actions per hour",
    ]


def test_the_operator_sets_it_from_the_agents_page(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        login(client, "a@e.com")
        page = client.get("/admin/agents").text
        csrf = client.cookies.get("athena_csrf")
        form = {"action_limit": "10", "window": "day", "csrf_token": csrf}
        with_ceiling = client.post(
            f"/admin/agents/{sol['user']['id']}/budget",
            data={**form, "coordination_limit": "4"},
            follow_redirects=False,
        )
        one = client.get(f"/users/{sol['user']['id']}/budget", headers=ADMIN).json()
        shown = client.get("/admin/agents").text
        without = client.post(
            f"/admin/agents/{sol['user']['id']}/budget",
            data={**form, "coordination_limit": ""},
            follow_redirects=False,
        )
        two = client.get(f"/users/{sol['user']['id']}/budget", headers=ADMIN).json()
        bad = client.post(
            f"/admin/agents/{sol['user']['id']}/budget",
            data={**form, "coordination_limit": "lots"},
            follow_redirects=False,
        )

    assert 'name="coordination_limit"' in page
    assert (
        with_ceiling.status_code == 303
        and "budget_set" in with_ceiling.headers["location"]
    )
    assert (one["action_limit"], one["coordination_limit"]) == (10, 4)
    assert 'value="4"' in shown and 'value="4"' not in page
    assert without.status_code == 303
    assert two["coordination_limit"] is None
    assert bad.status_code == 303 and "error=" in bad.headers["location"]
