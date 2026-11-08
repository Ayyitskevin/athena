"""A bounded long-poll on the events feed: `GET /events?after=N&wait=S`.

Why these tests exist: seats drain the trail in sleep loops. A wait lets a consumer
park one request instead, but only if waiting costs the server nothing it cannot
afford: no worker thread, no open transaction, no lock, a hard cap on how many may
wait and for how long, and a visibility rule identical to the immediate read, so a
waiter can never learn, even by timing, that an event it may not read has landed.
When the cap is hit the answer is today's immediate answer, labelled, never an error.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from athena.core import activity, db, event_wait
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server

ADMIN = {"X-Athena-Actor": "1"}


# --- the waiting primitive, driven with a fake clock --------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _poller(answers: list[list[dict]]):
    calls = {"n": 0}

    async def poll() -> list[dict]:
        calls["n"] += 1
        return answers.pop(0) if answers else []

    return poll, calls


async def _never_disconnected() -> bool:
    return False


def test_slots_count_and_refuse_past_capacity():
    slots = event_wait.WaiterSlots(2)
    assert slots.capacity == 2 and slots.active == 0
    assert slots.try_acquire() and slots.try_acquire()
    assert not slots.try_acquire()
    assert slots.active == 2
    slots.release()
    assert slots.active == 1 and slots.try_acquire()
    slots.release()
    slots.release()
    assert slots.active == 0


def test_wait_returns_on_the_first_visible_batch_and_releases_its_slot():
    clock = _Clock()
    poll, calls = _poller([[], [], [{"id": 7}]])
    slots = event_wait.WaiterSlots(1)
    rows, outcome, elapsed = asyncio.run(
        event_wait.wait_for_events(
            poll,
            timeout=10,
            slots=slots,
            interval=0.5,
            is_disconnected=_never_disconnected,
            clock=clock,
            sleep=clock.sleep,
        )
    )
    assert rows == [{"id": 7}] and outcome == "event"
    assert calls["n"] == 3 and clock.sleeps == [0.5, 0.5]
    assert elapsed == pytest.approx(1.0)
    assert slots.active == 0


def test_wait_times_out_empty_and_never_sleeps_past_the_deadline():
    clock = _Clock()
    poll, calls = _poller([])
    slots = event_wait.WaiterSlots(1)
    rows, outcome, elapsed = asyncio.run(
        event_wait.wait_for_events(
            poll,
            timeout=1.2,
            slots=slots,
            interval=0.5,
            is_disconnected=_never_disconnected,
            clock=clock,
            sleep=clock.sleep,
        )
    )
    assert rows == [] and outcome == "timeout"
    assert clock.sleeps == [0.5, 0.5, pytest.approx(0.2)]
    assert elapsed == pytest.approx(1.2) and calls["n"] == 4
    assert slots.active == 0


def test_no_free_slot_degrades_to_one_immediate_poll():
    clock = _Clock()
    poll, calls = _poller([[]])
    slots = event_wait.WaiterSlots(1)
    assert slots.try_acquire()  # someone else is waiting
    rows, outcome, elapsed = asyncio.run(
        event_wait.wait_for_events(
            poll,
            timeout=10,
            slots=slots,
            interval=0.5,
            is_disconnected=_never_disconnected,
            clock=clock,
            sleep=clock.sleep,
        )
    )
    assert rows == [] and outcome == "capacity" and elapsed == 0
    assert calls["n"] == 1 and clock.sleeps == []
    assert slots.active == 1  # the other waiter's slot, untouched


def test_a_client_that_went_away_frees_its_slot_without_waiting_out_the_clock():
    clock = _Clock()
    poll, calls = _poller([])
    slots = event_wait.WaiterSlots(1)
    seen = {"n": 0}

    async def gone_after_first_tick() -> bool:
        seen["n"] += 1
        return seen["n"] >= 2

    rows, outcome, _ = asyncio.run(
        event_wait.wait_for_events(
            poll,
            timeout=10,
            slots=slots,
            interval=0.5,
            is_disconnected=gone_after_first_tick,
            clock=clock,
            sleep=clock.sleep,
        )
    )
    assert rows == [] and outcome == "disconnected"
    assert calls["n"] == 2 and slots.active == 0


def test_a_failing_poll_releases_the_slot_and_raises():
    clock = _Clock()
    slots = event_wait.WaiterSlots(1)

    async def broken() -> list[dict]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(
            event_wait.wait_for_events(
                broken,
                timeout=1,
                slots=slots,
                interval=0.5,
                is_disconnected=_never_disconnected,
                clock=clock,
                sleep=clock.sleep,
            )
        )
    assert slots.active == 0


# --- the route, over the real app, with a second connection writing -----------------


@pytest.fixture
def feed(tmp_path, monkeypatch):
    # Poll fast so the tests measure behaviour, not the production interval.
    monkeypatch.setattr(event_wait, "EVENT_WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(event_wait, "SLOTS", event_wait.WaiterSlots(4))
    database = tmp_path / "wait.db"
    with TestClient(create_app(database)) as client:
        admin = client.post(
            "/users",
            json={
                "email": "operator@example.test",
                "name": "Operator",
                "password": "fixture-only-password",
            },
        )
        assert admin.status_code == 201, admin.text
        agent = client.post(
            "/users/onboard_agent",
            json={"email": "reader@example.test", "name": "Reader", "scopes": ["read"]},
            headers=ADMIN,
        )
        assert agent.status_code == 201, agent.text
        public = client.post("/issues", json={"title": "Public"}, headers=ADMIN)
        assert public.status_code == 201, public.text
        project = client.post(
            "/projects", json={"name": "Private", "key": "PRV"}, headers=ADMIN
        )
        assert project.status_code == 201, project.text
        hidden = client.post(
            "/issues",
            json={"title": "Hidden", "project_id": project.json()["id"]},
            headers=ADMIN,
        )
        assert hidden.status_code == 201, hidden.text
        private = client.put(
            f"/projects/{project.json()['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        assert private.status_code == 200, private.text
        writer = db.connect(database)  # a second connection: the "other" writer
        baseline = writer.execute("SELECT max(id) FROM activity").fetchone()[0]
        token = agent.json()["token"]["token"]
        try:
            yield {
                "client": client,
                "writer": writer,
                "baseline": baseline,
                "agent_headers": {"Authorization": "Bearer " + token},
                "agent_id": agent.json()["user"]["id"],
                "public_issue": public.json()["id"],
                "hidden_issue": hidden.json()["id"],
            }
        finally:
            writer.close()
    assert event_wait.SLOTS.active == 0  # nothing leaked past the requests


def _get_in_thread(
    client: TestClient, url: str, headers: dict
) -> tuple[threading.Thread, list]:
    box: list = []

    def run() -> None:
        started = time.monotonic()
        response = client.get(url, headers=headers)
        box.append((response, time.monotonic() - started))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def test_wait_zero_is_todays_answer(feed):
    client, baseline = feed["client"], feed["baseline"]
    plain = client.get(f"/events?after={baseline}", headers=ADMIN)
    zero = client.get(f"/events?after={baseline}&wait=0", headers=ADMIN)
    assert plain.status_code == 200 and zero.status_code == 200
    assert plain.json() == zero.json()
    assert zero.json()["wait"] is None
    assert zero.json()["events"] == [] and zero.json()["next_after"] == baseline


def test_wait_above_the_cap_is_refused_like_any_other_bound(feed):
    client, baseline = feed["client"], feed["baseline"]
    too_long = client.get(
        f"/events?after={baseline}&wait={event_wait.EVENT_WAIT_MAX_SECONDS + 1}",
        headers=ADMIN,
    )
    assert too_long.status_code == 422


def test_a_waiter_wakes_for_a_visible_event_written_by_another_connection(feed):
    client, baseline = feed["client"], feed["baseline"]
    thread, box = _get_in_thread(client, f"/events?after={baseline}&wait=5", ADMIN)
    time.sleep(0.25)
    assert not box  # still parked
    event_id = activity.record(
        feed["writer"],
        actor_id=1,
        verb="noted",
        target_kind="issue",
        target_id=feed["public_issue"],
    )
    thread.join(timeout=5)
    assert box, "the waiter never returned"
    response, took = box[0]
    assert response.status_code == 200, response.text
    body = response.json()
    assert [event["id"] for event in body["events"]] == [event_id]
    assert body["next_after"] == event_id and body["has_more"] is False
    assert body["wait"]["outcome"] == "event"
    assert body["wait"]["requested_seconds"] == 5
    assert 0 < body["wait"]["elapsed_seconds"] < 2 and took < 2


def test_a_waiter_does_not_wake_for_an_event_it_may_not_see(feed):
    client, baseline = feed["client"], feed["baseline"]
    agent_headers = feed["agent_headers"]
    thread, box = _get_in_thread(
        client, f"/events?after={baseline}&wait=1", agent_headers
    )
    time.sleep(0.2)
    hidden_event = activity.record(
        feed["writer"],
        actor_id=1,
        verb="noted",
        target_kind="issue",
        target_id=feed["hidden_issue"],
    )
    thread.join(timeout=5)
    assert box, "the waiter never returned"
    response, took = box[0]
    body = response.json()
    assert body["events"] == [] and body["next_after"] == baseline
    assert body["wait"]["outcome"] == "timeout"
    assert body["wait"]["elapsed_seconds"] >= 1 and took >= 1
    # Positive control: the same event does wake a waiter allowed to see it.
    control = client.get(f"/events?after={baseline}&wait=2", headers=ADMIN).json()
    assert [event["id"] for event in control["events"]] == [hidden_event]
    assert control["wait"]["outcome"] == "event"


def test_past_the_waiter_cap_the_answer_is_immediate_and_labelled(feed, monkeypatch):
    client, baseline = feed["client"], feed["baseline"]
    monkeypatch.setattr(event_wait, "SLOTS", event_wait.WaiterSlots(1))
    parked, parked_box = _get_in_thread(
        client, f"/events?after={baseline}&wait=5", ADMIN
    )
    time.sleep(0.25)
    assert event_wait.SLOTS.active == 1
    started = time.monotonic()
    degraded = client.get(f"/events?after={baseline}&wait=5", headers=ADMIN)
    took = time.monotonic() - started
    assert degraded.status_code == 200
    body = degraded.json()
    assert body["events"] == [] and body["next_after"] == baseline
    assert (
        body["wait"]["outcome"] == "capacity" and body["wait"]["elapsed_seconds"] == 0
    )
    assert took < 1
    activity.record(
        feed["writer"],
        actor_id=1,
        verb="noted",
        target_kind="issue",
        target_id=feed["public_issue"],
    )
    parked.join(timeout=5)
    assert parked_box and parked_box[0][0].json()["wait"]["outcome"] == "event"
    assert event_wait.SLOTS.active == 0


def test_the_mcp_tool_is_the_rest_call_and_keeps_its_bounds(feed):
    client, baseline = feed["client"], feed["baseline"]
    client.headers.update(feed["agent_headers"])
    api = AthenaClient(client=client)
    server = build_server(api, scopes={"read"})

    def call(name: str, **arguments):
        result = asyncio.run(server.call_tool(name, arguments))
        return json.loads(result[0].text)

    immediate = call("wait_for_events", after=baseline, wait=0)
    assert immediate == call("recent_events", after=baseline)
    with pytest.raises(ToolError):
        call(
            "wait_for_events",
            after=baseline,
            wait=event_wait.EVENT_WAIT_MAX_SECONDS + 1,
        )
    waited = call("wait_for_events", after=baseline, wait=1)
    assert waited["events"] == [] and waited["wait"]["outcome"] == "timeout"
