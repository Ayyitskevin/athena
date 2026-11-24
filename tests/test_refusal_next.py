"""Every registered `next` makes progress, or waits for the change it needs, from
the state that produces its refusal (F11, FLEET-15).

WHY: a refusal's `next` is the call the registry promises resolves it, and an agent
makes that call without reading prose. The reviewer found one that does not:
`claim_held` named `claim_next_ready`, which for a seat whose one delegation is the
held issue answers `nothing_ready` and says nothing about the held work, when it
frees, or what to do. The agent is told to call something that cannot help it.

So, for every code in `core/refusal_codes.py` that names a `next`, this test builds
the state that produces the refusal, takes the refusal as MCP hands it to the agent,
makes exactly the call `next` names, as that same agent, over the MCP protocol on the
seat profile, and requires one of exactly two outcomes:

* **progress**: after following `next`, the refused call, repeated (with what the
  `next` answered), succeeds, or the caller now holds something it did not (a
  lease, an acknowledged position).
* **wait**: the `next` is a call that parks until the blocking state changes. It is
  made twice. With nobody changing anything it must park and come back empty at its
  timeout, not answer at once from history, and the refused call must still be
  refused. Then the test changes the blocking state (the holder completes, the
  operator decides), and the same call must answer with that change, after which
  the repeated refused call succeeds.

Anything else fails. In particular a read that restates the refusal (who holds the
lease and until when, what this token may do) is neither: the agent learns nothing
it did not have, and nothing will tell it when to try again. The fix for a failing
code is its registry entry (a better call, or none), never this test.

A code whose state cannot be built here is on UNBUILDABLE with its reason; the
registry and the scenarios are checked against each other so a new code with a
`next` cannot be added without a scenario or a reason.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Any

from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from support import ADMIN, bearer, bootstrap, make_app, onboard_agent

from athena.core import refusal_codes
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient, AthenaError

#: Codes whose `next` this test cannot follow from a built state, with the reason.
UNBUILDABLE = {
    "coordination_root_unconfirmed": (
        "the MCP client answers it when Athena's claim answer omits the root the "
        "agent sent; this server always echoes the root it stored, so only an "
        "older or broken server produces the state"
    ),
}

#: How long the first, unchanged wait parks before it must come back empty.
QUIET_WAIT_SECONDS = 1
#: How long the second wait may take to answer once the state has changed.
CHANGED_WAIT_SECONDS = 10


class Seat:
    """One agent account: its token, a REST client and an MCP server on `seat`."""

    def __init__(self, app, onboarded: dict, *, scopes: frozenset[str] | None = None):
        self.user_id = onboarded["user"]["id"]
        self.name = onboarded["user"]["name"]
        self.http = TestClient(app, headers=bearer(onboarded))
        self.api = AthenaClient(client=self.http)
        self.server = mcp_server.build_server(
            self.api, scopes=scopes, profile=mcp_server.PROFILES["seat"]
        )

    def refused(self, method: str, path: str, **kwargs: Any) -> dict:
        """A refusal met over REST, as MCP would hand it to this agent: the REST
        body, the status, Retry-After and ETag, and the registry's `next`. Used
        only where no seat tool can make the refused request (the tool checks the
        argument itself, or always sends a precondition)."""
        response = self.http.request(method, path, **kwargs)
        try:
            answer = self.api._result(response)  # noqa: SLF001 — the MCP client's own read
        except AthenaError as exc:
            return exc.refusal()
        assert isinstance(answer, dict) and "next" in answer, answer
        return answer

    def refused_by(self, tool: str, **arguments: Any) -> dict:
        """A refusal met through a seat tool over the MCP protocol: exactly what
        the agent receives (an error object, or the 202 ask of a gated write)."""
        ok, answer = self.call(tool, **arguments)
        assert isinstance(answer, dict) and "code" in answer, (tool, answer)
        assert not ok or answer.get("applied") is False, (tool, answer)
        return answer

    def call(self, tool: str, **arguments: Any) -> tuple[bool, Any]:
        """One tool call over the MCP protocol, as a client makes it."""

        async def run():
            async with create_connected_server_and_client_session(
                self.server._mcp_server  # noqa: SLF001 — the protocol server under test
            ) as session:
                return await session.call_tool(tool, arguments)

        result = asyncio.run(run())
        text = "".join(getattr(block, "text", "") for block in result.content)
        return not result.isError, json.loads(text) if text else None

    def etag(self, issue_id: int) -> str:
        return self.http.get(f"/issues/{issue_id}").headers["ETag"]

    def claim(self, issue_id: int, **body: Any) -> dict:
        claimed = self.http.post(
            f"/issues/{issue_id}/claim",
            json=body,
            headers={"If-Match": self.etag(issue_id)},
        )
        assert claimed.status_code in (200, 201), claimed.text
        return claimed.json()

    def complete(self, issue_id: int, generation: str) -> None:
        done = self.http.post(
            f"/issues/{issue_id}/complete", json={"generation": generation}
        )
        assert done.status_code == 200, done.text


class World:
    """An operator (user 1), a project, and seats Ada and Bo."""

    def __init__(self, app, client: TestClient):
        self.app = app
        self.client = client
        bootstrap(client)
        self.ada = Seat(app, onboard_agent(client, email="ada@e.com", name="Ada"))
        self.bo = Seat(app, onboard_agent(client, email="bo@e.com", name="Bo"))
        made = client.post("/projects", json={"name": "W", "key": "WRK"}, headers=ADMIN)
        assert made.status_code == 201, made.text
        self.project_id = made.json()["id"]

    def issue(self, title: str, *seats: Seat) -> int:
        made = self.client.post(
            "/issues",
            json={"title": title, "project_id": self.project_id},
            headers=ADMIN,
        )
        assert made.status_code == 201, made.text
        issue_id = made.json()["id"]
        for seat in seats:
            handed = self.client.post(
                f"/issues/{issue_id}/delegate",
                json={"user_id": seat.user_id},
                headers=ADMIN,
            )
            assert handed.status_code == 201, handed.text
        return issue_id


@dataclass
class Scenario:
    """A refusal met by `seat` and what following its `next` takes from there.

    ``arguments`` holds the arguments each plausible `next` tool takes in this
    state; a tool missing from it fails the test. ``repeat`` makes the refused
    call again, given what the `next` answered, and returns ``(succeeded,
    answer)``; a `next` answer it cannot use raises. ``unblock`` changes the
    blocking state, for a wait to observe; None where nobody else holds the key.
    """

    seat: Seat
    refusal: dict
    arguments: dict[str, dict[str, Any]]
    repeat: Callable[[Any], tuple[bool, Any]]
    unblock: Callable[[], None] | None = None


# --- the scenarios, one per code with a `next` ----------------------------------------


def _claimed_by(seat: Seat, issue_id: int) -> Callable[[Any], tuple[bool, Any]]:
    """Repeat a refused claim: progress when the seat now holds the lease."""

    def repeat(_answer: Any) -> tuple[bool, Any]:
        ok, lease = seat.call(
            "claim_issue", issue_id=issue_id, if_match=seat.etag(issue_id)
        )
        return ok and lease.get("holder_id") == seat.user_id, lease

    return repeat


def _precondition_required(world: World) -> Scenario:
    issue_id = world.issue("Needs a tag", world.ada)
    # The seat tool always sends a tag, so the missing one comes over REST.
    refusal = world.ada.refused("POST", f"/issues/{issue_id}/claim", json={})

    def repeat(answer: Any) -> tuple[bool, Any]:
        tag = answer.get("_etag") or answer.get("issue_etag")
        assert tag, f"the read gave no tag: {answer}"
        ok, lease = world.ada.call("claim_issue", issue_id=issue_id, if_match=tag)
        return ok and lease.get("holder_id") == world.ada.user_id, lease

    return Scenario(
        world.ada,
        refusal,
        {
            "get_issue": {"issue_id": issue_id},
            "get_issue_work_context": {"issue_id": issue_id},
        },
        repeat,
    )


def _precondition_failed(world: World) -> Scenario:
    issue_id = world.issue("Moves under you", world.ada)
    stale = world.ada.etag(issue_id)
    edited = world.client.patch(
        f"/issues/{issue_id}", json={"title": "Moved"}, headers=ADMIN
    )
    assert edited.status_code == 200, edited.text
    refusal = world.ada.refused_by("claim_issue", issue_id=issue_id, if_match=stale)

    def repeat(answer: Any) -> tuple[bool, Any]:
        # The read gives the current issue to merge into, and its tag.
        assert answer.get("title") == "Moved", answer
        ok, lease = world.ada.call(
            "claim_issue", issue_id=issue_id, if_match=answer["_etag"]
        )
        return ok and lease.get("holder_id") == world.ada.user_id, lease

    return Scenario(world.ada, refusal, {"get_issue": {"issue_id": issue_id}}, repeat)


def _claim_held(world: World) -> Scenario:
    """FLEET-15: Bo's one delegation is the issue Ada holds."""
    issue_id = world.issue("Held", world.ada, world.bo)
    lease = world.ada.claim(issue_id)
    refusal = world.bo.refused_by(
        "claim_issue", issue_id=issue_id, if_match=world.bo.etag(issue_id)
    )
    return Scenario(
        world.bo,
        refusal,
        {
            "claim_next_ready": {},
            "get_issue_lease": {"issue_id": issue_id},
            "wait_for_events": {
                "kind": "issue",
                "target": issue_id,
                "after": refusal.get("events_after"),
            },
        },
        _claimed_by(world.bo, issue_id),
        unblock=lambda: world.ada.complete(issue_id, lease["generation"]),
    )


def _claim_required(world: World) -> Scenario:
    issue_id = world.issue("Never claimed", world.ada)
    refusal = world.ada.refused_by(
        "complete_claim", issue_id=issue_id, generation="a" * 32
    )
    claim = _claimed_by(world.ada, issue_id)

    def repeat(answer: Any) -> tuple[bool, Any]:
        # The read says there is no possession to complete, and the issue is
        # free: claiming it is the progress the hint names.
        assert isinstance(answer, dict) and (
            answer.get("found") is False or answer.get("active") is False
        ), f"the read does not say the issue is free: {answer}"
        return claim(answer)

    return Scenario(
        world.ada, refusal, {"get_issue_lease": {"issue_id": issue_id}}, repeat
    )


def _completed_with_read_generation(
    world: World, issue_id: int, refusal: dict
) -> Callable[[Any], tuple[bool, Any]]:
    def repeat(answer: Any) -> tuple[bool, Any]:
        assert answer.get("holder_id") == world.ada.user_id, answer
        generation = answer["generation"]
        # The read supplies what the refusal did not: no generation in it.
        assert generation not in json.dumps(refusal), refusal
        return world.ada.call(
            "complete_claim", issue_id=issue_id, generation=generation
        )

    return repeat


def _invalid_lease_generation(world: World) -> Scenario:
    issue_id = world.issue("Typo", world.ada)
    world.ada.claim(issue_id)
    # The seat tool checks the shape itself, so this one comes over REST.
    refusal = world.ada.refused(
        "POST", f"/issues/{issue_id}/complete", json={"generation": "not-a-generation"}
    )
    return Scenario(
        world.ada,
        refusal,
        {"get_issue_lease": {"issue_id": issue_id}},
        _completed_with_read_generation(world, issue_id, refusal),
    )


def _lease_generation_mismatch(world: World) -> Scenario:
    issue_id = world.issue("Stale", world.ada)
    world.ada.claim(issue_id)
    refusal = world.ada.refused_by(
        "complete_claim", issue_id=issue_id, generation="0" * 32
    )
    return Scenario(
        world.ada,
        refusal,
        {"get_issue_lease": {"issue_id": issue_id}},
        _completed_with_read_generation(world, issue_id, refusal),
    )


def _fence_overlap(world: World) -> Scenario:
    held = world.issue("Ada's paths", world.ada)
    lease = world.ada.claim(held, paths=["src/athena"])
    wanted = world.issue("Bo's work", world.bo)
    refusal = world.bo.refused_by(
        "claim_issue",
        issue_id=wanted,
        if_match=world.bo.etag(wanted),
        paths=["src/athena/core"],
    )
    fence = refusal.get("fence") or {}

    def repeat(_answer: Any) -> tuple[bool, Any]:
        ok, claimed = world.bo.call(
            "claim_issue",
            issue_id=wanted,
            if_match=world.bo.etag(wanted),
            paths=["src/athena/core"],
        )
        return ok and claimed.get("holder_id") == world.bo.user_id, claimed

    return Scenario(
        world.bo,
        refusal,
        {
            "claim_next_ready": {"paths": ["src/athena/core"]},
            "get_issue_lease": {"issue_id": held},
            "wait_for_events": {
                "kind": "issue",
                "target": (fence.get("issue") or {}).get("id"),
                "after": fence.get("events_after"),
            },
        },
        repeat,
        unblock=lambda: world.ada.complete(held, lease["generation"]),
    )


def _cursor_ahead(world: World) -> Scenario:
    world.issue("Something happened")
    refusal = world.ada.refused_by("advance_desk_cursor", after_id=999_999)

    def repeat(answer: Any) -> tuple[bool, Any]:
        # The corrected call is the `next` itself: the cursor now stands where
        # the refusal said it may, a position the seat did not hold.
        return answer.get("after_id") == refusal["latest_visible_event_id"], answer

    return Scenario(
        world.ada,
        refusal,
        {"advance_desk_cursor": {"after_id": refusal["latest_visible_event_id"]}},
        repeat,
    )


def _approval_required(world: World) -> Scenario:
    gated = world.client.put(
        f"/approvals/policies/{world.ada.user_id}",
        json={"action_kind": "issue.close"},
        headers=ADMIN,
    )
    assert gated.status_code == 200, gated.text
    issue_id = world.issue("Gated", world.ada)
    assigned = world.client.put(
        f"/issues/{issue_id}/assignee",
        json={"assignee_id": world.ada.user_id},
        headers=ADMIN,
    )
    assert assigned.status_code == 200, assigned.text
    refusal = world.ada.refused_by(
        "update_issue", issue_id=issue_id, status="done", idempotency_key="close-gated"
    )
    approval = refusal["approval"]
    assert approval["state"] == "pending", approval

    def repeat(_answer: Any) -> tuple[bool, Any]:
        ok, issue = world.ada.call(
            "update_issue",
            issue_id=issue_id,
            status="done",
            idempotency_key="close-gated",
        )
        applied = ok and issue.get("applied") is not False
        return applied and issue.get("status") == "done", issue

    def decide() -> None:
        decided = world.client.post(
            f"/approvals/{approval['id']}/decision",
            json={"decision": "approve"},
            headers=ADMIN,
        )
        assert decided.status_code == 200, decided.text

    return Scenario(
        world.ada,
        refusal,
        {
            "wait_for_events": {
                "kind": approval["target_kind"],
                "target": approval["target_id"],
                "after": refusal.get("events_after"),
            }
        },
        repeat,
        unblock=decide,
    )


def _run_id_mismatch(world: World) -> Scenario:
    # The seat's session is tagged with a run (athena-mcp sends it on every
    # request); it then reports a heartbeat on some other run (A12).
    world.ada.api.set_run("ada-session-run")
    refusal = world.ada.refused_by("heartbeat_agent_run", run_id="not-this-session")

    def repeat(answer: Any) -> tuple[bool, Any]:
        # The read names the session's run, which the refused call takes.
        assert answer["run_id"] == "ada-session-run", answer
        return world.ada.call("heartbeat_agent_run", run_id=answer["run_id"])

    return Scenario(world.ada, refusal, {"current_run": {}}, repeat)


SCENARIOS: dict[str, Callable[[World], Scenario]] = {
    "precondition_required": _precondition_required,
    "precondition_failed": _precondition_failed,
    "claim_held": _claim_held,
    "claim_required": _claim_required,
    "invalid_lease_generation": _invalid_lease_generation,
    "lease_generation_mismatch": _lease_generation_mismatch,
    "fence_overlap": _fence_overlap,
    "cursor_ahead": _cursor_ahead,
    "approval_required": _approval_required,
    "run_id_mismatch": _run_id_mismatch,
}


def _codes_with_next() -> set[str]:
    entries = [
        *refusal_codes.REFUSAL_CODES.values(),
        *refusal_codes.MCP_REFUSAL_CODES.values(),
    ]
    return {entry.code for entry in entries if entry.next is not None}


def _judge_wait(code: str, scenario: Scenario, arguments: dict[str, Any]) -> str:
    """A `next` that waits: it parks while nothing changes, and answers with the
    change once the test makes it; the refused call then succeeds."""
    seat = scenario.seat
    assert scenario.unblock is not None, "a wait needs a change to wait for"
    ok, quiet = seat.call("wait_for_events", **arguments, wait=QUIET_WAIT_SECONDS)
    assert ok, quiet
    assert quiet["events"] == [] and quiet["wait"]["outcome"] == "timeout", (
        "the wait answered at once instead of parking (history, or a cursor the "
        f"refusal does not carry): {[e['verb'] for e in quiet['events']]}"
    )
    still, answer = scenario.repeat(None)
    assert not still, f"nothing changed, yet the refused call succeeded: {answer}"
    assert answer.get("code") == code, answer
    scenario.unblock()
    ok, changed = seat.call("wait_for_events", **arguments, wait=CHANGED_WAIT_SECONDS)
    assert ok and changed["events"], f"the wait missed the change: {changed}"
    assert changed["wait"]["outcome"] == "event", changed
    done, answer = scenario.repeat(changed)
    assert done, f"the change came, but the refused call still fails: {answer}"
    return "wait"


def _follow(code: str, tmp_path) -> str:
    """Build the state, meet the refusal, make its `next` call, judge the outcome:
    "progress" or "wait" as the module docstring defines them, or raise."""
    app, _db_file = make_app(tmp_path, name=f"{code}.db")
    with TestClient(app) as client:
        world = World(app, client)
        scenario = SCENARIOS[code](world)
        refusal = scenario.refusal
        assert refusal["code"] == code, refusal
        named = refusal["next"]
        assert named == refusal_codes.next_call(code), refusal
        tool = named["tool"]
        assert tool in mcp_server.SEAT_PROFILE, tool
        assert tool in scenario.arguments, (
            f"the registry names {tool}, which this state gives no arguments "
            f"for: {named['hint']}"
        )
        arguments = scenario.arguments[tool]
        if tool == "wait_for_events":
            return _judge_wait(code, scenario, arguments)
        ok, answer = scenario.seat.call(tool, **arguments)
        assert ok, f"{tool} refused: {answer}"
        done, repeated = scenario.repeat(answer)
        assert done, (
            f"neither progress nor a wait: after {tool} answered {answer}, the "
            f"refused call still fails: {repeated}"
        )
        return "progress"


def test_every_registered_next_makes_progress_or_waits_for_its_change(tmp_path):
    """One test over the whole registry: every code with a `next` has a scenario
    or a reason on UNBUILDABLE, and every scenario's `next` reaches "progress" or
    "wait". The failures are collected, so one run names every wrong entry."""
    assert not set(SCENARIOS) & set(UNBUILDABLE)
    assert set(SCENARIOS) | set(UNBUILDABLE) == _codes_with_next(), (
        "a code with a `next` needs a scenario here, or a reason on UNBUILDABLE"
    )
    outcomes: dict[str, str] = {}
    failures: dict[str, str] = {}
    for code in sorted(SCENARIOS):
        try:
            outcomes[code] = _follow(code, tmp_path)
        except AssertionError as exc:
            failures[code] = str(exc)
    assert not failures, failures
    # The waits are the codes whose resolution belongs to someone else.
    assert {code for code, outcome in outcomes.items() if outcome == "wait"} == {
        "approval_required",
        "claim_held",
        "fence_overlap",
    }, outcomes
