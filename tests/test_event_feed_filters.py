"""The event feed and its wait take `project_id` and `mine=true` (F8, FLEET-7).

WHY: `wait_for_events` was a long poll on the whole instance's feed. A conductor
waiting on its own outcome was woken by 19 events from two projects it had nothing
to do with, and each wake cost it a call and a page. These tests hold what replaces
that:

* `project_id` keeps the project's own events and those of the issues that were in
  it when they were recorded; `mine=true` keeps the events of the issues the caller
  is assigned, delegated or created, and those it delegated to another account
  (the accounts a lapsed lease is recorded for). Both only narrow: the visibility gate is the
  same, so a hidden project's events are never returned, counted (`has_more`) or used
  to wake a waiter, and a project the caller cannot see answers exactly as a project
  that does not exist;
* a filtered wait completes only on a matching event: an event in an unrelated
  project, or on someone else's issue, does not wake it;
* cursors stay monotonic and a filtered read never moves the cursor past an event it
  would have matched;
* the feed's observer still runs under a filter, so a conductor waiting on its
  project learns of a lapsed lease in one call;
* each filter reads through an index (query-plan assertions), and the unfiltered
  read's statement is unchanged;
* MCP's `wait_for_events` and `recent_events` take the same filters and answer what
  REST answers.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    add_user,
    as_user,
    bearer,
    bootstrap,
    lapse_lease,
    make_app,
    onboard_agent,
)

from athena.core import activity, db, event_wait, users
from athena.mcp.client import AthenaClient
from athena.mcp.server import build_server

# --- the cast -----------------------------------------------------------------


class Workspace:
    """An operator (admin, user 1); a conductor agent; a member who is not the
    conductor; projects Alpha (the conductor's), Beta (unrelated) and Secret
    (private, the conductor is no member)."""

    def __init__(self, app, db_file, client: TestClient):
        self.app = app
        self.db_file = db_file
        self.client = client
        bootstrap(client)
        self.conductor = onboard_agent(client, email="cora@e.com", name="Cora")
        self.seat = onboard_agent(client, email="ada@e.com", name="Ada")
        self.member = add_user(client, "mia@e.com", name="Mia")
        self.alpha = self.project("Alpha", "ALP")
        self.beta = self.project("Beta", "BET")
        self.secret = self.project("Secret", "SEC", private=True)

    @property
    def conductor_id(self) -> int:
        return self.conductor["user"]["id"]

    @property
    def seat_id(self) -> int:
        return self.seat["user"]["id"]

    def as_conductor(self) -> dict[str, str]:
        return bearer(self.conductor)

    def project(self, name: str, key: str, *, private: bool = False) -> int:
        body: dict[str, Any] = {"name": name, "key": key}
        if private:
            body["visibility"] = "private"
        made = self.client.post("/projects", json=body, headers=ADMIN)
        assert made.status_code == 201, made.text
        return made.json()["id"]

    def issue(
        self,
        title: str,
        project_id: int | None,
        headers: dict[str, str] | None = None,
    ) -> int:
        body: dict[str, Any] = {"title": title}
        if project_id is not None:
            body["project_id"] = project_id
        made = self.client.post("/issues", json=body, headers=headers or ADMIN)
        assert made.status_code == 201, made.text
        return made.json()["id"]

    def comment(self, issue_id: int, body: str = "progress") -> None:
        said = self.client.post(
            f"/issues/{issue_id}/comments", json={"body": body}, headers=ADMIN
        )
        assert said.status_code == 201, said.text

    def feed(self, headers: dict[str, str], after: int = 0, **params: Any) -> dict:
        answer = self.client.get(
            "/events", params={"after": after, "limit": 200, **params}, headers=headers
        )
        assert answer.status_code == 200, answer.text
        return answer.json()

    def latest(self) -> int:
        conn = db.connect(self.db_file)
        try:
            return int(conn.execute("SELECT max(id) FROM activity").fetchone()[0])
        finally:
            conn.close()

    def ungated(self) -> list[dict]:
        conn = db.connect(self.db_file)
        try:
            return activity.list_events(conn, limit=100_000)
        finally:
            conn.close()

    def scope_of(self, event_id: int) -> set[int]:
        """The projects whose access envelope the event carries (0042)."""
        conn = db.connect(self.db_file)
        try:
            rows = conn.execute(
                "SELECT p.id FROM activity_visibility_projects avp "
                "JOIN projects p ON p.activity_scope_key = avp.project_scope_key "
                "WHERE avp.event_id = ?",
                (event_id,),
            ).fetchall()
            return {int(row[0]) for row in rows}
        finally:
            conn.close()


@pytest.fixture
def ws(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        yield Workspace(app, db_file, client)


def _ids(feed: dict) -> list[int]:
    return [event["id"] for event in feed["events"]]


# --- what each filter keeps -------------------------------------------------------


def test_project_filter_keeps_that_projects_events_and_nothing_else(ws):
    start = ws.latest()
    alpha_one = ws.issue("Alpha one", ws.alpha)
    beta_one = ws.issue("Beta one", ws.beta)
    backlog = ws.issue("Backlog", None)
    ws.comment(alpha_one)
    ws.comment(beta_one)
    ws.comment(backlog)
    renamed = ws.client.patch(
        f"/projects/{ws.alpha}", json={"description": "the outcome"}, headers=ADMIN
    )
    assert renamed.status_code == 200, renamed.text
    # An issue that moves out of Alpha: the move names both projects, so it is
    # Alpha's event and Beta's; what happens to it afterwards is Beta's alone.
    mover = ws.issue("Mover", ws.alpha)
    moved = ws.client.put(
        f"/issues/{mover}/project", json={"project_id": ws.beta}, headers=ADMIN
    )
    assert moved.status_code == 200, moved.text
    ws.comment(mover, "after the move")

    everything = ws.feed(ws.as_conductor(), after=start)
    expected = [
        event["id"]
        for event in everything["events"]
        if ws.alpha in ws.scope_of(event["id"])
    ]
    alpha = ws.feed(ws.as_conductor(), after=start, project_id=ws.alpha)
    assert _ids(alpha) == expected
    assert expected, "the fixture recorded no Alpha events"
    targets = {(event["target_kind"], event["target_id"]) for event in alpha["events"]}
    assert ("issue", alpha_one) in targets and ("project", ws.alpha) in targets
    assert ("issue", beta_one) not in targets and ("issue", backlog) not in targets
    mover_verbs = [
        event["detail"] for event in alpha["events"] if event["target_id"] == mover
    ]
    assert all("after the move" not in detail for detail in mover_verbs)
    # The same events as the unfiltered read, byte for byte, only fewer.
    by_id = {event["id"]: event for event in everything["events"]}
    assert all(event == by_id[event["id"]] for event in alpha["events"])


def test_mine_keeps_the_issues_the_caller_is_assigned_delegated_or_created(ws):
    start = ws.latest()
    created = ws.issue("Filed by the conductor", ws.alpha, headers=ws.as_conductor())
    assigned = ws.issue("Assigned to the conductor", ws.beta)
    put = ws.client.put(
        f"/issues/{assigned}/assignee",
        json={"assignee_id": ws.conductor_id},
        headers=ADMIN,
    )
    assert put.status_code == 200, put.text
    delegated = ws.issue("Delegated to the conductor", None)
    handed = ws.client.post(
        f"/issues/{delegated}/delegate",
        json={"user_id": ws.conductor_id},
        headers=ADMIN,
    )
    assert handed.status_code == 201, handed.text
    unrelated = ws.issue("Someone else's", ws.alpha)
    for issue_id in (created, assigned, delegated, unrelated):
        ws.comment(issue_id)

    mine = ws.feed(ws.as_conductor(), after=start, mine="true")
    everything = ws.feed(ws.as_conductor(), after=start)
    own = {created, assigned, delegated}
    expected = [
        event["id"]
        for event in everything["events"]
        if event["target_kind"] == "issue" and event["target_id"] in own
    ]
    assert _ids(mine) == expected
    assert {event["target_id"] for event in mine["events"]} == own
    # mine=false is no filter at all.
    assert ws.feed(ws.as_conductor(), after=start, mine="false") == everything
    # Both filters narrow together.
    both = ws.feed(ws.as_conductor(), after=start, mine="true", project_id=ws.alpha)
    assert {event["target_id"] for event in both["events"]} == {created}


# --- visibility ---------------------------------------------------------------------


def test_a_hidden_project_reads_exactly_as_a_project_that_does_not_exist(ws):
    start = ws.latest()
    hidden = ws.issue("Secret work", ws.secret)
    for _ in range(3):
        ws.comment(hidden)
    # Visible work elsewhere: neither filter may answer with it.
    ws.comment(ws.issue("Alpha work", ws.alpha))
    missing = ws.secret + 1000
    for params in ({}, {"limit": 1}, {"mine": "true"}):
        secret = ws.client.get(
            "/events",
            params={"after": start, "project_id": ws.secret, **params},
            headers=ws.as_conductor(),
        )
        absent = ws.client.get(
            "/events",
            params={"after": start, "project_id": missing, **params},
            headers=ws.as_conductor(),
        )
        assert secret.status_code == absent.status_code == 200
        assert secret.content == absent.content
        assert secret.json() == {"events": [], "next_after": start, "has_more": False}
    # Positive control: the events exist, and the operator, who may see them, gets
    # them through the same filter.
    operator = ws.feed(ADMIN, after=start, project_id=ws.secret)
    assert {event["target_id"] for event in operator["events"]} == {hidden}
    assert len(operator["events"]) == 4


def test_a_hidden_project_is_answered_before_the_trail_is_read(ws):
    """A hidden project must cost what a missing one costs. Driving the envelope
    index for it and letting the gate reject each row took time in proportion
    to its events (9.5 ms against 0.013 ms at 20,000 events in review), which
    told a non-member that the project exists and how busy it is."""
    hidden = ws.issue("Secret work", ws.secret)
    ws.comment(hidden)
    conn = db.connect(ws.db_file)
    try:
        conductor = users.get_user(conn, ws.conductor_id)
        operator = users.get_user(conn, 1)
        for actor, project_id, reads_trail in (
            (conductor, ws.secret, False),
            (conductor, ws.secret + 1000, False),
            (None, ws.secret, False),
            (conductor, ws.alpha, True),
            (operator, ws.secret, True),
        ):
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            try:
                rows = activity.list_events(
                    conn, project_id=project_id, limit=10, actor=actor
                )
            finally:
                conn.set_trace_callback(None)
            read = any("FROM activity" in sql for sql in statements)
            assert read is reads_trail, (project_id, statements)
            if not reads_trail:
                assert rows == []
    finally:
        conn.close()


def test_a_project_filter_never_widens_what_the_gate_admits(ws):
    """An issue moved from Secret into Alpha keeps Secret in the envelope of the
    events recorded while it was there: an Alpha filter does not show them to an
    account that cannot see Secret."""
    start = ws.latest()
    issue_id = ws.issue("Born secret", ws.secret)
    ws.comment(issue_id)
    while_secret = ws.latest()
    moved = ws.client.put(
        f"/issues/{issue_id}/project", json={"project_id": ws.alpha}, headers=ADMIN
    )
    assert moved.status_code == 200, moved.text
    the_move = ws.latest()
    ws.comment(issue_id)
    in_alpha = ws.latest()
    assert {ws.alpha, ws.secret} <= ws.scope_of(the_move)
    alpha = ws.feed(ws.as_conductor(), after=start, project_id=ws.alpha)
    assert _ids(alpha) == [in_alpha]
    # The operator, who sees Secret, gets the move and the later comment.
    operator = ws.feed(ADMIN, after=start, project_id=ws.alpha)
    assert _ids(operator) == [the_move, in_alpha]
    assert while_secret not in _ids(operator)


def test_project_id_out_of_range_is_refused_like_any_other_bound(ws):
    for value in ("0", "-1", "notanumber", str(2**63)):
        answer = ws.client.get(
            "/events", params={"project_id": value}, headers=ws.as_conductor()
        )
        assert answer.status_code == 422, (value, answer.text)
        assert answer.json()["code"] == "validation_error"


# --- cursors -------------------------------------------------------------------------


def test_a_filtered_read_never_moves_the_cursor_past_an_event_it_would_match(ws):
    start = ws.latest()
    alpha_issue = ws.issue("Alpha", ws.alpha)
    beta_issue = ws.issue("Beta", ws.beta)
    for _ in range(3):
        ws.comment(alpha_issue)
        ws.comment(beta_issue)
    everything = ws.feed(ADMIN, after=start)
    expected = [
        event["id"]
        for event in everything["events"]
        if event["target_kind"] == "issue" and event["target_id"] == alpha_issue
    ]
    assert len(expected) == 4  # created + three comments
    # One at a time: each page is the next matching event, and next_after is that
    # event's id, never a later non-matching one.
    cursor, seen = start, []
    while True:
        page = ws.feed(ws.as_conductor(), after=cursor, project_id=ws.alpha, limit=1)
        if not page["events"]:
            assert page["next_after"] == cursor and page["has_more"] is False
            break
        assert page["next_after"] == page["events"][-1]["id"] > cursor
        seen.extend(_ids(page))
        cursor = page["next_after"]
        assert page["has_more"] is (seen != expected)
    assert seen == expected
    # Caught up while later unrelated events exist: the cursor stays where it was,
    # not at the newest event, so a later Alpha event is not skipped.
    ws.comment(beta_issue)
    caught_up = ws.feed(ws.as_conductor(), after=cursor, project_id=ws.alpha)
    assert caught_up == {"events": [], "next_after": cursor, "has_more": False}
    ws.comment(alpha_issue)
    later = ws.feed(ws.as_conductor(), after=cursor, project_id=ws.alpha)
    assert _ids(later) == [ws.latest()]


# --- waiting -------------------------------------------------------------------------


@pytest.fixture
def fast_waits(monkeypatch):
    monkeypatch.setattr(event_wait, "EVENT_WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(event_wait, "SLOTS", event_wait.WaiterSlots(4))
    yield
    assert event_wait.SLOTS.active == 0


@pytest.fixture
def parked(monkeypatch, fast_waits) -> threading.Event:
    """Set once a waiter has polled, found nothing, and gone to sleep."""
    signal = threading.Event()
    real_wait = event_wait.wait_for_events

    async def sleep_parked(seconds: float) -> None:
        signal.set()
        await asyncio.sleep(seconds)

    async def wait_for_events(poll, **options):
        return await real_wait(poll, **options, sleep=sleep_parked)

    monkeypatch.setattr(event_wait, "wait_for_events", wait_for_events)
    return signal


def _wait_in_thread(ws: Workspace, params: dict, headers: dict):
    box: list = []

    def run() -> None:
        started = time.monotonic()
        answer = ws.client.get("/events", params=params, headers=headers)
        box.append((answer, time.monotonic() - started))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _record(ws: Workspace, issue_id: int, detail: str = "") -> int:
    """An event written by another connection while the waiter is parked."""
    conn = db.connect(ws.db_file)
    try:
        return activity.record(
            conn,
            actor_id=1,
            verb="noted",
            target_kind="issue",
            target_id=issue_id,
            detail=detail,
            commit=True,
        )["id"]
    finally:
        conn.close()


def _parked_answer(parked: threading.Event, thread, box) -> tuple[dict, float]:
    thread.join(timeout=10)
    assert box, "the waiter never returned"
    answer, took = box[0]
    assert answer.status_code == 200, answer.text
    return answer.json(), took


def test_a_project_filtered_wait_is_not_woken_by_an_unrelated_project(ws, parked):
    alpha_issue = ws.issue("Alpha", ws.alpha)
    beta_issue = ws.issue("Beta", ws.beta)
    start = ws.latest()
    thread, box = _wait_in_thread(
        ws, {"after": start, "wait": 1, "project_id": ws.alpha}, ws.as_conductor()
    )
    assert parked.wait(timeout=5), "the waiter never parked"
    for _ in range(3):
        _record(ws, beta_issue)
    body, took = _parked_answer(parked, thread, box)
    assert body["events"] == [] and body["next_after"] == start
    assert body["wait"]["outcome"] == "timeout"
    assert body["wait"]["elapsed_seconds"] >= 1 and took >= 1
    # Positive control: an Alpha event does wake the same filtered wait.
    parked.clear()
    thread, box = _wait_in_thread(
        ws, {"after": start, "wait": 5, "project_id": ws.alpha}, ws.as_conductor()
    )
    assert parked.wait(timeout=5), "the waiter never parked"
    woke = _record(ws, alpha_issue)
    body, took = _parked_answer(parked, thread, box)
    assert _ids(body) == [woke] and body["wait"]["outcome"] == "event"
    assert took < 4


def test_a_mine_wait_is_not_woken_by_someone_elses_issue(ws, parked):
    own = ws.issue("Mine", ws.alpha, headers=ws.as_conductor())
    other = ws.issue("Not mine", ws.alpha)
    start = ws.latest()
    thread, box = _wait_in_thread(
        ws, {"after": start, "wait": 1, "mine": "true"}, ws.as_conductor()
    )
    assert parked.wait(timeout=5), "the waiter never parked"
    _record(ws, other)
    body, _took = _parked_answer(parked, thread, box)
    assert body["events"] == [] and body["wait"]["outcome"] == "timeout"
    parked.clear()
    thread, box = _wait_in_thread(
        ws, {"after": start, "wait": 5, "mine": "true"}, ws.as_conductor()
    )
    assert parked.wait(timeout=5), "the waiter never parked"
    woke = _record(ws, own)
    body, _took = _parked_answer(parked, thread, box)
    assert _ids(body) == [woke] and body["wait"]["outcome"] == "event"


def test_a_hidden_projects_event_does_not_wake_a_wait_filtered_on_it(ws, parked):
    hidden = ws.issue("Secret work", ws.secret)
    elsewhere = ws.issue("Beta work", ws.beta)
    start = ws.latest()
    thread, box = _wait_in_thread(
        ws, {"after": start, "wait": 1, "project_id": ws.secret}, ws.as_conductor()
    )
    assert parked.wait(timeout=5), "the waiter never parked"
    _record(ws, hidden)
    _record(ws, elsewhere)  # visible to the waiter, and not what it asked about
    body, took = _parked_answer(parked, thread, box)
    assert body["events"] == [] and body["next_after"] == start
    assert body["wait"]["outcome"] == "timeout" and took >= 1
    # The operator's identical wait is answered by the hidden event alone.
    control = ws.feed(ADMIN, after=start, project_id=ws.secret, wait=2)
    assert [event["target_id"] for event in control["events"]] == [hidden]
    assert control["wait"]["outcome"] == "event"


def test_the_lapse_observer_still_answers_a_filtered_wait(clock, ws, fast_waits):
    """The conductor files and delegates the work; the seat claims it and goes
    away. One project-filtered call, and one mine call, each learn of the lapse."""
    issue_id = ws.issue("Abandoned", ws.alpha, headers=ws.as_conductor())
    delegated = ws.client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": ws.seat_id},
        headers=ws.as_conductor(),
    )
    assert delegated.status_code == 201, delegated.text
    read = ws.client.get(f"/issues/{issue_id}", headers=bearer(ws.seat))
    claimed = ws.client.post(
        f"/issues/{issue_id}/claim",
        headers={**bearer(ws.seat), "If-Match": read.headers["ETag"]},
    )
    assert claimed.status_code == 201, claimed.text
    start = ws.latest()
    lapse_lease(clock, ws.db_file, issue_id)
    by_project = ws.feed(ws.as_conductor(), after=start, project_id=ws.alpha, wait=1)
    assert [event["verb"] for event in by_project["events"]] == ["lease_lapsed"]
    assert by_project["wait"]["outcome"] == "event"
    by_mine = ws.feed(ws.as_conductor(), after=start, mine="true")
    assert _ids(by_mine) == _ids(by_project)
    # An unrelated project's filter neither shows it nor is woken by it.
    beta = ws.feed(ws.as_conductor(), after=start, project_id=ws.beta, wait=1)
    assert beta["events"] == [] and beta["wait"]["outcome"] == "timeout"


def test_a_mine_wait_returns_the_lapse_the_delegating_account_answers_for(
    clock, ws, fast_waits
):
    """The operator files the work and assigns it to the conductor; the conductor
    delegates it to a seat, which claims; the operator names another owner; the
    lease lapses. The conductor is neither assignee, delegate nor creator, but it
    delegated the lease's holder, so the observer records the lapse for it. Its
    `mine` wait recorded that event and then hid it, sleeping to the timeout."""
    issue_id = ws.issue("Handed down", ws.alpha)
    for assignee in (ws.conductor_id,):
        put = ws.client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": assignee},
            headers=ADMIN,
        )
        assert put.status_code == 200, put.text
    delegated = ws.client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": ws.seat_id},
        headers=ws.as_conductor(),
    )
    assert delegated.status_code == 201, delegated.text
    read = ws.client.get(f"/issues/{issue_id}", headers=bearer(ws.seat))
    claimed = ws.client.post(
        f"/issues/{issue_id}/claim",
        headers={**bearer(ws.seat), "If-Match": read.headers["ETag"]},
    )
    assert claimed.status_code == 201, claimed.text
    put = ws.client.put(
        f"/issues/{issue_id}/assignee",
        json={"assignee_id": ws.member["id"]},
        headers=ADMIN,
    )
    assert put.status_code == 200, put.text
    start = ws.latest()
    lapse_lease(clock, ws.db_file, issue_id)
    by_mine = ws.feed(ws.as_conductor(), after=start, mine="true", wait=1)
    assert [event["verb"] for event in by_mine["events"]] == ["lease_lapsed"]
    assert by_mine["wait"]["outcome"] == "event"
    # The seat is the delegate, so the same event is its own too; an account
    # with no part in the issue gets nothing.
    assert _ids(ws.feed(bearer(ws.seat), after=start, mine="true")) == _ids(by_mine)
    other = onboard_agent(ws.client, email="bo@e.com", name="Bo")
    assert ws.feed(bearer(other), after=start, mine="true")["events"] == []


# --- MCP ---------------------------------------------------------------------------


def test_the_mcp_tools_take_the_filters_and_answer_what_rest_answers(ws, fast_waits):
    start = ws.latest()
    own = ws.issue("Mine", ws.alpha, headers=ws.as_conductor())
    ws.comment(own)
    ws.comment(ws.issue("Beta", ws.beta))
    api = AthenaClient(client=TestClient(ws.app, headers=ws.as_conductor()))
    server = build_server(api, scopes={"read", "issue:write"})

    def call(name: str, **arguments: Any) -> dict:
        result = asyncio.run(server.call_tool(name, arguments))
        return json.loads(result[0][0].text)

    for filters in ({"project_id": ws.alpha}, {"mine": True}):
        rest = ws.feed(
            ws.as_conductor(),
            after=start,
            limit=50,
            **{
                key: "true" if value is True else value
                for key, value in filters.items()
            },
        )
        assert call("recent_events", after=start, **filters) == rest
        assert call("wait_for_events", after=start, wait=0, **filters) == rest
        assert {event["target_id"] for event in rest["events"]} == {own}
    waited = call("wait_for_events", after=ws.latest(), wait=1, project_id=ws.beta)
    assert waited["events"] == [] and waited["wait"]["outcome"] == "timeout"


# --- query plans --------------------------------------------------------------------


def _plans(db_file, actor_id: int, **filters: Any) -> tuple[str, list[str]]:
    """The statement the feed runs for this actor and these filters, and its plan."""
    conn = db.connect(db_file)
    try:
        actor = users.get_user(conn, actor_id)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        activity.list_events(conn, after_id=1, limit=51, actor=actor, **filters)
        conn.set_trace_callback(None)
        feed_sql = [
            sql for sql in statements if "FROM activity" in sql or "scope_filter" in sql
        ]
        assert len(feed_sql) == 1, statements
        plan = [
            row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + feed_sql[0])
        ]
        return feed_sql[0], plan
    finally:
        conn.close()


def _no_scan_of_history(plan: list[str]) -> None:
    scans = [
        step
        for step in plan
        if step.startswith("SCAN ") and not step.startswith("SCAN (subquery")
    ]
    assert not scans, plan


@pytest.mark.parametrize("who", ["admin", "member"])
def test_the_project_filter_seeks_the_envelope_index(ws, who):
    ws.comment(ws.issue("Alpha", ws.alpha))
    actor_id = 1 if who == "admin" else ws.member["id"]
    _sql, plan = _plans(ws.db_file, actor_id, project_id=ws.alpha)
    _no_scan_of_history(plan)
    drivers = [step for step in plan if step.startswith("SEARCH scope_filter ")]
    assert drivers and all(
        step == "SEARCH scope_filter USING COVERING INDEX "
        "idx_activity_visibility_projects_project (project_scope_key=? AND event_id>?)"
        for step in drivers
    ), plan
    rows = [step for step in plan if step.startswith("SEARCH a ")]
    assert rows and all(
        step == "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)" for step in rows
    ), plan
    # One driven arm per kind that can carry a project (issue, project): the page
    # and space arms are not asked at all.
    assert len(drivers) == (1 if who == "admin" else 2), plan


@pytest.mark.parametrize("who", ["admin", "member"])
def test_the_mine_filter_seeks_each_index(ws, who):
    ws.comment(ws.issue("Mine", ws.alpha, headers=ws.as_conductor()))
    actor_id = 1 if who == "admin" else ws.member["id"]
    _sql, plan = _plans(ws.db_file, actor_id, mine_of=ws.conductor_id)
    _no_scan_of_history(plan)
    rows = [step for step in plan if step.startswith("SEARCH a ")]
    assert rows == [
        "SEARCH a USING INDEX idx_activity_target "
        "(target_kind=? AND target_id=? AND id>?)"
    ], plan
    for expected in (
        "SEARCH mine_assigned USING COVERING INDEX idx_issues_assignee (assignee_id=?)",
        "SEARCH mine_delegated USING COVERING INDEX idx_issue_contributors_user "
        "(user_id=?)",
        "SEARCH mine_gave USING COVERING INDEX idx_issue_contributors_added_by "
        "(added_by=?)",
        "SEARCH mine_created USING COVERING INDEX idx_issues_created_by (created_by=?)",
    ):
        assert expected in plan, plan


def test_a_filter_adds_nothing_to_the_read_that_asks_for_none(ws):
    """The unfiltered statement is the one 0076 built, byte for byte: the per-arm
    merge walking idx_activity_kind_id for a member, the plain rowid walk for the
    operator. And mine=false is no filter: the same bytes as no parameter."""
    ws.comment(ws.issue("Alpha", ws.alpha))
    sql, plan = _plans(ws.db_file, ws.member["id"])
    assert "scope_filter" not in sql and "INDEXED BY" not in sql
    arm_rows = [step for step in plan if step.startswith("SEARCH a ")]
    assert (
        arm_rows
        == ["SEARCH a USING INDEX idx_activity_kind_id (target_kind=? AND id>?)"] * 4
    ), plan
    admin_sql, admin_plan = _plans(ws.db_file, 1)
    assert admin_sql == (
        "SELECT a.id, a.actor_id, a.verb, a.target_kind, a.target_id, a.detail, "
        "a.created_at, a.run_id, a.parent_run_id, a.forked_from_event_id, "
        "a.imported_at, a.reverses_event_id, u.name AS actor_name FROM activity a "
        "JOIN users u ON u.id = a.actor_id WHERE a.id > 1 ORDER BY a.id ASC LIMIT 51"
    ), admin_sql
    assert admin_plan[0] == "SEARCH a USING INTEGER PRIMARY KEY (rowid>?)", admin_plan
    plain = ws.client.get("/events", params={"after": 0}, headers=ws.as_conductor())
    explicit = ws.client.get(
        "/events", params={"after": 0, "mine": "false"}, headers=ws.as_conductor()
    )
    assert plain.content == explicit.content
    # Positive control, so the test cannot pass where the parameter is ignored:
    # mine=true is a filter, and the conductor owns none of these issues.
    mine = ws.client.get(
        "/events", params={"after": 0, "mine": "true"}, headers=ws.as_conductor()
    )
    assert mine.json()["events"] == [] and plain.json()["events"]


def test_the_filters_read_an_empty_database(tmp_path):
    """`INDEXED BY` fails the statement outright if SQLite cannot use the index;
    the filtered reads must work on a database with no issues and no events."""
    conn = db.connect(tmp_path / "empty.db")
    try:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO users (email, name, role) VALUES ('a@e', 'A', 'member')"
        )
        conn.commit()
        actor = users.get_user(conn, 1)
        assert activity.list_events(conn, mine_of=1, actor=actor) == []
        assert activity.list_events(conn, mine_of=1) == []
        assert activity.list_events(conn, project_id=1, actor=actor) == []
        assert activity.list_events(conn, project_id=1, mine_of=1) == []
    finally:
        conn.close()


def test_a_human_member_gets_the_same_filters(ws):
    issue_id = ws.issue("Alpha", ws.alpha)
    ws.issue("Beta", ws.beta)
    feed = ws.feed(as_user(ws.member["id"]), after=0, project_id=ws.alpha)
    assert {
        event["target_id"]
        for event in feed["events"]
        if event["target_kind"] == "issue"
    } == {issue_id}
