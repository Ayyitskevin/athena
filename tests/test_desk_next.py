"""The desk names its first moves (AD4).

WHY: in docs/plans/reports/agent-day-2026-11-26.md (DAY-2) Sol's first desk held
an unanswered steer, a claim near expiry and a pending approval in three
sections among 7.8 KB, and ranked none of them. The desk now opens with
`next`: at most five moves from the same snapshot, each {why, tool,
arguments}, in the fixed order docs/DESK.md states.

Each state is seeded alone in its own workspace; the test asserts the first
move, then makes its call over MCP, as the seat, and asserts it succeeds.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    bearer,
    bootstrap,
    lapse_lease,
    make_app,
    mcp_server_for,
    mcp_sessions,
    onboard_agent,
    run_agents,
)

#: What a call needs beyond the arguments a move names: the text a comment
#: carries, and a wait that returns at once.
EXTRA = {
    "comment_on_issue": {"body": "Use fixture B."},
    "wait_for_events": {"wait": 0},
    "complete_run_control": {"summary": "switched to approach B"},
}


def _issue(client, title, **fields):
    made = client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _claim(client, agent, issue_id, **body):
    tag = client.get(f"/issues/{issue_id}", headers=bearer(agent)).headers["ETag"]
    held = client.post(
        f"/issues/{issue_id}/claim",
        json=body,
        headers={**bearer(agent), "If-Match": tag},
    )
    assert held.status_code == 201, held.text
    return held.json()["generation"]


def _seed_nothing(client, sol, rex, clock, db_file):
    return "wait_for_events"


def _seed_ready(client, sol, rex, clock, db_file):
    issue_id = _issue(client, "Ready for Sol")
    delegated = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": sol["user"]["id"]},
        headers=ADMIN,
    )
    assert delegated.status_code == 201, delegated.text
    return "claim_next_ready"


def _seed_run_control(client, sol, rex, clock, db_file):
    beat = client.put(
        "/agent-runs/heartbeat", json={"run_id": "steered"}, headers=bearer(sol)
    )
    assert beat.status_code == 200, beat.text
    made = client.post(
        "/run-controls",
        json={"run_id": "steered", "kind": "steer", "payload": "use approach B"},
        headers=ADMIN,
    )
    assert made.status_code == 201, made.text
    return "acknowledge_run_control"


def _seed_acknowledged_run_control(client, sol, rex, clock, db_file):
    # WHY: the dogfood found the desk still naming acknowledge_run_control for a
    # steer Sol had acknowledged, a loop: an acknowledgement is a receipt, so the
    # move that settles it is complete_run_control.
    _seed_run_control(client, sol, rex, clock, db_file)
    (control,) = client.get(
        "/run-controls", params={"run_id": "steered"}, headers=bearer(sol)
    ).json()
    acknowledged = client.post(
        f"/run-controls/{control['id']}/acknowledge", json={}, headers=bearer(sol)
    )
    assert acknowledged.status_code == 200, acknowledged.text
    return "complete_run_control"


def _seed_expiring_lease(client, sol, rex, clock, db_file):
    issue_id = _issue(client, "Short lease", assignee_id=sol["user"]["id"])
    _claim(client, sol, issue_id, lease_seconds=300)
    return "claim_issue"


def _seed_lapsed_lease(client, sol, rex, clock, db_file):
    issue_id = _issue(client, "Lapsed lease", assignee_id=sol["user"]["id"])
    _claim(client, sol, issue_id)
    lapse_lease(clock, db_file, issue_id)
    return "complete_claim"


def _seed_question(client, sol, rex, clock, db_file):
    made = client.post("/issues", json={"title": "Sol's slice"}, headers=bearer(sol))
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    delegated = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": rex["user"]["id"]},
        headers=bearer(sol),
    )
    assert delegated.status_code == 201, delegated.text
    generation = _claim(client, rex, issue_id)
    yielded = client.post(
        f"/issues/{issue_id}/yield",
        json={
            "generation": generation,
            "reason": "needs_input",
            "attempted_work": "tried fixture A",
            "evidence": ["pytest -q: 2 failed"],
            "blocking_question": "A or B?",
            "resume_instructions": "rerun with the chosen fixture",
        },
        headers=bearer(rex),
    )
    assert yielded.status_code == 201, yielded.text
    return "comment_on_issue"


SCENARIOS = [
    _seed_nothing,
    _seed_ready,
    _seed_run_control,
    _seed_acknowledged_run_control,
    _seed_expiring_lease,
    _seed_lapsed_lease,
    _seed_question,
]


@pytest.mark.parametrize("seed", SCENARIOS, ids=lambda seed: seed.__name__[6:])
def test_the_first_move_is_the_states_and_its_call_succeeds(seed, clock, tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        rex = onboard_agent(client, email="rex@e.com", name="Rex")
        tool = seed(client, sol, rex, clock, db_file)
        desk = client.get("/desk", headers=bearer(sol)).json()
        assert list(desk)[:2] == ["schema", "next"]
        moves = desk["next"]
        assert 1 <= len(moves) <= 5
        first = moves[0]
        assert first["tool"] == tool, moves
        assert first["why"]

        async def call() -> Any:
            server = mcp_server_for(app, sol, run_id="steered", profile="seat")
            async with mcp_sessions(server) as (session,):
                return await session.call_tool(
                    tool, {**first["arguments"], **EXTRA.get(tool, {})}
                )

        result = run_agents(call())
        (block,) = result.content
        assert not result.isError, json.loads(block.text)


def test_the_order_is_the_fixed_rule(clock, tmp_path):
    """A steer outranks a lapsed lease, which outranks ready work."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        rex = onboard_agent(client, email="rex@e.com", name="Rex")
        _seed_ready(client, sol, rex, clock, db_file)
        _seed_lapsed_lease(client, sol, rex, clock, db_file)
        _seed_run_control(client, sol, rex, clock, db_file)
        desk = client.get("/desk", headers=bearer(sol)).json()
        tools = [move["tool"] for move in desk["next"]]
        assert tools.index("acknowledge_run_control") < tools.index("complete_claim")
        assert tools.index("complete_claim") < tools.index("claim_next_ready")
        assert "wait_for_events" not in tools


def test_a_question_only_its_asker_could_answer_reaches_the_operator(clock, tmp_path):
    """WHY: the dogfood's seat created and assigned itself an issue, then yielded
    with a blocking question. AD5 lists a question on the desks of the accounts
    that created, delegated or are assigned the issue, other than the asker:
    here there were none, so the question reached no desk while the attention
    ranking called it critical. The operator's desk now carries it; a question
    another account can answer stays off it."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        rex = onboard_agent(client, email="rex@e.com", name="Rex")
        _seed_question(client, sol, rex, clock, db_file)  # Sol can answer it
        made = client.post(
            "/issues",
            json={"title": "Sol's own", "assignee_id": sol["user"]["id"]},
            headers=bearer(sol),
        )
        assert made.status_code == 201, made.text
        issue_id = made.json()["id"]
        generation = _claim(client, sol, issue_id)
        yielded = client.post(
            f"/issues/{issue_id}/yield",
            json={
                "generation": generation,
                "reason": "needs_input",
                "attempted_work": "read the spec",
                "evidence": ["spec section 3 is ambiguous"],
                "blocking_question": "Which section wins?",
                "resume_instructions": "apply the chosen section",
            },
            headers=bearer(sol),
        )
        assert yielded.status_code == 201, yielded.text

        operator = client.get("/desk", headers=ADMIN).json()
        asked = [item["issue"]["id"] for item in operator["asks"]["questions"]["items"]]
        assert asked == [issue_id]
        assert {
            "why": f"a question asked of you on issue #{issue_id}",
            "tool": "comment_on_issue",
            "arguments": {"issue_id": issue_id},
        } in operator["next"]
        # A non-admin account that neither created nor holds it still sees none.
        rex_desk = client.get("/desk", headers=bearer(rex)).json()
        assert rex_desk["asks"]["questions"]["items"] == []


def test_a_creator_refused_a_claim_is_told_what_to_do_next(clock, tmp_path):
    """WHY: the dogfood's seat created an issue assigned to nobody and could not
    claim it: a 403 with no next, and a seat has no tool that makes itself a
    claimant. The permission is unchanged (creating an issue is not being handed
    it); the refusal now names the move that asks for it, on REST and MCP."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        made = client.post("/issues", json={"title": "Unassigned"}, headers=bearer(sol))
        assert made.status_code == 201, made.text
        issue_id = made.json()["id"]
        tag = client.get(f"/issues/{issue_id}", headers=bearer(sol)).headers["ETag"]
        refused = client.post(
            f"/issues/{issue_id}/claim",
            json={},
            headers={**bearer(sol), "If-Match": tag},
        )
        assert refused.status_code == 403, refused.text
        body = refused.json()
        assert body["code"] == "forbidden"
        assert body["next"]["tool"] == "comment_on_issue"
        assert body["next"]["arguments"] == {"issue_id": issue_id}

        async def call() -> Any:
            server = mcp_server_for(app, sol, run_id="steered", profile="seat")
            async with mcp_sessions(server) as (session,):
                return await session.call_tool("claim_issue", {"issue_id": issue_id})

        result = run_agents(call())
        (block,) = result.content
        assert result.isError
        assert json.loads(block.text)["next"] == body["next"]
