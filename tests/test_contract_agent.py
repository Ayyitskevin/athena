"""A scripted seat plays the documented loop and every failure branch (A16).

WHY: each loop defect the agentic-use review found (a gated close that never
applied, a pickup that handed back the work just finished, a cursor that could
pass the present, refusals with no code) passed the gate, because no test
played the agent. This one does. A seat, built from the MCP config Athena
emits for its token, works through ``build_server`` with the ``seat`` profile
over the MCP protocol, using only tools that profile carries. A second seat
and the operator act through their own clients where a scenario needs them,
and a lease lapses as it does in use: the app's clock (G6) moves past its
expiry, and nothing writes the lease row or sleeps.

The run asserts progress, not status codes:

1. ``my_desk``, ``claim_next_ready``, the work context, a comment, a completion
   report, then ``claim_next_ready`` again picks a different issue;
2. a gated close answers ``approval_required`` with ``applied: false``; the
   operator approves; the same-key retry applies and the issue is closed;
3. a ``needs_input`` yield is offered to nobody until another account answers,
   then a second seat picks it up, reads the handoff and resumes it;
4. a lease lapses, the desk lists it, and a deliberate re-claim takes a new
   generation while the old one is refused;
5. every refusal met carries a registered ``code``, and where the registry
   names a ``next`` call, following it succeeds;
6. the desk cursor advances to the last handled event and cannot pass it.

It also counts the tool calls and the bytes of every answer, and fails when
either exceeds ``tests/contract_agent_ceilings.json``, and it fails when any
answer exceeds its tool's declared response budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import (
    AppClock,
    McpAgent as Agent,
    Meter,
    lapse_lease,
    mcp_server_for,
    mcp_sessions,
    run_agents,
)

from athena.core import db, refusal_codes, token_commands, tokens
from athena.main import create_app
from athena.mcp import server as mcp_server

SEAT = mcp_server.PROFILES["seat"] or frozenset()
CEILINGS = Path(__file__).with_name("contract_agent_ceilings.json")
SEAT_SCOPES = ["read", "issue:write"]


# --- the board --------------------------------------------------------------------


@dataclass
class Board:
    app: Any
    db_file: Path
    operator: TestClient
    operator_id: int
    seats: dict[str, dict[str, Any]]  # name -> onboarding answer
    issues: dict[str, int]
    project_id: int
    clock: AppClock
    facts: dict[str, Any] = field(default_factory=dict)  # what one scenario hands on

    def ok(self, response, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    def issue(self, title: str, priority: str, *delegates: str) -> int:
        made = self.ok(
            self.operator.post(
                "/issues",
                json={
                    "title": title,
                    "project_id": self.project_id,
                    "priority": priority,
                },
            ),
            201,
        )
        for name in delegates:
            self.delegate(made["id"], name)
        self.issues[title] = made["id"]
        return made["id"]

    def delegate(self, issue_id: int, name: str) -> None:
        user_id = self.seats[name]["user"]["id"]
        self.ok(
            self.operator.post(
                f"/issues/{issue_id}/delegate", json={"user_id": user_id}
            ),
            201,
        )

    def lapse(self, issue_id: int) -> None:
        lapse_lease(self.clock, self.db_file, issue_id)

    def server_for(self, name: str) -> Any:
        """The server athena-mcp builds from the config Athena emitted for this
        seat: its token, its profile, its probed scopes, a fixed run id."""
        return mcp_server_for(self.app, self.seats[name], run_id=f"contract-{name}-run")


@pytest.fixture
def board(clock, tmp_path):
    db_file = tmp_path / "contract.db"
    app = create_app(db_file)
    with TestClient(app) as anonymous:
        admin = anonymous.post(
            "/users", json={"email": "op@e.com", "name": "Operator", "password": "pw"}
        )
        assert admin.status_code == 201, admin.text
        conn = db.connect(db_file)
        try:
            minted = token_commands.mint_token(
                conn, actor_id=1, name="operator", scopes=[tokens.ADMIN_SCOPE]
            )
        finally:
            conn.close()
        operator = TestClient(
            app, headers={"Authorization": f"Bearer {minted['token']}"}
        )
        seats = {}
        for name in ("sol", "terra"):
            onboarded = operator.post(
                "/users/onboard_agent",
                json={
                    "email": f"{name}@e.com",
                    "name": name.title(),
                    "scopes": SEAT_SCOPES,
                },
            )
            assert onboarded.status_code == 201, onboarded.text
            seats[name] = onboarded.json()
        project = operator.post("/projects", json={"name": "Contract", "key": "CON"})
        assert project.status_code == 201, project.text
        board = Board(app, db_file, operator, 1, seats, {}, project.json()["id"], clock)
        # The seat is gated on closing an issue, as athena-demo gates Sol.
        board.ok(
            operator.put(
                f"/approvals/policies/{seats['sol']['user']['id']}",
                json={"action_kind": "issue.close"},
            )
        )
        board.issue("Alpha", "high", "sol")
        board.issue("Bravo", "medium", "sol")
        board.issue("Charlie", "low", "sol", "terra")
        board.issue("Delta", "low", "sol")
        yield board


# --- the scenarios ----------------------------------------------------------------


async def pickup_complete_pickup(board: Board, sol: Agent) -> None:
    """1. Orient, pick up, read, comment, report, pick up again: new work."""
    desk = await sol.call("my_desk")
    assert desk["identity"]["id"] == sol.user_id
    ready = desk["work"]["ready"]
    assert ready["total"] == 4
    first_ready = ready["items"][0]["issue"]["id"]

    first = await sol.call("claim_next_ready", idempotency_key="s1-pickup-1")
    assert first["claimed"] is True, first
    alpha = first["issue"]["id"]
    assert alpha == board.issues["Alpha"] == first_ready  # the desk and pickup agree
    context = await sol.call("get_issue_work_context", issue_id=alpha)
    assert context["issue"]["id"] == alpha
    comment = await sol.call(
        "comment_on_issue",
        issue_id=alpha,
        body="Read the issue; the boundary holds. Reporting and moving on.",
        idempotency_key="s1-comment",
    )
    assert comment["author_id"] == sol.user_id
    released = await sol.call(
        "complete_claim",
        issue_id=alpha,
        generation=first["lease"]["generation"],
        verdict="PASS",
        results=[{"condition": "boundary re-checked", "state": "met", "note": ""}],
        unfinished=["Closing the issue is the operator's call."],
        idempotency_key="s1-complete",
    )
    assert released["released"] is True and released["issue_still_open"] is True

    second = await sol.call("claim_next_ready", idempotency_key="s1-pickup-2")
    assert second["claimed"] is True, second
    assert second["issue"]["id"] == board.issues["Bravo"] != alpha
    # Progress: the comment is on Alpha, Alpha is free, Bravo is held.
    comments = await sol.call("list_issue_comments", issue_id=alpha)
    assert [row["id"] for row in comments["items"]] == [comment["id"]]
    lease = await sol.call("get_issue_lease", issue_id=alpha)
    assert lease == {"found": False} or lease.get("active") is False, lease
    held = await sol.call("get_issue_lease", issue_id=second["issue"]["id"])
    assert held["holder_id"] == sol.user_id and held["active"] is True
    board.facts["bravo_generation"] = second["lease"]["generation"]


async def gated_close(board: Board, sol: Agent) -> None:
    """2. A gated close is an ask; approval lets the same key apply it."""
    bravo = board.issues["Bravo"]
    await sol.call(
        "complete_claim",
        issue_id=bravo,
        generation=board.facts["bravo_generation"],
        idempotency_key="s2-complete",
    )
    asked = await sol.call(
        "update_issue", issue_id=bravo, status="done", idempotency_key="s2-close"
    )
    assert asked["code"] == "approval_required" and asked["applied"] is False, asked
    assert asked["next"] == refusal_codes.next_call("approval_required")
    waiting = await sol.call(
        "update_issue", issue_id=bravo, status="done", idempotency_key="s2-close"
    )
    assert waiting["approval"]["id"] == asked["approval"]["id"]  # one ask, not two
    issue = await sol.call("get_issue", issue_id=bravo)
    assert issue["status"] != "done"

    board.ok(
        board.operator.post(
            f"/approvals/{asked['approval']['id']}/decision",
            json={"decision": "approve", "note": "reviewed"},
        )
    )
    # The `next` the ask named: wait for the decision on the gated target, from
    # the cursor the ask carries.
    approval = asked["approval"]
    feed = await sol.follow(
        asked,
        kind=approval["target_kind"],
        target=approval["target_id"],
        after=asked["events_after"],
        wait=0,
    )
    assert "approval_approved" in [event["verb"] for event in feed["events"]]
    applied = await sol.call(
        "update_issue", issue_id=bravo, status="done", idempotency_key="s2-close"
    )
    assert applied.get("applied") is not False and applied["status"] == "done"
    issue = await sol.call("get_issue", issue_id=bravo)
    assert issue["status"] == "done"


async def yield_answer_resume(board: Board, sol: Agent, terra: Agent) -> None:
    """3. A question blocks the issue for everyone until someone else answers;
    then a second seat picks it up and resumes the handoff."""
    picked = await sol.call("claim_next_ready", idempotency_key="s3-pickup")
    charlie = board.issues["Charlie"]
    assert picked["claimed"] is True and picked["issue"]["id"] == charlie, picked
    handoff = await sol.call(
        "yield_claim",
        issue_id=charlie,
        generation=picked["lease"]["generation"],
        reason="needs_input",
        attempted_work="Read the issue and its links; changed nothing.",
        evidence=["The issue names two scopes and picks neither."],
        blocking_question="Should this cover the docs pass or only the boundary?",
        resume_instructions="Answer the question, then resume and finish.",
        idempotency_key="s3-yield",
    )
    assert handoff["state"] == "awaiting_resume"

    # Unanswered, the question is offered to nobody, the asker included.
    nothing = await terra.call("claim_next_ready", idempotency_key="s3-terra-1")
    assert nothing["claimed"] is False and nothing["reason"] == "nothing_ready"
    desk = await sol.call("my_desk")
    waiting = desk["work"]["waiting_on_operator"]["not_offered"]["items"]
    assert [(row["issue"]["id"], row["reason"]) for row in waiting] == [
        (board.issues["Alpha"], "completed_by_you"),
        (charlie, "awaiting_answer"),
    ], waiting

    answer = board.ok(
        board.operator.post(
            f"/issues/{charlie}/comments", json={"body": "The boundary only."}
        ),
        201,
    )
    resumed_by = await terra.call("claim_next_ready", idempotency_key="s3-terra-2")
    assert resumed_by["claimed"] is True and resumed_by["issue"]["id"] == charlie
    context = await terra.call("get_issue_work_context", issue_id=charlie)
    # Peer-written text names its writer: the operator's answer, Sol's handoff.
    written = {row["id"]: row for row in context["comments"]["items"]}
    assert written[answer["id"]]["authored_by"] == {
        "id": board.operator_id,
        "is_agent": False,
    }
    open_handoff = resumed_by["lease"]["open_claim_handoff"] or {}
    assert open_handoff["handoff_token"] == handoff["handoff_token"]
    assert open_handoff["authored_by"] == {"id": sol.user_id, "is_agent": True}
    resumed = await terra.call(
        "resume_claim_handoff",
        issue_id=charlie,
        handoff_token=handoff["handoff_token"],
        generation=resumed_by["lease"]["generation"],
        resume_note="Covering the boundary only, as answered.",
        idempotency_key="s3-resume",
    )
    assert resumed["state"] == "resumed"
    assert resumed["resumed"]["actor_id"] == terra.user_id
    await terra.call(
        "complete_claim",
        issue_id=charlie,
        generation=resumed_by["lease"]["generation"],
        idempotency_key="s3-terra-complete",
    )
    closed = await terra.call(
        "update_issue", issue_id=charlie, status="done", idempotency_key="s3-close"
    )
    assert closed["status"] == "done"


async def lapse_and_reclaim(board: Board, sol: Agent) -> None:
    """4. A lease lapses; the desk shows it; a deliberate re-claim takes a new
    generation and the old one is refused (with a `next` that resolves it)."""
    picked = await sol.call("claim_next_ready", idempotency_key="s4-pickup")
    delta = board.issues["Delta"]
    assert picked["claimed"] is True and picked["issue"]["id"] == delta, picked
    old = picked["lease"]["generation"]
    board.lapse(delta)

    desk = await sol.call("my_desk")
    lapsed = desk["work"]["leases"]["lapsed"]
    assert [(row["issue_id"], row["generation"]) for row in lapsed] == [(delta, old)]
    issue = await sol.call("get_issue", issue_id=delta)
    retaken = await sol.call("claim_issue", issue_id=delta, if_match=issue["_etag"])
    new = retaken["generation"]
    assert new != old and retaken["active"] is True

    stale = await sol.refused(
        "lease_generation_mismatch",
        "complete_claim",
        issue_id=delta,
        generation=old,
        idempotency_key="s4-stale",
    )
    lease = await sol.follow(stale, issue_id=delta)
    assert lease["generation"] == new and lease["holder_id"] == sol.user_id
    board.facts["delta_generation"] = new


async def refusal_branches(board: Board, sol: Agent, terra: Agent) -> None:
    """5. Each refusal carries a registered code, and its `next` resolves it."""
    delta = board.issues["Delta"]
    # A held claim: Terra, eligible, meets Sol's lease. The refusal names the
    # holder and the expiry as fields, and its `next` waits on the issue from
    # the cursor it carries: with Sol idle it answers empty, not with history,
    # and Terra takes other work as the hint says (F11).
    board.delegate(delta, "terra")
    echo = board.issue("Echo", "low", "terra")
    tag = (await terra.call("get_issue", issue_id=delta))["_etag"]
    held = await terra.refused(
        "claim_held", "claim_issue", issue_id=delta, if_match=tag
    )
    assert held["holder"] == {"id": sol.user_id, "name": "Sol"}, held
    quiet = await terra.follow(
        held, kind="issue", target=delta, after=held["events_after"], wait=0
    )
    assert quiet["events"] == [], quiet
    other = await terra.call("claim_next_ready", idempotency_key="s5-terra-pickup")
    assert other["claimed"] is True and other["issue"]["id"] == echo, other

    # A repeated claim without the generation is a renewal that needs it; the
    # holder's refusal names it, so the renewal follows at once (no `next`). The
    # generation alone renews (A12): the seat does not re-read the issue's ETag.
    renew = await sol.refused(
        "lease_generation_required", "claim_issue", issue_id=delta
    )
    assert "next" not in renew, renew
    renewed = await sol.call(
        "claim_issue", issue_id=delta, generation=renew["generation"]
    )
    assert renewed["generation"] == renew["generation"] and renewed["active"]

    # A stale precondition: the issue moved after Sol read it.
    foxtrot = board.issue("Foxtrot", "low", "sol")
    stale_tag = (await sol.call("get_issue", issue_id=foxtrot))["_etag"]
    # The operator's title edit over its token names the revision (D20).
    board.ok(
        board.operator.patch(
            f"/issues/{foxtrot}",
            json={"title": "Foxtrot 2"},
            headers={"If-Match": stale_tag},
        )
    )
    moved = await sol.refused(
        "precondition_failed", "claim_issue", issue_id=foxtrot, if_match=stale_tag
    )
    fresh = await sol.follow(moved, issue_id=foxtrot)
    assert fresh["_etag"] != stale_tag and fresh["title"] == "Foxtrot 2"
    claimed = await sol.call("claim_issue", issue_id=foxtrot, if_match=fresh["_etag"])
    assert claimed["holder_id"] == sol.user_id

    # A bad argument never reaches Athena; a missing issue is not found.
    bad = await sol.refused("invalid_arguments", "claim_next_ready", limit=9)
    assert bad["status_code"] is None and "limit" in bad["detail"]
    missing = await sol.refused("not_found", "get_issue", issue_id=99_999)
    assert missing["status_code"] == 404

    # Put it down: both claims completed, Terra's too.
    await sol.call(
        "complete_claim",
        issue_id=foxtrot,
        generation=claimed["generation"],
        idempotency_key="s5-complete-foxtrot",
    )
    await sol.call(
        "complete_claim",
        issue_id=delta,
        generation=renewed["generation"],
        idempotency_key="s5-complete-delta",
    )
    await terra.call(
        "complete_claim",
        issue_id=echo,
        generation=other["lease"]["generation"],
        idempotency_key="s5-terra-complete",
    )


async def cursor_to_the_last_handled_event(sol: Agent) -> None:
    """6. The cursor records what was handled, and cannot pass the present."""
    desk = await sol.call("my_desk")
    latest = desk["signals"]["latest_visible_event_id"]
    # Handle a first batch, and record exactly that much.
    page = await sol.call("wait_for_events", after=0, wait=0, limit=10)
    handled = page["events"][-1]["id"]
    assert page["has_more"] is True and handled < latest
    await sol.call("advance_desk_cursor", after_id=handled)
    desk = await sol.call("my_desk")
    assert desk["cursor"]["after_id"] == handled
    assert desk["signals"]["events_since_cursor"] > 0

    while page["has_more"]:  # then drain the rest
        page = await sol.call(
            "wait_for_events", after=page["next_after"], wait=0, limit=200
        )
    handled = page["next_after"]
    assert handled == latest
    ahead = await sol.refused(
        "cursor_ahead", "advance_desk_cursor", after_id=latest + 1
    )
    assert ahead["latest_visible_event_id"] == latest
    await sol.follow(ahead, after_id=ahead["latest_visible_event_id"])
    desk = await sol.call("my_desk")
    assert desk["cursor"]["after_id"] == latest
    assert desk["signals"]["events_since_cursor"] == 0
    # Nor can it move back.
    await sol.refused("conflict", "advance_desk_cursor", after_id=latest - 1)


# --- the run ----------------------------------------------------------------------


async def _play(board: Board, meter: Meter) -> None:
    async with mcp_sessions(board.server_for("sol"), board.server_for("terra")) as (
        sol_session,
        terra_session,
    ):
        for session in (sol_session, terra_session):
            listed = {tool.name for tool in (await session.list_tools()).tools}
            assert listed == SEAT  # the emitted config is the seat surface
        sol = Agent("sol", board.seats["sol"]["user"]["id"], sol_session, meter, SEAT)
        terra = Agent(
            "terra", board.seats["terra"]["user"]["id"], terra_session, meter, SEAT
        )
        await pickup_complete_pickup(board, sol)
        await gated_close(board, sol)
        await yield_answer_resume(board, sol, terra)
        await lapse_and_reclaim(board, sol)
        await refusal_branches(board, sol, terra)
        await cursor_to_the_last_handled_event(sol)


def test_a_seat_plays_the_documented_loop_and_every_failure_branch(board):
    for name in board.seats:
        env = board.seats[name]["mcp_config"]["mcpServers"]["athena"]["env"]
        assert env["ATHENA_MCP_PROFILE"] == "seat"
    meter = Meter()
    run_agents(_play(board, meter))

    met = {refusal.answer["code"] for refusal in meter.refusals}
    assert {
        "claim_held",
        "precondition_failed",
        "lease_generation_mismatch",
        "lease_generation_required",
        "invalid_arguments",
        "not_found",
        "cursor_ahead",
        "conflict",
    } <= met, met
    unresolved = [
        (refusal.agent, refusal.tool, refusal.answer["code"])
        for refusal in meter.refusals
        if refusal.answer.get("next") and not refusal.followed
    ]
    assert unresolved == []
    assert meter.over_budget == []

    ceilings = json.loads(CEILINGS.read_text(encoding="utf-8"))
    measured = {"tool_calls": meter.calls, "bytes_received": meter.bytes_received}
    print(f"contract agent: {measured}")
    assert measured["tool_calls"] <= ceilings["tool_calls"], (measured, ceilings)
    assert measured["bytes_received"] <= ceilings["bytes_received"], (
        measured,
        ceilings,
    )


def test_the_ceilings_file_says_how_it_may_change():
    ceilings = json.loads(CEILINGS.read_text(encoding="utf-8"))
    assert set(ceilings) == {"note", "tool_calls", "bytes_received"}
    assert "stated reason in the same PR" in ceilings["note"]
    assert all(
        isinstance(ceilings[key], int) for key in ("tool_calls", "bytes_received")
    )
