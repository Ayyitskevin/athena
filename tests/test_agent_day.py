"""One agent's working day, held as a test (AD1).

WHY: docs/plans/reports/agent-day-2026-11-26.md is the lead running Athena as an
agent for one session: the demo's seat Sol on ``seat+knowledge`` and a
conductor Nova on ``conductor``, over MCP, against a fresh demo workspace. It
measured what the day cost: tool definitions read before the first call, a desk
that is half repeated explanation, calls made only to read a generation, and a
delegation loop whose question and answer had to be fetched by hand. That was a
scratch script. This is the same day in process and in the suite, so
docs/plans/AGENT_DAY_SPEC.md's scorecard is a number a change moves.

The day, on ``athena.demo.seed_demo`` with the clock held still:

1. Sol boots at the desk and answers the operator's unanswered steer.
2. Sol finishes the claim it holds on the command issue: reads its work
   context, heartbeats, comments, completes the claim, and asks to close it
   (gated: ``approval_required``).
3. Nova writes an outcome with a work order and one slice under it, delegated
   to Sol.
4. Sol picks the slice up, reads it, and yields it with a blocking question.
5. Nova finds the question and answers it in a comment.
6. Sol picks the slice up again, reads the answer, resumes the handoff,
   completes, and closes through an approval the operator decides.

Agents are scripted, and each takes the cheapest route the answers it has read
allow: where an answer already carries what it needs (a lease's generation, the
reply to its question, its parent's contract, a question asked of it) it uses
that, and otherwise it makes the call that fetches it. So when a requirement
lands, the day's call count drops without the script changing, and the change
lowers the ceiling in ``tests/agent_day_ceilings.json`` in the same commit.

Each of AD4 to AD8 also has a scenario that states its requirement directly,
marked ``@unmet`` until it lands, in the fleet trial's sense: it passes while
it stops at its requirement's check and fails once the check holds, so the
change that lands the requirement removes the mark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import functools
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import (
    McpAgent,
    Meter,
    client_as,
    mcp_server_for,
    mcp_sessions,
    onboard_agent,
    run_agents,
)

from athena import demo
from athena.aegis import answer_glossary, notification_groups
from athena.core import db, token_commands, users
from athena.main import create_app
from athena.mcp import server as mcp_server

CEILINGS = Path(__file__).with_name("agent_day_ceilings.json")
ROLES = ("seat", "conductor")
CALL_MEASURES = ("tool_calls", "bytes_received")
SIZE_MEASURES = (
    "listing_bytes_seat",
    "listing_bytes_seat_knowledge",
    "listing_bytes_conductor",
    "instructions_bytes",
    "desk_full_bytes",
    "work_context_full_bytes",
    "work_context_brief_bytes",
)


class RequirementUnmet(AssertionError):
    """The behaviour a requirement adds is absent; only ``@unmet`` stops on it."""


def require(condition: bool, requirement: str) -> None:
    if not condition:
        raise RequirementUnmet(requirement)


def unmet(requirement: str, why: str):
    """A scenario whose requirement does not hold yet: it passes while it stops
    at that requirement's check and fails once the check holds."""

    def mark(scenario):
        @functools.wraps(scenario)
        def test(*args: Any, **kwargs: Any) -> None:
            try:
                scenario(*args, **kwargs)
            except RequirementUnmet as gap:
                assert str(gap).startswith(f"{requirement}:"), gap
                return
            pytest.fail(
                f"{requirement} holds now ({why!r} no longer happens): remove "
                f"@unmet from {scenario.__name__} in the change that lands it"
            )

        return test

    return mark


# --- the workspace ------------------------------------------------------------------


@dataclass
class Day:
    app: Any
    seeded: dict[str, Any]
    operator: TestClient
    accounts: dict[str, dict[str, Any]]  # role -> onboarding-shaped answer
    profiles: dict[str, str] = field(
        default_factory=lambda: {"seat": "seat+knowledge", "conductor": "conductor"}
    )
    facts: dict[str, Any] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    def ok(self, response, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    def user_id(self, role: str) -> int:
        return self.accounts[role]["user"]["id"]


@pytest.fixture
def day(tmp_path, clock):
    seeded = demo.seed_demo(tmp_path / "day.db", attach_dir=tmp_path / "attachments")
    conn = db.connect(seeded["db_path"])
    try:
        operator_row = users.get_user(conn, seeded["ids"]["operator"])
        admin_token = token_commands.mint_token(
            conn, actor=operator_row, name="day-operator", scopes=["admin"]
        )["token"]
    finally:
        conn.close()
    app = create_app(Path(seeded["db_path"]))
    with TestClient(app):
        operator = client_as(app, token=admin_token)
        # Sol as the demo printed it: its own token on its printed profile.
        sol = {
            "user": {"id": seeded["ids"]["sol"]},
            "mcp_config": {
                "mcpServers": {
                    "athena": {
                        "env": {
                            "ATHENA_TOKEN": seeded["agent_token"],
                            mcp_server.PROFILE_ENV: "seat+knowledge",
                        }
                    }
                }
            },
        }
        nova = onboard_agent(
            operator,
            email="nova@athena.local",
            name="Nova Conductor",
            scopes=("read", "issue:write"),
            headers={},
            profile="conductor",
        )
        yield Day(app, seeded, operator, {"seat": sol, "conductor": nova})


class DayAgent(McpAgent):
    def __init__(self, role: str, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.role = role


async def _play(day: Day, scene) -> dict[str, DayAgent]:
    servers = {
        role: mcp_server_for(
            day.app,
            day.accounts[role],
            run_id=f"day-{role}-run",
            profile=day.profiles[role],
        )
        for role in ROLES
    }
    day.sizes["instructions_bytes"] = len(
        (servers["seat"].instructions or "").encode("utf-8")
    )
    async with mcp_sessions(*servers.values()) as sessions:
        agents = {}
        for role, session in zip(ROLES, sessions, strict=True):
            listed = (await session.list_tools()).tools
            listing = json.dumps([tool.model_dump(mode="json") for tool in listed])
            key = {"seat+knowledge": "seat_knowledge"}.get(
                day.profiles[role], day.profiles[role]
            )
            day.sizes[f"listing_bytes_{key}"] = len(listing.encode("utf-8"))
            tools = frozenset(tool.name for tool in listed)
            agents[role] = DayAgent(
                role, role, day.user_id(role), session, Meter(), tools
            )
        await scene(day, agents)
    return agents


def play(day: Day, scene) -> dict[str, DayAgent]:
    return run_agents(_play(day, scene))


async def listing_size(day: Day, role: str, profile: str) -> int:
    """The bytes of the tool listing ``role``'s account reads on ``profile``
    before its first call, measured as the day measures it."""
    server = mcp_server_for(
        day.app, day.accounts[role], run_id="day-size", profile=profile
    )
    async with mcp_sessions(server) as (session,):
        listed = (await session.list_tools()).tools
        return len(
            json.dumps([tool.model_dump(mode="json") for tool in listed]).encode()
        )


async def seat_listing_size(day: Day) -> int:
    """The plain ``seat`` profile's listing, which nobody in the day carries."""
    return await listing_size(day, "seat", "seat")


def size(answer: Any) -> int:
    """The bytes of an answer as the client receives it: compact JSON (F12)."""
    return len(json.dumps(answer, separators=(",", ":")).encode("utf-8"))


# --- what an agent does with what it read ------------------------------------------


async def held_generation(agent: DayAgent, packet: dict, issue_id: int) -> str:
    """The generation the agent holds: from the packet when it says (AD8),
    else one ``get_issue_lease`` read."""
    mine = packet.get("your_lease")
    if mine and mine.get("generation"):
        return mine["generation"]
    lease = await agent.call("get_issue_lease", issue_id=issue_id)
    return lease["generation"]


async def question_for_me(agent: DayAgent, desk: dict, issue_id: int) -> str:
    """The question waiting on the conductor: from its desk when the desk says
    (AD5), else from the attention ranking."""
    for row in desk.get("asks", {}).get("questions", {}).get("items", []):
        if row["issue"]["id"] == issue_id:
            return row["blocking_question"]
    ranking = await agent.call("get_attention_ranking")
    for row in ranking["items"]:
        if row["signal"] == "awaiting_input" and row["source_id"] == issue_id:
            return row["reason"]
    raise AssertionError("the question reached the conductor nowhere")


async def reply_to_my_question(agent: DayAgent, handoff: dict, issue_id: int) -> str:
    """The answer to the yield's question: from the handoff when it carries it
    (AD6), else by listing the issue's comments."""
    replies = handoff.get("replies") or []
    if replies:
        return replies[0]["excerpt"]
    comments = await agent.call("list_issue_comments", issue_id=issue_id)
    return comments["items"][-1]["excerpt"]


async def contract_of(agent: DayAgent, packet: dict) -> dict:
    """The acceptance criteria the slice is held to: its own work order, else
    the one it inherits (AD7), else the parent's body read by hand."""
    order = packet["work_order"]
    if order["present"]:
        return {"from": packet["issue"]["key"], "present": order["present"]}
    inherited = order.get("inherited")
    if inherited:
        return {"from": inherited["from"]["key"], "present": inherited["present"]}
    parent = packet["hierarchy"]["parent"]
    issue = await agent.call("get_issue", issue_id=parent["id"])
    present = [
        part
        for part, heading in (
            ("goal", "## Goal"),
            ("success_criteria", "## Success criteria"),
        )
        if heading in issue["body"]
    ]
    return {"from": issue["key"], "present": present}


WORK_ORDER = (
    "## Goal\nStop credential stuffing on /login.\n\n"
    "## Success criteria\n- 10 failures per account in 15 minutes lock the account\n"
    "- tests pass\n"
)


# --- the day ------------------------------------------------------------------------


async def seat_boots_and_answers_the_steer(day: Day, seat: DayAgent) -> dict:
    desk = await seat.call("my_desk")
    day.sizes["desk_full_bytes"] = size(desk)
    day.facts["first_desk"] = desk
    control = desk["asks"]["run_controls"]["items"][0]
    await seat.call(
        "acknowledge_run_control", control_id=control["id"], idempotency_key="day-ack-1"
    )
    await seat.call(
        "complete_run_control",
        control_id=control["id"],
        summary="Scope narrowed to the authorization boundary; docs pass split off.",
        idempotency_key="day-steer-1",
    )
    return desk


async def seat_finishes_its_claim(day: Day, seat: DayAgent) -> None:
    issue_id = day.seeded["ids"]["command_issue"]
    packet = await seat.call("get_issue_work_context", issue_id=issue_id)
    day.sizes["work_context_full_bytes"] = size(packet)
    day.facts["held_packet"] = packet
    await seat.call("heartbeat_agent_run")
    await seat.call(
        "comment_on_issue",
        issue_id=issue_id,
        body="Boundary checks live in the command layer; docs pass deferred.",
        idempotency_key="day-progress-1",
    )
    generation = await held_generation(seat, packet, issue_id)
    await seat.call(
        "complete_claim",
        issue_id=issue_id,
        generation=generation,
        results=[
            {
                "condition": "authorization lives in the command",
                "state": "met",
                "note": "tests pass",
            }
        ],
        verdict="PASS",
        idempotency_key="day-complete-1",
    )
    gated = await seat.call(
        "update_issue", issue_id=issue_id, status="done", idempotency_key="day-close-1"
    )
    # A gated close is an answer, not a refusal: it applied nothing and names
    # the wait.
    assert gated["code"] == "approval_required" and gated["applied"] is False, gated


async def conductor_plans_and_delegates(day: Day, conductor: DayAgent) -> None:
    await conductor.call("my_desk")
    project_id = day.seeded["ids"]["project"]
    outcome = await conductor.call(
        "create_issue",
        title="Rate-limit the public login form",
        project_id=project_id,
        priority="high",
        body=WORK_ORDER,
        idempotency_key="day-outcome-1",
    )
    slice_ = await conductor.call(
        "create_issue",
        title="Slice: lockout counter",
        project_id=project_id,
        parent_id=outcome["id"],
        delegate_ids=[day.user_id("seat")],
        idempotency_key="day-slice-1",
    )
    day.facts["outcome"] = outcome
    day.facts["slice"] = slice_


async def seat_picks_up_and_asks(day: Day, seat: DayAgent) -> None:
    picked = await seat.call("claim_next_ready", idempotency_key="day-next-1")
    assert picked["claimed"] and picked["issue"]["id"] == day.facts["slice"]["id"], (
        picked
    )
    packet = await seat.call("get_issue_work_context", issue_id=picked["issue"]["id"])
    brief = await seat.call(
        "get_issue_work_context", issue_id=picked["issue"]["id"], brief=True
    )
    day.sizes["work_context_brief_bytes"] = size(brief)
    day.facts["brief_packet"] = brief
    day.facts["slice_packet"] = packet
    day.facts["contract"] = await contract_of(seat, packet)
    generation = await held_generation(seat, packet, picked["issue"]["id"])
    await seat.call(
        "yield_claim",
        issue_id=picked["issue"]["id"],
        generation=generation,
        reason="needs_input",
        attempted_work="Read the outcome; the lockout window is ambiguous.",
        evidence=["The outcome says 15 minutes; the login throttle uses 10."],
        blocking_question="Should the lockout window be 10 or 15 minutes?",
        resume_instructions="Answer in a comment on the slice.",
        idempotency_key="day-yield-1",
    )


async def conductor_answers(day: Day, conductor: DayAgent) -> None:
    desk = await conductor.call("my_desk")
    day.facts["conductor_desk"] = desk
    slice_id = day.facts["slice"]["id"]
    question = await question_for_me(conductor, desk, slice_id)
    assert "10 or 15 minutes" in question, question
    await conductor.call(
        "comment_on_issue",
        issue_id=slice_id,
        body="15 minutes, as the outcome says; the throttle is a different control.",
        idempotency_key="day-answer-1",
    )


async def seat_resumes_and_closes(day: Day, seat: DayAgent) -> None:
    picked = await seat.call("claim_next_ready", idempotency_key="day-next-2")
    slice_id = day.facts["slice"]["id"]
    assert picked["claimed"] and picked["issue"]["id"] == slice_id, picked
    handoff = picked["lease"]["open_claim_handoff"]
    day.facts["reclaimed_handoff"] = handoff
    reply = await reply_to_my_question(seat, handoff, slice_id)
    assert reply.startswith("15 minutes"), reply
    await seat.call(
        "resume_claim_handoff",
        issue_id=slice_id,
        handoff_token=handoff["handoff_token"],
        generation=picked["lease"]["generation"],
        resume_note="Going with 15 minutes.",
        idempotency_key="day-resume-1",
    )
    await seat.call(
        "complete_claim",
        issue_id=slice_id,
        generation=picked["lease"]["generation"],
        results=[
            {
                "condition": "lockout after 10 failures in 15 minutes",
                "state": "met",
                "note": "tests pass",
            }
        ],
        verdict="PASS",
        idempotency_key="day-complete-2",
    )
    gated = await seat.call(
        "update_issue", issue_id=slice_id, status="done", idempotency_key="day-close-2"
    )
    assert gated["code"] == "approval_required" and gated["applied"] is False, gated
    day.ok(
        day.operator.post(
            f"/approvals/{gated['approval']['id']}/decision",
            json={"decision": "approve", "note": "ok"},
        )
    )
    await seat.call(
        "wait_for_events",
        after=gated["events_after"],
        wait=0,
        kind="issue",
        target=slice_id,
    )
    closed = await seat.call(
        "update_issue", issue_id=slice_id, status="done", idempotency_key="day-close-2"
    )
    assert closed["status"] == "done", closed


async def the_day(day: Day, agents: dict[str, DayAgent]) -> None:
    seat, conductor = agents["seat"], agents["conductor"]
    await seat_boots_and_answers_the_steer(day, seat)
    await seat_finishes_its_claim(day, seat)
    await conductor_plans_and_delegates(day, conductor)
    await seat_picks_up_and_asks(day, seat)
    await conductor_answers(day, conductor)
    await seat_resumes_and_closes(day, seat)


def measure(day: Day, agents: dict[str, DayAgent]) -> dict[str, Any]:
    measured: dict[str, Any] = {
        role: {
            "tool_calls": agent.meter.calls,
            "bytes_received": agent.meter.bytes_received,
        }
        for role, agent in agents.items()
    }
    measured["sizes"] = {name: day.sizes[name] for name in SIZE_MEASURES}
    return measured


# --- the day, measured --------------------------------------------------------------


def test_an_agents_day_costs_no_more_than_its_ceilings(day):
    agents = play(day, the_day)
    day.sizes["listing_bytes_seat"] = run_agents(seat_listing_size(day))

    refused = [
        (refusal.tool, refusal.answer.get("code"))
        for agent in agents.values()
        for refusal in agent.meter.refusals
    ]
    assert refused == []
    over_budget = {
        role: agent.meter.over_budget
        for role, agent in agents.items()
        if agent.meter.over_budget
    }
    assert over_budget == {}
    assert day.facts["contract"] == {
        "from": day.facts["outcome"]["key"],
        "present": ["goal", "success_criteria"],
    }

    measured = measure(day, agents)
    print(f"agent day: {measured}")
    ceilings = json.loads(CEILINGS.read_text(encoding="utf-8"))
    over = [
        f"{role}.{name}: {measured[role][name]} > {ceilings[role][name]}"
        for role in ROLES
        for name in CALL_MEASURES
        if measured[role][name] > ceilings[role][name]
    ] + [
        f"sizes.{name}: {measured['sizes'][name]} > {ceilings['sizes'][name]}"
        for name in SIZE_MEASURES
        if measured["sizes"][name] > ceilings["sizes"][name]
    ]
    assert over == [], (over, measured)


def test_the_ceilings_file_says_how_it_may_change():
    ceilings = json.loads(CEILINGS.read_text(encoding="utf-8"))
    assert set(ceilings) == {"note", *ROLES, "sizes"}
    assert "stated reason in the same PR" in ceilings["note"]
    for role in ROLES:
        assert set(ceilings[role]) == set(CALL_MEASURES), role
    assert set(ceilings["sizes"]) == set(SIZE_MEASURES)
    for group in (*ROLES, "sizes"):
        assert all(isinstance(value, int) for value in ceilings[group].values())


# --- the requirements the day has met -----------------------------------------------

# AD3: the most tool-listing bytes each profile may cost before its first call,
# docs/plans/AGENT_DAY_SPEC.md's targets. The ceilings file holds the measured
# values, which may only go down; these hold the requirement itself, so a
# ceiling raised with a stated reason still cannot pass a target.
AD3_LISTING_TARGETS = {
    ("seat", "seat"): 34_000,
    ("seat", "seat+knowledge"): 45_000,
    ("conductor", "conductor"): 48_000,
}


def test_ad3_each_profiles_listing_is_within_its_target(day):
    measured = {
        profile: (run_agents(listing_size(day, role, profile)), target)
        for (role, profile), target in AD3_LISTING_TARGETS.items()
    }
    over = {
        profile: f"{size} > {target}"
        for profile, (size, target) in measured.items()
        if size > target
    }
    assert over == {}, measured


# AD2: the most bytes the desk and the work context an MCP session reads by
# default may cost at the demo seed, the specification's targets. Measured as
# the day measures them (Sol's first desk, the packet of the issue it holds).
AD2_TARGETS = {"desk_full_bytes": 4_300, "work_context_full_bytes": 5_500}


def test_ad2_the_desk_and_the_work_context_are_within_their_targets(day):
    play(day, the_day)
    measured = {name: day.sizes[name] for name in AD2_TARGETS}
    over = {
        name: f"{measured[name]} > {target}"
        for name, target in AD2_TARGETS.items()
        if measured[name] > target
    }
    assert over == {}, measured


def test_ad2_explain_answers_the_full_answer_and_the_default_is_its_lean_shape(day):
    """explain=true answers what my_desk and get_issue_work_context answered
    before AD2, the REST default; the default answer is exactly that answer's
    lean shape (answer_glossary.lean), named by the lean schema."""
    issue_id = day.seeded["ids"]["command_issue"]
    reads = {
        "desk": ("my_desk", {}, "/desk"),
        "packet": (
            "get_issue_work_context",
            {"issue_id": issue_id},
            f"/issues/{issue_id}/work-context",
        ),
        "brief": (
            "get_issue_work_context",
            {"issue_id": issue_id, "brief": True},
            f"/issues/{issue_id}/work-context?brief=true",
        ),
    }

    async def scene(day: Day, agents: dict[str, DayAgent]) -> None:
        seat = agents["seat"]
        for name, (tool, arguments, _route) in reads.items():
            day.facts[name] = (
                await seat.call(tool, explain=True, **arguments),
                await seat.call(tool, **arguments),
            )

    play(day, scene)
    seat_rest = client_as(day.app, token=day.seeded["agent_token"])
    for name, (_tool, _arguments, route) in reads.items():
        explained, default = day.facts[name]
        response = seat_rest.get(route)
        rest = day.ok(response)
        if "ETag" in response.headers:
            assert explained.pop("_etag") == response.headers["ETag"], name
            assert default.pop("_etag") == response.headers["ETag"], name
        assert explained == rest, name
        shape = answer_glossary.SHAPES[explained["schema"]]
        assert default["schema"] == shape.lean_schema, name
        if name == "desk":
            # The MCP desk also folds its notification preview one row per
            # target (AD12), as GET /desk?group=issue does.
            signals = explained["signals"]
            explained = {
                **explained,
                "signals": {
                    **signals,
                    "notifications": notification_groups.group_rows(
                        signals["notifications"]
                    ),
                },
            }
        assert default == answer_glossary.lean(explained), name
    assert day.facts["desk"][0]["schema"] == "athena.agent_desk.v2"
    assert day.facts["desk"][1]["schema"] == "athena.agent_desk.v3"


# --- the requirements the day is waiting on -----------------------------------------


def test_ad4_the_desk_names_the_first_move(day):
    play(day, the_day)
    first = (day.facts["first_desk"].get("next") or [None])[0]
    require(
        first is not None and first.get("tool") == "acknowledge_run_control",
        "AD4: Sol's first desk opens with the unanswered steer, by its call",
    )


def test_ad5_a_delegates_question_is_on_the_delegators_desk(day):
    """AD5: the question Sol asked on the slice Nova created and delegated is on
    Nova's desk, by its asker, with the call that answers it; Sol's own desk
    does not list it as asked of Sol."""
    play(day, the_day)
    lane = day.facts["conductor_desk"]["asks"]["questions"]
    slice_id = day.facts["slice"]["id"]
    (row,) = lane["items"]
    assert lane["total"] == 1 and row["issue"]["id"] == slice_id
    assert row["asked_by"]["id"] == day.user_id("seat")
    assert row["blocking_question"] == "Should the lockout window be 10 or 15 minutes?"
    assert row["authored_by"] == {"id": day.user_id("seat"), "is_agent": True}
    assert row["next"] == {
        "tool": "comment_on_issue",
        "arguments": {"issue_id": slice_id},
    }
    assert day.facts["first_desk"]["asks"]["questions"]["items"] == []


def test_ad6_the_reply_comes_back_with_the_handoff(day):
    """AD6: the open handoff Sol reclaims carries Nova's reply, by its author,
    and nothing Sol wrote itself."""
    play(day, the_day)
    handoff = day.facts["reclaimed_handoff"]
    (reply,) = handoff["replies"]
    assert handoff["replies_total"] == 1
    assert reply["excerpt"].startswith("15 minutes"), reply
    assert reply["author"]["id"] == day.user_id("conductor")
    assert reply["authored_by"] == {"id": day.user_id("conductor"), "is_agent": True}


def test_ad7_a_slice_shows_its_parents_contract(day):
    """AD7: the slice's packet, full and brief, shows the outcome's goal and
    success criteria with their text and who wrote them."""
    play(day, the_day)
    for packet in (day.facts["slice_packet"], day.facts["brief_packet"]):
        inherited = packet["work_order"]["inherited"]
        assert inherited["from"]["key"] == day.facts["outcome"]["key"]
        assert inherited["present"] == ["goal", "success_criteria"]
        assert (
            inherited["parts"]["goal"]["text"] == "Stop credential stuffing on /login."
        )
        assert inherited["parts"]["success_criteria"]["text"].startswith(
            "- 10 failures"
        )
        assert inherited["authored_by"] == {
            "id": day.user_id("conductor"),
            "is_agent": True,
        }


def test_ad8_the_packet_knows_its_reader_holds_the_lease(day):
    """AD8: the work context of an issue the reader holds carries the lease's
    generation (and says so in how_to_claim); nobody else's packet does."""
    play(day, the_day)
    packet = day.facts["held_packet"]
    mine = packet["your_lease"]
    assert len(mine["generation"]) == 32 and mine["until"].endswith("Z"), mine
    # Lean (AD2): a null key is left out, so no open handoff reads as absent.
    assert "open_handoff_token" not in mine
    assert packet["how_to_claim"]["claimant_prerequisite"]["next_operation"].startswith(
        "you hold it"
    )
    # The brief packet carries it too.
    assert (
        day.facts["brief_packet"]["your_lease"]["generation"]
        == (day.facts["slice_packet"]["your_lease"]["generation"])
    )
