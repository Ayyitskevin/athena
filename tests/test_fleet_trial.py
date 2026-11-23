"""The fleet trial: a conductor and three seats follow the conductor recipe (F1).

WHY: the contract agent (tests/test_contract_agent.py, A16) holds one seat to
the documented loop. Nothing held the recipe a fleet follows: a reviewer ran it
once as a scratch script against a live demo, counted what it cost, and nothing
re-ran the count. This test is that trial, in process and in the suite. One
conductor on the ``conductor`` profile and three seats on ``seat``, each built
from the MCP config Athena emits for its token, work over the MCP protocol with
only the tools their profile carries; the operator acts over REST. Each agent
follows docs/CONDUCTOR_RECIPE.md literally, as an agent that read only that
document and the tool descriptions would:

1. the conductor reads its desk and the project, creates an outcome and three
   slices (design, build, review), parents, assigns, delegates and links them;
2. the design seat picks up its slice, yields with a question, the operator
   answers, and the seat picks it up again, resumes the handoff and completes;
3. the build seat claims with its paths and submits a frozen candidate; the
   conductor requests a review; the reviewer reports HOLD; the builder reworks
   and submits a replacement; the reviewer reports PASS on the new candidate;
4. the builder's close is gated behind an approval the operator decides, and
   the conductor closes the review slice and the outcome;
5. every agent puts down: acknowledges what it handled and records its cursor.

It asserts all four issues end done and no call was refused, and holds tool
calls, bytes received and single-notification acknowledgements, per role and
in total, to ``tests/fleet_trial_ceilings.json``. Agents are scripted: a model
spends more, and the ceilings say only what the recipe costs followed exactly.
Like the contract agent it counts and never times.

An agent that cannot take the recipe's next step from what it reads stops (it
raises ``Stalled``), and the trial still reads the four issues: skip a step,
such as the operator's answer, and it is the "all done" assertion that fails.

One scenario holds a requirement that has landed: F2 (after the reviewer's
HOLD and a second request, the reviewer's desk and pickup agree).

Three more scenarios state behaviour the fleet needs, each marked ``@unmet``
until its requirement in docs/plans/ATHENA_IN_USE_SPEC.md lands: C6 (disjoint
path fences on an instance with no root catalog, and pickup with paths), C4 (a
lapsed lease is an event the conductor receives) and C5 (what the conductor
gates on is what the requested reviewer reported). C6 and C5 have landed: their
marks are gone, and the recipe above gates on C5's field.
A marked scenario must stop by raising ``RequirementUnmet`` at the check that
states its requirement, and its test passes when it does; any other failure,
such as a refused setup step, is a real failure. When the requirement lands the
scenario runs clean, the mark turns that into a failure, and the mark is
removed in the same change. This is not ``xfail``: the coverage gate accepts no
skipped or expected-failure outcome, so every test here passes.
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
    bootstrap,
    client_as,
    lapse_lease,
    make_app,
    mcp_server_for,
    mcp_sessions,
    mint_token,
    onboard_agent,
    run_agents,
)

from athena import config
from athena.mcp import server as mcp_server

CEILINGS = Path(__file__).with_name("fleet_trial_ceilings.json")
SEAT_SCOPES = ["read", "issue:write"]
# Role -> (account, MCP profile). The conductor's account is onboarded like any
# agent; the operator sets ATHENA_MCP_PROFILE=conductor for it, as the recipe says.
CAST = {
    "conductor": ("cora", "conductor"),
    "design": ("ada", "seat"),
    "build": ("bo", "seat"),
    "review": ("rex", "seat"),
}
ROLES = tuple(CAST)
MEASURES = ("tool_calls", "bytes_received", "single_acks")
REVISION_1 = "1" * 40
REVISION_2 = "2" * 40
BUILD_PATHS = ["src/export/widget.py"]


class Stalled(Exception):
    """An agent cannot take the recipe's next step from what it has read."""


class RequirementUnmet(AssertionError):
    """The behaviour a later requirement adds is absent. The ``@unmet`` scenarios
    stop with this and nothing else, so a mistake in a scenario's setup is not
    mistaken for the missing behaviour."""


def require(condition: bool, requirement: str) -> None:
    if not condition:
        raise RequirementUnmet(requirement)


def unmet(requirement: str, why: str):
    """Mark a scenario that states what ``requirement`` adds and does not hold yet.

    The test passes while the scenario stops at a check of that requirement, and
    fails once the scenario runs clean: the change that lands the requirement
    removes the mark. ``why`` says what happens today."""

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


# --- the cast ---------------------------------------------------------------------


class FleetAgent(McpAgent):
    """A scripted agent in the trial. A refused call or a missing precondition
    stops it, and the trial records where."""

    def __init__(self, role: str, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.role = role
        self.cursor = 0  # the last event this agent has read and handled

    async def call(self, tool: str, **arguments: Any) -> dict:
        ok, answer = await self.attempt(tool, **arguments)
        if not ok:
            raise Stalled(
                f"{self.role}: {tool} refused with {answer.get('code')}: "
                f"{answer.get('detail')}"
            )
        return answer

    def expect(self, condition: bool, what: str) -> None:
        if not condition:
            raise Stalled(f"{self.role}: {what}")

    async def start(self) -> dict:
        """Start at the desk, reading the feed from the present."""
        desk = await self.call("my_desk")
        self.cursor = desk["signals"]["latest_visible_event_id"]
        return desk

    async def catch_up(self) -> list[dict]:
        """Drain the unfiltered feed after the processed cursor without waiting,
        following ``has_more``; everything read is handled (the recipe's
        "Observe and acknowledge only processed work")."""
        events: list[dict] = []
        while True:
            page = await self.call("wait_for_events", after=self.cursor, wait=0)
            events.extend(page["events"])
            self.cursor = page["next_after"]
            if not page["has_more"]:
                return events

    async def acknowledge(self, desk: dict) -> None:
        """Acknowledge each unread notification, one call each: the desk previews
        some, and a list read finds the rest."""
        signals = desk["signals"]
        unread = [row["id"] for row in signals["notifications"] if not row["read_at"]]
        if signals["unread_notifications"] > len(unread):
            listed = await self.call("list_notifications", unread=True, limit=100)
            unread = [row["id"] for row in listed["items"]]
        for notification_id in unread:
            await self.call("mark_notification_read", notification_id=notification_id)

    async def put_down(self) -> None:
        """Before ending: acknowledge what was handled, then record the cursor
        through the prefix actually read."""
        await self.acknowledge(await self.call("my_desk"))
        await self.catch_up()
        await self.call("advance_desk_cursor", after_id=self.cursor)


# --- the board --------------------------------------------------------------------


@dataclass
class Fleet:
    app: Any
    db_file: Path
    operator: TestClient
    accounts: dict[str, dict[str, Any]]  # role -> onboarding answer
    project_id: int
    cast: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(CAST))
    issues: dict[str, int] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    def ok(self, response, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    def user_id(self, role: str) -> int:
        return self.accounts[role]["user"]["id"]

    def onboard(self, role: str, name: str, profile: str = "seat") -> None:
        self.cast[role] = (name, profile)
        self.accounts[role] = onboard_agent(
            self.operator,
            email=f"{name}@e.com",
            name=name.title(),
            scopes=SEAT_SCOPES,
            headers={},
        )

    def issue(self, title: str, *, assignee: str, delegate: str) -> int:
        """An issue the operator files, as a scenario's setup: assigned to one
        account and delegated to another."""
        made = self.ok(
            self.operator.post(
                "/issues", json={"title": title, "project_id": self.project_id}
            ),
            201,
        )
        self.ok(
            self.operator.put(
                f"/issues/{made['id']}/assignee",
                json={"assignee_id": self.user_id(assignee)},
            )
        )
        self.ok(
            self.operator.post(
                f"/issues/{made['id']}/delegate",
                json={"user_id": self.user_id(delegate)},
            ),
            201,
        )
        self.issues[title] = made["id"]
        return made["id"]

    def status(self, issue_id: int) -> str:
        return self.ok(self.operator.get(f"/issues/{issue_id}"))["status"]


@pytest.fixture
def fleet(tmp_path):
    app, db_file = make_app(tmp_path, "fleet.db")
    with TestClient(app) as anonymous:
        bootstrap(anonymous, email="op@e.com", name="Operator")
        operator = client_as(app, token=mint_token(anonymous, scopes=["admin"]))
        project = operator.post("/projects", json={"name": "Fleet", "key": "FLT"})
        assert project.status_code == 201, project.text
        board = Fleet(app, db_file, operator, {}, project.json()["id"])
        for role, (name, profile) in CAST.items():
            board.onboard(role, name, profile)
        # The builder is gated on closing an issue, as athena-demo gates Sol.
        board.ok(
            operator.put(
                f"/approvals/policies/{board.user_id('build')}",
                json={"action_kind": "issue.close"},
            )
        )
        yield board


async def _play(fleet: Fleet, roles: tuple[str, ...], scene) -> dict[str, FleetAgent]:
    """Connect an agent per role, each on its profile, and play ``scene``."""
    servers = [
        mcp_server_for(
            fleet.app,
            fleet.accounts[role],
            run_id=f"fleet-{fleet.cast[role][0]}-run",
            profile=fleet.cast[role][1],
        )
        for role in roles
    ]
    async with mcp_sessions(*servers) as sessions:
        agents = {}
        for role, session in zip(roles, sessions, strict=True):
            name, profile = fleet.cast[role]
            tools = mcp_server.PROFILES[profile] or frozenset()
            listed = {tool.name for tool in (await session.list_tools()).tools}
            assert listed == tools, role  # the session is the profile's surface
            agents[role] = FleetAgent(
                role, name, fleet.user_id(role), session, Meter(), tools
            )
        await scene(fleet, agents)
    return agents


def play(fleet: Fleet, scene, roles: tuple[str, ...] = ROLES) -> dict[str, FleetAgent]:
    return run_agents(_play(fleet, roles, scene))


# --- the trial: the recipe, one outcome, three slices -------------------------------


async def plan_and_delegate(fleet: Fleet, conductor: FleetAgent, workers) -> None:
    """Record intent and delegate slices: create, parent, assign, delegate, link."""
    project_id = fleet.project_id
    await conductor.start()
    await conductor.call("current_run")
    await conductor.call("get_project_brief", project_id=project_id)
    outcome = await conductor.call(
        "create_issue",
        title="Outcome: widget export",
        project_id=project_id,
        body="Acceptance: the export contract is agreed, and its implementation "
        "is reviewed PASS by a non-author.",
    )
    fleet.issues["outcome"] = outcome["id"]
    for role, title in (
        ("design", "Design: export contract"),
        ("build", "Build: export"),
        ("review", "Review: export candidate"),
    ):
        made = await conductor.call(
            "create_issue",
            title=title,
            project_id=project_id,
            body=f"Slice of {outcome['key']}.",
        )
        fleet.issues[role] = made["id"]
    for role in ("design", "build", "review"):
        slice_id = fleet.issues[role]
        await conductor.call(
            "set_issue_parent", issue_id=slice_id, parent_id=outcome["id"]
        )
        context = await conductor.call("get_issue_work_context", issue_id=slice_id)
        await conductor.call(
            "assign_issue",
            issue_id=slice_id,
            assignee_id=conductor.user_id,
            if_match=context["issue_etag"],
        )
        await conductor.call(
            "delegate_issue", issue_id=slice_id, agent_user_id=workers[role].user_id
        )
    for later, earlier in (("build", "design"), ("review", "build")):
        await conductor.call(
            "link_issues",
            issue_id=fleet.issues[later],
            target_ref=str(fleet.issues[earlier]),
            relation="blocked_by",
        )
    children = await conductor.call("list_subtasks", issue_id=outcome["id"])
    conductor.expect(children["count"] == 3, f"three slices, not {children}")
    context = await conductor.call("get_issue_work_context", issue_id=outcome["id"])
    await conductor.call(
        "assign_issue",
        issue_id=outcome["id"],
        assignee_id=conductor.user_id,
        if_match=context["issue_etag"],
    )


async def design_asks(fleet: Fleet, design: FleetAgent) -> None:
    """The architect picks up its slice and yields with a question."""
    issue_id = fleet.issues["design"]
    await design.start()
    picked = await design.call("claim_next_ready", idempotency_key="design-pickup-1")
    design.expect(
        picked["claimed"] is True and picked["issue"]["id"] == issue_id,
        f"its slice was not picked up: {picked}",
    )
    await design.call("get_issue_work_context", issue_id=issue_id)
    run = await design.call("current_run")
    await design.call("heartbeat_agent_run", run_id=run["run_id"])
    await design.call(
        "comment_on_issue",
        issue_id=issue_id,
        body="Draft contract: CSV with a header row. One open point.",
    )
    await design.call(
        "yield_claim",
        issue_id=issue_id,
        generation=picked["lease"]["generation"],
        reason="needs_input",
        attempted_work="Drafted the export contract; stopped at the encoding.",
        evidence=["The draft is the first comment on this issue."],
        blocking_question="UTF-8 with a byte-order mark, or without?",
        resume_instructions="Answer the encoding question in a comment, then I "
        "claim and resume.",
    )


async def conductor_sees_the_question(fleet: Fleet, conductor: FleetAgent) -> None:
    await conductor.catch_up()
    brief = await conductor.call("get_project_brief", project_id=fleet.project_id)
    conductor.expect(
        brief["open_handoffs"]["visible_total"] == 1,
        f"the question is not in the brief: {brief['open_handoffs']}",
    )


def operator_answers(fleet: Fleet) -> None:
    """The operator reads the open question from the brief and answers it."""
    brief = fleet.ok(fleet.operator.get(f"/projects/{fleet.project_id}/brief"))
    (handoff,) = brief["open_handoffs"]["items"]
    assert handoff["issue"]["id"] == fleet.issues["design"], handoff
    fleet.ok(
        fleet.operator.post(
            f"/issues/{fleet.issues['design']}/comments",
            json={"body": "Operator: UTF-8 without a byte-order mark."},
        ),
        201,
    )


async def design_resumes(fleet: Fleet, design: FleetAgent) -> None:
    """Answered, the slice is offered again; the architect resumes and completes."""
    issue_id = fleet.issues["design"]
    await design.catch_up()
    await design.call("my_desk")
    picked = await design.call("claim_next_ready", idempotency_key="design-pickup-2")
    design.expect(
        picked["claimed"] is True and picked["issue"]["id"] == issue_id,
        f"the answered slice was not offered back: {picked}",
    )
    generation = picked["lease"]["generation"]
    await design.call("get_issue_work_context", issue_id=issue_id)
    handoff = picked["lease"].get("open_claim_handoff")
    design.expect(bool(handoff), "the pickup returned no open handoff to resume")
    await design.call(
        "resume_claim_handoff",
        issue_id=issue_id,
        handoff_token=handoff["handoff_token"],
        generation=generation,
    )
    await design.call(
        "comment_on_issue",
        issue_id=issue_id,
        body="Contract final: CSV, header row, UTF-8 without a byte-order mark.",
    )
    await design.call(
        "complete_claim",
        issue_id=issue_id,
        generation=generation,
        verdict="PASS",
        results=[
            {
                "condition": "export contract agreed",
                "state": "met",
                "note": "the operator answered the encoding",
            }
        ],
        evidence=[
            {
                "kind": "doc",
                "source": "athena",
                "ref": f"issue {issue_id} comments",
                "scope": "synthetic",
            }
        ],
    )
    await design.put_down()


async def conductor_closes_design(fleet: Fleet, conductor: FleetAgent) -> None:
    """The conductor checks the evidence before it changes the status."""
    issue_id = fleet.issues["design"]
    await conductor.catch_up()
    await conductor.call("get_issue_work_context", issue_id=issue_id)
    closed = await conductor.call("update_issue", issue_id=issue_id, status="done")
    conductor.expect(closed.get("status") == "done", f"design not closed: {closed}")


async def build_submits(
    fleet: Fleet, build: FleetAgent, *, revision: str, rework: bool
) -> None:
    """The builder claims with its paths and submits a frozen candidate."""
    issue_id = fleet.issues["build"]
    if rework:
        await build.catch_up()
        await build.call("my_desk")
    else:
        await build.start()
    context = await build.call("get_issue_work_context", issue_id=issue_id)
    previous = None
    if rework:
        tracking = context.get("review_tracking") or {}
        current = (tracking.get("current_candidate") or {}).get("candidate") or {}
        previous = current.get("candidate_id")
        build.expect(
            previous == fleet.facts["candidates"][-1],
            f"the held candidate is not current: {previous}",
        )
    else:
        await build.call("get_issue_lease", issue_id=issue_id)
    claim = await build.call(
        "claim_issue",
        issue_id=issue_id,
        if_match=context["issue_etag"],
        paths=BUILD_PATHS,
    )
    if not rework:
        run = await build.call("current_run")
        await build.call("heartbeat_agent_run", run_id=run["run_id"])
    await build.call(
        "comment_on_issue",
        issue_id=issue_id,
        body="Fixed: the header row is written for an empty result; 13 tests pass."
        if rework
        else "Implemented the export to the design contract; 12 tests pass.",
    )
    done = await build.call(
        "complete_claim",
        issue_id=issue_id,
        generation=claim["generation"],
        verdict="PASS",
        results=[
            {
                "condition": "empty result writes the header"
                if rework
                else "export implemented",
                "state": "met",
                "note": "13 tests" if rework else "12 tests",
            }
        ],
        candidate_submission={
            "identity": {"kind": "git", "source": "fleet/widget", "revision": revision},
            "expected_previous_candidate_id": previous,
        },
    )
    fleet.facts.setdefault("candidates", []).append(
        done["candidate_submission"]["candidate_id"]
    )


async def conductor_requests_review(fleet: Fleet, conductor: FleetAgent) -> None:
    """The conductor learns of the candidate and asks the reviewer for it."""
    candidate = fleet.facts["candidates"][-1]
    await conductor.catch_up()
    queue = await conductor.call(
        "get_project_review_queue", project_id=fleet.project_id
    )
    current = [row["candidate"]["candidate_id"] for row in queue["items"]]
    conductor.expect(candidate in current, f"candidate {candidate} not in {current}")
    await conductor.call(
        "request_candidate_review",
        review_issue_id=fleet.issues["review"],
        candidate_id=candidate,
        reviewer_id=fleet.user_id("review"),
    )


async def reviewer_reports(
    fleet: Fleet, review: FleetAgent, *, verdict: str, first: bool
) -> None:
    """The reviewer starts from its desk's review request, claims the review
    issue, reads the source in full and reports on that exact candidate."""
    candidate = fleet.facts["candidates"][-1]
    review_id, build_id = fleet.issues["review"], fleet.issues["build"]
    if first:
        desk = await review.start()
    else:
        await review.catch_up()
        desk = await review.call("my_desk")
    asked = [row["candidate_id"] for row in desk["asks"]["review_requests"]["items"]]
    review.expect(candidate in asked, f"no request for {candidate}: {asked}")
    context = await review.call("get_issue_work_context", issue_id=review_id)
    claim = await review.call(
        "claim_issue", issue_id=review_id, if_match=context["issue_etag"]
    )
    source = await review.call("get_issue_work_context", issue_id=build_id)
    under_review = source["review_tracking"]["current_candidate"]["candidate"]
    review.expect(
        under_review["candidate_id"] == candidate,
        f"the source's current candidate is {under_review}",
    )
    if verdict == "HOLD":
        await review.call(
            "comment_on_issue",
            issue_id=build_id,
            body="HOLD: the header row is missing when the result is empty (P1).",
        )
    await review.call(
        "complete_claim",
        issue_id=review_id,
        generation=claim["generation"],
        verdict=verdict,
        results=[
            {
                "condition": "matches the design contract",
                "state": "not_met" if verdict == "HOLD" else "met",
                "note": "an empty result drops the header"
                if verdict == "HOLD"
                else "re-ran the 13 tests",
            }
        ],
        review_candidate_id=candidate,
        **({"findings": {"P1": 1}} if verdict == "HOLD" else {}),
    )
    if verdict != "HOLD":
        await review.put_down()


async def conductor_reads_verdict(
    fleet: Fleet, conductor: FleetAgent, *, verdict: str
) -> None:
    """The conductor reads the exact candidate's reported review and records
    what it means for the build slice."""
    candidate = fleet.facts["candidates"][-1]
    await conductor.catch_up()
    queue = await conductor.call(
        "get_project_review_queue", project_id=fleet.project_id
    )
    rows = [
        row for row in queue["items"] if row["candidate"]["candidate_id"] == candidate
    ]
    # Gate on what the reviewer it asked reported, not the candidate's newest
    # review by whoever wrote it (C5).
    reported = (
        requested_reviewer_verdict(rows[0], fleet.user_id("review")) if rows else None
    )
    conductor.expect(
        reported == verdict,
        f"expected a {verdict} on {candidate}, the queue says {rows}",
    )
    if verdict == "HOLD":
        body = (
            "HOLD recorded: an empty result must still write the header row. "
            "Builder: rework and resubmit."
        )
    else:
        await conductor.call("get_issue_work_context", issue_id=fleet.issues["build"])
        body = "PASS on the replacement candidate accepted. Builder: close the slice."
    await conductor.call("comment_on_issue", issue_id=fleet.issues["build"], body=body)


async def build_closes_through_approval(fleet: Fleet, build: FleetAgent) -> None:
    """The builder's close is an ask; the operator decides; the same key applies."""
    issue_id = fleet.issues["build"]
    await build.catch_up()
    asked = await build.call(
        "update_issue", issue_id=issue_id, status="done", idempotency_key="build-close"
    )
    closed = asked
    if asked.get("code") == "approval_required":
        build.expect(asked.get("applied") is False, f"an ask that applied: {asked}")
        operator_decides(fleet, asked["approval"]["id"])
        approval = asked["approval"]
        decided = await build.follow(
            asked,
            after=build.cursor,
            kind=approval["target_kind"],
            target=approval["target_id"],
            wait=0,
        )  # a filtered read: it does not move the processed cursor
        verbs = [event["verb"] for event in decided["events"]]
        build.expect("approval_approved" in verbs, f"no decision seen: {verbs}")
        closed = await build.call(
            "update_issue",
            issue_id=issue_id,
            status="done",
            idempotency_key="build-close",
        )
    build.expect(closed.get("status") == "done", f"build not closed: {closed}")
    await build.put_down()


def operator_decides(fleet: Fleet, approval_id: int) -> None:
    pending = fleet.ok(fleet.operator.get("/approvals?state=pending"))
    assert [row["id"] for row in pending] == [approval_id], pending
    fleet.ok(
        fleet.operator.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": "approve", "note": "reviewed PASS"},
        )
    )


async def conductor_closes_the_outcome(fleet: Fleet, conductor: FleetAgent) -> None:
    outcome = fleet.issues["outcome"]
    await conductor.catch_up()
    await conductor.call("update_issue", issue_id=fleet.issues["review"], status="done")
    children = await conductor.call("list_subtasks", issue_id=outcome)
    statuses = [row["status"] for row in children["items"]]
    conductor.expect(statuses == ["done"] * 3, f"open slices: {statuses}")
    await conductor.call("update_issue", issue_id=outcome, status="done")
    await conductor.put_down()


async def the_recipe(fleet: Fleet, agents: dict[str, FleetAgent]) -> None:
    conductor, design, build, review = (agents[role] for role in ROLES)
    try:
        await plan_and_delegate(fleet, conductor, agents)
        await design_asks(fleet, design)
        await conductor_sees_the_question(fleet, conductor)
        operator_answers(fleet)
        await design_resumes(fleet, design)
        await conductor_closes_design(fleet, conductor)
        await build_submits(fleet, build, revision=REVISION_1, rework=False)
        await conductor_requests_review(fleet, conductor)
        await reviewer_reports(fleet, review, verdict="HOLD", first=True)
        await conductor_reads_verdict(fleet, conductor, verdict="HOLD")
        await build_submits(fleet, build, revision=REVISION_2, rework=True)
        await conductor_requests_review(fleet, conductor)
        await reviewer_reports(fleet, review, verdict="PASS", first=False)
        await conductor_reads_verdict(fleet, conductor, verdict="PASS")
        await build_closes_through_approval(fleet, build)
        await conductor_closes_the_outcome(fleet, conductor)
    except Stalled as stall:
        fleet.facts["stalled"] = str(stall)


def measure(agents: dict[str, FleetAgent]) -> dict[str, dict[str, int]]:
    measured = {
        role: {
            "tool_calls": agent.meter.calls,
            "bytes_received": agent.meter.bytes_received,
            # Every acknowledgement the recipe's put-down makes names one id.
            "single_acks": agent.meter.tools["mark_notification_read"],
        }
        for role, agent in agents.items()
    }
    measured["total"] = {
        name: sum(measured[role][name] for role in agents) for name in MEASURES
    }
    return measured


def test_a_fleet_follows_the_conductor_recipe_to_an_outcome_done(fleet):
    agents = play(fleet, the_recipe)

    final = {name: fleet.status(issue_id) for name, issue_id in fleet.issues.items()}
    assert final == dict.fromkeys(("outcome", "design", "build", "review"), "done"), (
        final,
        fleet.facts.get("stalled"),
    )
    refused = [
        (refusal.agent, refusal.tool, refusal.answer.get("code"))
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

    measured = measure(agents)
    print(f"fleet trial: {measured}")
    ceilings = json.loads(CEILINGS.read_text(encoding="utf-8"))
    over = [
        f"{role}.{name}: {measured[role][name]} > {ceilings[role][name]}"
        for role in (*ROLES, "total")
        for name in MEASURES
        if measured[role][name] > ceilings[role][name]
    ]
    assert over == [], (over, measured)


def test_the_ceilings_file_says_how_it_may_change():
    ceilings = json.loads(CEILINGS.read_text(encoding="utf-8"))
    assert set(ceilings) == {"note", *ROLES, "total"}
    assert "stated reason in the same PR" in ceilings["note"]
    for role in (*ROLES, "total"):
        assert set(ceilings[role]) == set(MEASURES), role
        assert all(isinstance(value, int) for value in ceilings[role].values()), role
    for name in MEASURES:  # the total is the sum of the roles, not a fifth guess
        assert ceilings["total"][name] == sum(ceilings[role][name] for role in ROLES)


# --- F2: what the desk asks, pickup serves or names -------------------------------


def test_f2_after_a_hold_and_a_second_request_desk_and_pickup_agree(fleet):
    """FLEET-4, second case: the reviewer reported HOLD, the builder resubmitted
    and the conductor asked again. The desk asked for the review while the ready
    lane held the issue back as ``completed_by_you`` and pickup answered
    ``nothing_ready``. Under D16 the second request answers the reviewer's own
    completion: the desk asks, the ready lane offers, pickup takes it."""
    build_id = fleet.issue("Build", assignee="conductor", delegate="build")
    review_id = fleet.issue("Review", assignee="conductor", delegate="review")

    async def scene(fleet: Fleet, agents: dict[str, FleetAgent]) -> None:
        conductor, build, review = (
            agents[role] for role in ("conductor", "build", "review")
        )

        async def submit(previous: int | None, revision: str) -> int:
            context = await build.call("get_issue_work_context", issue_id=build_id)
            claim = await build.call(
                "claim_issue", issue_id=build_id, if_match=context["issue_etag"]
            )
            done = await build.call(
                "complete_claim",
                issue_id=build_id,
                generation=claim["generation"],
                verdict="PASS",
                candidate_submission={
                    "identity": {
                        "kind": "git",
                        "source": "fleet/f2",
                        "revision": revision,
                    },
                    "expected_previous_candidate_id": previous,
                },
            )
            return done["candidate_submission"]["candidate_id"]

        async def ask(candidate: int) -> None:
            await conductor.call(
                "request_candidate_review",
                review_issue_id=review_id,
                candidate_id=candidate,
                reviewer_id=review.user_id,
            )

        async def reviewer_view() -> tuple[list[int], list, dict, dict]:
            desk = await review.call("my_desk")
            asked = [
                row["candidate_id"]
                for row in (desk["asks"].get("review_requests") or {}).get("items", [])
                if row["status"] == "requested"
            ]
            ready = desk["work"]["ready"]
            not_offered = {
                row["issue"]["id"]: row["reason"]
                for row in desk["work"]["waiting_on_operator"]["not_offered"]["items"]
            }
            return (
                asked,
                [row["issue"]["id"] for row in ready["items"]],
                ready,
                not_offered,
            )

        first = await submit(None, "1" * 40)
        await ask(first)
        picked = await review.call("claim_next_ready", idempotency_key="f2-review-1")
        assert picked["claimed"] and picked["issue"]["id"] == review_id, picked
        await review.call(
            "complete_claim",
            issue_id=review_id,
            generation=picked["lease"]["generation"],
            verdict="HOLD",
            review_candidate_id=first,
        )
        second = await submit(first, "2" * 40)
        await ask(second)

        asked, ready_ids, ready, not_offered = await reviewer_view()
        assert asked == [second], asked
        # The baseline: ready_total 0 and not_offered completed_by_you.
        assert ready_ids == [review_id] and ready["total"] == 1, ready
        assert review_id not in not_offered, not_offered
        again = await review.call("claim_next_ready", idempotency_key="f2-review-2")
        assert again["claimed"] is True and again["issue"]["id"] == review_id, again
        await review.call(
            "complete_claim",
            issue_id=review_id,
            generation=again["lease"]["generation"],
            verdict="PASS",
            review_candidate_id=second,
        )
        # Fulfilled: the desk stops asking and pickup names nothing.
        asked, ready_ids, _ready, not_offered = await reviewer_view()
        assert asked == [] and ready_ids == [], (asked, ready_ids)
        assert not_offered == {review_id: "completed_by_you"}, not_offered
        empty = await review.call("claim_next_ready", idempotency_key="f2-review-3")
        assert empty["reason"] == "nothing_ready" and empty["review_request"] is None

    agents = play(fleet, scene, roles=("conductor", "build", "review"))
    assert [r.tool for agent in agents.values() for r in agent.meter.refusals] == []


# --- what the fleet needs next: C6, C4, C5 ----------------------------------------


def test_c6_disjoint_paths_are_both_fenced_without_a_root_catalog(fleet, monkeypatch):
    """C6 has landed: with no root catalog, paths are compared under one implicit
    root. A disjoint fence is granted beside another, an overlapping one is
    refused fence_overlap and names the path and the holder's issue, and pickup
    declares its paths on the claim it makes."""
    monkeypatch.setattr(config, "LEASE_ROOT_CATALOG", None)  # the default instance
    alpha = fleet.issue("Alpha", assignee="conductor", delegate="design")
    beta = fleet.issue("Beta", assignee="conductor", delegate="build")
    gamma = fleet.issue("Gamma", assignee="conductor", delegate="review")

    async def scene(fleet: Fleet, agents: dict[str, FleetAgent]) -> None:
        first, second, third = agents["design"], agents["build"], agents["review"]
        context = await first.call("get_issue_work_context", issue_id=alpha)
        held = await first.call(
            "claim_issue",
            issue_id=alpha,
            if_match=context["issue_etag"],
            paths=["src/alpha/a.py"],
        )
        assert held["active"] is True, held
        assert held["path_fence"] == "lexical, root unknown", held
        context = await second.call("get_issue_work_context", issue_id=beta)
        granted, answer = await second.attempt(
            "claim_issue",
            issue_id=beta,
            if_match=context["issue_etag"],
            paths=["docs/beta/b.md"],
        )
        require(
            granted,
            "C6: a lease declaring paths that overlap no other lease is granted "
            f"on an instance with no root catalog; it was refused: {answer}",
        )
        lease = await first.call("get_issue_lease", issue_id=alpha)
        require(lease.get("active") is True, f"C6: the first lease stands: {lease}")

        # Pickup with a path inside the design seat's fence claims nothing and
        # says why; with its own path it claims the ready issue.
        fenced = await third.call(
            "claim_next_ready", paths=["src/alpha"], idempotency_key="c6-overlap"
        )
        assert fenced["claimed"] is False and fenced["reason"] == "fence_overlap"
        assert fenced["fence"]["path"] == "src/alpha"
        assert fenced["fence"]["holder_path"] == "src/alpha/a.py"
        assert fenced["fence"]["issue"]["id"] == alpha
        taken = await third.call(
            "claim_next_ready", paths=["src/gamma"], idempotency_key="c6-own"
        )
        assert taken["claimed"] is True and taken["issue"]["id"] == gamma, taken
        assert taken["lease"]["declared_paths"] == ["src/gamma"]

    agents = play(fleet, scene, roles=("design", "build", "review"))
    assert [r.tool for agent in agents.values() for r in agent.meter.refusals] == []


@unmet(
    "C4",
    "a lapsed lease writes no event, no notification and no attention "
    "row for the conductor that delegated the work",
)
def test_c4_a_lapsed_lease_reaches_the_conductor_that_delegated_it(fleet):
    async def scene(fleet: Fleet, agents: dict[str, FleetAgent]) -> None:
        conductor, seat = agents["conductor"], agents["design"]
        made = await conductor.call(
            "create_issue",
            title="Work that will be abandoned",
            project_id=fleet.project_id,
        )
        issue_id = made["id"]
        context = await conductor.call("get_issue_work_context", issue_id=issue_id)
        await conductor.call(
            "assign_issue",
            issue_id=issue_id,
            assignee_id=conductor.user_id,
            if_match=context["issue_etag"],
        )
        await conductor.call(
            "delegate_issue", issue_id=issue_id, agent_user_id=seat.user_id
        )
        context = await seat.call("get_issue_work_context", issue_id=issue_id)
        await seat.call(
            "claim_issue", issue_id=issue_id, if_match=context["issue_etag"]
        )
        await conductor.start()
        lapse_lease(fleet.db_file, issue_id)  # the holder went away

        # One wait_for_events call is enough: it observes the lapse on entry.
        page = await conductor.call("wait_for_events", after=conductor.cursor, wait=0)
        lapsed_events = [
            event
            for event in page["events"]
            if event["verb"] == "lease_lapsed" and event["target_id"] == issue_id
        ]
        listed = await conductor.call("list_notifications", unread=True, limit=100)
        notified = [
            row
            for row in listed["items"]
            if row["verb"] == "lease_lapsed" and row["target_id"] == issue_id
        ]
        ranking = await conductor.call("get_attention_ranking")
        attention = [
            row
            for row in ranking["items"]
            if row["source_kind"] == "issue" and row["source_id"] == issue_id
        ]
        require(
            len(lapsed_events) == 1 and notified and attention,
            "C4: the conductor that delegated a lapsed lease gets one lease_lapsed "
            "event within one wait_for_events call, a notification and an "
            f"attention row; it got events {[e['verb'] for e in page['events']]}, "
            f"{len(notified)} lease_lapsed notifications and attention "
            f"{[(row['signal'], row['source_id']) for row in ranking['items']]}",
        )

    play(fleet, scene, roles=("conductor", "design"))


def requested_reviewer_verdict(queue_row: dict, reviewer_id: int) -> str | None:
    """What the requested reviewer reported on this candidate: what the conductor
    gates on, as docs/CONDUCTOR_RECIPE.md says.

    C5 added `reported_verdict` to the review request row: the verdict of the
    requested reviewer's own report on that review issue about that exact
    candidate, which no other account's review can move. None until it reports."""
    requests = (queue_row.get("review_requests") or {}).get("items", [])
    for request in requests:
        if request["reviewer_id"] == reviewer_id:
            return request["reported_verdict"]
    return None


def test_c5_an_unrequested_pass_does_not_move_the_requested_hold(fleet):
    fleet.onboard("unasked", "uma")
    build_id = fleet.issue("Build", assignee="conductor", delegate="build")
    review_id = fleet.issue("Review", assignee="conductor", delegate="review")
    chore_id = fleet.issue("Unrelated chore", assignee="conductor", delegate="unasked")

    async def scene(fleet: Fleet, agents: dict[str, FleetAgent]) -> None:
        conductor, build = agents["conductor"], agents["build"]
        review, unasked = agents["review"], agents["unasked"]
        picked = await build.call(
            "claim_next_ready", project_id=fleet.project_id, idempotency_key="c5-build"
        )
        assert picked["claimed"] and picked["issue"]["id"] == build_id, picked
        done = await build.call(
            "complete_claim",
            issue_id=build_id,
            generation=picked["lease"]["generation"],
            verdict="PASS",
            candidate_submission={
                "identity": {"kind": "git", "source": "fleet/x", "revision": "a" * 40},
                "expected_previous_candidate_id": None,
            },
        )
        candidate = done["candidate_submission"]["candidate_id"]
        await conductor.call(
            "request_candidate_review",
            review_issue_id=review_id,
            candidate_id=candidate,
            reviewer_id=review.user_id,
        )
        for agent, issue_id, verdict in (
            (review, review_id, "HOLD"),  # the reviewer the conductor asked
            (unasked, chore_id, "PASS"),  # nobody asked this account
        ):
            context = await agent.call("get_issue_work_context", issue_id=issue_id)
            claim = await agent.call(
                "claim_issue", issue_id=issue_id, if_match=context["issue_etag"]
            )
            await agent.call(
                "complete_claim",
                issue_id=issue_id,
                generation=claim["generation"],
                verdict=verdict,
                review_candidate_id=candidate,
            )
        queue = await conductor.call(
            "get_project_review_queue", project_id=fleet.project_id
        )
        (row,) = [
            row
            for row in queue["items"]
            if row["candidate"]["candidate_id"] == candidate
        ]
        latest = row.get("latest_review") or {}
        require(
            requested_reviewer_verdict(row, review.user_id) == "HOLD",
            "C5: what the conductor gates on says the requested reviewer reported "
            f"HOLD; the request rows carry {row.get('review_requests')}, and the "
            f"queue reads {row['review_state']}, latest by user "
            f"{(latest.get('reported_by') or {}).get('id')} "
            f"({latest.get('reported_verdict')})",
        )

    play(fleet, scene, roles=("conductor", "build", "review", "unasked"))
