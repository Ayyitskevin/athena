"""An event carries its verdict, lease generation and handoff token as fields (F8).

WHY: an agent waiting on the feed learned that a seat claimed, renewed, yielded,
resumed, completed or let a lease lapse, and then had to parse a prose `detail`
string (`generation <g>; handoff <t>; ...`) to learn WHICH possession, WHICH handoff
and WHAT verdict (FLEET-7). Prose is for people; an agent branches on fields.

These tests hold, for each verb that records one:

* the event carries `generation`, `handoff_token` and `verdict` as fields, equal to
  the authoritative value the command answered with (the lease, the handoff, the
  report), not to something parsed from the prose;
* the prose `detail` is unchanged and names the same values;
* an event that records none of them has none of the keys, so it reads as before;
* the generation and handoff token go only to an account that could read them from
  the lease and the handoff themselves, that is, an account that can read the issue;
* imported history never acquires facts, and a recorded fact can never change.
"""

from __future__ import annotations

import sqlite3
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

from athena.core import activity, db

FACTS = ("generation", "handoff_token", "verdict")


class Desk:
    """An operator (admin, user 1) who assigns and delegates; a seat agent; a
    member who can read the public project; an outsider of the private one."""

    def __init__(self, app, db_file, client: TestClient, *, private: bool = False):
        self.app = app
        self.db_file = db_file
        self.client = client
        bootstrap(client)
        self.seat = onboard_agent(client, email="ada@e.com", name="Ada")
        self.member = add_user(client, "mia@e.com", name="Mia")
        body: dict[str, Any] = {"name": "Work", "key": "WRK"}
        if private:
            body["visibility"] = "private"
        made = client.post("/projects", json=body, headers=ADMIN)
        assert made.status_code == 201, made.text
        self.project_id = made.json()["id"]
        if private:
            added = client.post(
                f"/projects/{self.project_id}/members",
                json={"user_id": self.seat_id},
                headers=ADMIN,
            )
            assert added.status_code in (200, 201), added.text

    @property
    def seat_id(self) -> int:
        return self.seat["user"]["id"]

    def as_seat(self) -> dict[str, str]:
        return bearer(self.seat)

    def delegated_issue(self, title: str = "Work item") -> int:
        made = self.client.post(
            "/issues",
            json={"title": title, "project_id": self.project_id},
            headers=ADMIN,
        )
        assert made.status_code == 201, made.text
        issue_id = made.json()["id"]
        assigned = self.client.put(
            f"/issues/{issue_id}/assignee", json={"assignee_id": 1}, headers=ADMIN
        )
        assert assigned.status_code == 200, assigned.text
        delegated = self.client.post(
            f"/issues/{issue_id}/delegate",
            json={"user_id": self.seat_id},
            headers=ADMIN,
        )
        assert delegated.status_code == 201, delegated.text
        return issue_id

    def claim(self, issue_id: int, generation: str | None = None) -> dict:
        read = self.client.get(f"/issues/{issue_id}", headers=self.as_seat())
        assert read.status_code == 200, read.text
        body = {} if generation is None else {"generation": generation}
        claimed = self.client.post(
            f"/issues/{issue_id}/claim",
            json=body,
            headers={**self.as_seat(), "If-Match": read.headers["ETag"]},
        )
        assert claimed.status_code in (200, 201), claimed.text
        return claimed.json()

    def yield_claim(self, issue_id: int, generation: str) -> dict:
        answer = self.client.post(
            f"/issues/{issue_id}/yield",
            json={
                "generation": generation,
                "reason": "blocked",
                "attempted_work": "Reproduced the failure.",
                "evidence": ["the focused test fails"],
                "blocking_question": "Which behaviour is right?",
                "resume_instructions": "Answer the question, then rerun the test.",
            },
            headers=self.as_seat(),
        )
        assert answer.status_code == 201, answer.text
        return answer.json()

    def latest(self) -> int:
        conn = db.connect(self.db_file)
        try:
            return int(conn.execute("SELECT max(id) FROM activity").fetchone()[0])
        finally:
            conn.close()

    def feed(self, headers: dict[str, str], after: int = 0) -> list[dict]:
        answer = self.client.get(
            "/events", params={"after": after, "limit": 200}, headers=headers
        )
        assert answer.status_code == 200, answer.text
        return answer.json()["events"]

    def only(
        self, verb: str, after: int, headers: dict[str, str] | None = None
    ) -> dict:
        found = [
            event
            for event in self.feed(headers or self.as_seat(), after)
            if event["verb"] == verb
        ]
        assert len(found) == 1, (verb, found)
        return found[0]


@pytest.fixture
def desk(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        yield Desk(app, db_file, client)


def _facts(event: dict) -> dict:
    return {name: event[name] for name in FACTS if name in event}


# --- one test per verb that carries a fact ------------------------------------------


def test_a_claim_carries_its_generation(desk):
    before = desk.latest()
    issue_id = desk.delegated_issue()
    start = desk.latest()
    lease = desk.claim(issue_id)
    event = desk.only("claimed", start)
    assert _facts(event) == {"generation": lease["generation"]}
    assert event["detail"].startswith(f"generation {lease['generation']}; until ")
    # The issue's creation, assignment and delegation record no fact, and carry
    # none of the keys: those events read exactly as they did.
    others = [event for event in desk.feed(ADMIN, before) if event["id"] <= start]
    assert len(others) >= 3
    assert all(not set(FACTS) & set(other) for other in others)


def test_a_renewal_carries_the_generation_it_renewed(desk):
    issue_id = desk.delegated_issue()
    lease = desk.claim(issue_id)
    start = desk.latest()
    renewed = desk.claim(issue_id, generation=lease["generation"])
    assert renewed["generation"] == lease["generation"]
    event = desk.only("lease_renewed", start)
    assert _facts(event) == {"generation": lease["generation"]}


def test_a_yield_carries_its_generation_and_handoff_token(desk):
    issue_id = desk.delegated_issue()
    lease = desk.claim(issue_id)
    start = desk.latest()
    handoff = desk.yield_claim(issue_id, lease["generation"])
    event = desk.only("claim_yielded", start)
    assert _facts(event) == {
        "generation": lease["generation"],
        "handoff_token": handoff["handoff_token"],
    }
    assert event["detail"].startswith(
        f"generation {lease['generation']}; handoff {handoff['handoff_token']}; "
    )


def test_a_handoff_resume_carries_the_handoff_and_the_new_generation(desk):
    issue_id = desk.delegated_issue()
    first = desk.claim(issue_id)
    handoff = desk.yield_claim(issue_id, first["generation"])
    second = desk.claim(issue_id)
    assert second["generation"] != first["generation"]
    start = desk.latest()
    resumed = desk.client.post(
        f"/issues/{issue_id}/claim-handoffs/{handoff['handoff_token']}/resume",
        json={"generation": second["generation"]},
        headers=desk.as_seat(),
    )
    assert resumed.status_code in (200, 201), resumed.text
    event = desk.only("claim_handoff_resumed", start)
    assert _facts(event) == {
        "generation": second["generation"],
        "handoff_token": handoff["handoff_token"],
    }


@pytest.mark.parametrize(
    "verdict", ["PASS", "PASS-WITH-NOTES", "HOLD", "BLOCKED", None]
)
def test_a_completion_carries_its_generation_and_reported_verdict(desk, verdict):
    issue_id = desk.delegated_issue()
    lease = desk.claim(issue_id)
    start = desk.latest()
    body: dict[str, Any] = {"generation": lease["generation"]}
    if verdict is not None:
        body["verdict"] = verdict
    done = desk.client.post(
        f"/issues/{issue_id}/complete", json=body, headers=desk.as_seat()
    )
    assert done.status_code == 200, done.text
    event = desk.only("claim_completed", start)
    expected = {"generation": lease["generation"]}
    if verdict is not None:
        expected["verdict"] = verdict
        assert f'"verdict":"{verdict}"' in event["detail"]
    assert _facts(event) == expected


def test_a_lapse_carries_the_generation_that_lapsed(clock, desk):
    issue_id = desk.delegated_issue()
    lease = desk.claim(issue_id)
    start = desk.latest()
    lapse_lease(clock, desk.db_file, issue_id)
    # The operator is the assignee and delegated the work: its read observes.
    event = desk.only("lease_lapsed", start, headers=ADMIN)
    assert _facts(event) == {"generation": lease["generation"]}
    assert event["detail"].startswith(f"generation {lease['generation']}; holder_id ")


def test_a_decline_that_releases_a_lease_carries_that_generation(desk):
    issue_id = desk.delegated_issue()
    lease = desk.claim(issue_id)
    start = desk.latest()
    declined = desk.client.post(
        f"/issues/{issue_id}/decline",
        json={"generation": lease["generation"]},
        headers=desk.as_seat(),
    )
    assert declined.status_code == 200, declined.text
    event = desk.only("delegation_declined", start, headers=ADMIN)
    assert _facts(event) == {"generation": lease["generation"]}
    # A decline that releases nothing names no generation, and carries none.
    other = desk.delegated_issue("Never claimed")
    start = desk.latest()
    declined = desk.client.post(
        f"/issues/{other}/decline", json={}, headers=desk.as_seat()
    )
    assert declined.status_code == 200, declined.text
    assert _facts(desk.only("delegation_declined", start, headers=ADMIN)) == {}


# --- what does not change -------------------------------------------------------------


def test_the_fields_come_from_the_recorder_not_from_the_prose(desk):
    """The facts row is written by the recorder in the event's own transaction;
    rewriting the stored prose (which nothing in the product does) does not move
    the field."""
    issue_id = desk.delegated_issue()
    start = desk.latest()
    lease = desk.claim(issue_id)
    event = desk.only("claimed", start)
    conn = db.connect(desk.db_file)
    try:
        stored = conn.execute(
            "SELECT generation, handoff_token, verdict FROM activity_event_facts "
            "WHERE event_id = ?",
            (event["id"],),
        ).fetchone()
        assert tuple(stored) == (lease["generation"], None, None)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE activity_event_facts SET generation = ? WHERE event_id = ?",
                ("0" * 32, event["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM activity_event_facts WHERE event_id = ?", (event["id"],)
            )
    finally:
        conn.close()


def test_imported_history_never_carries_facts(desk):
    issue_id = desk.delegated_issue()
    conn = db.connect(desk.db_file)
    try:
        with pytest.raises(ValueError, match="imported history"):
            activity.record(
                conn,
                actor_id=1,
                verb="claimed",
                target_kind="issue",
                target_id=issue_id,
                detail="generation " + "a" * 32,
                imported_at="2026-01-01 00:00:00",
                facts={"generation": "a" * 32},
                commit=True,
            )
        imported = activity.record(
            conn,
            actor_id=1,
            verb="claimed",
            target_kind="issue",
            target_id=issue_id,
            detail="generation " + "a" * 32,
            imported_at="2026-01-01 00:00:00",
            commit=True,
        )
        with pytest.raises(sqlite3.IntegrityError, match="native activity event"):
            conn.execute(
                "INSERT INTO activity_event_facts (event_id, generation) VALUES (?, ?)",
                (imported["id"], "a" * 32),
            )
        with pytest.raises(ValueError, match="unknown event facts"):
            activity.record(
                conn,
                actor_id=1,
                verb="noted",
                target_kind="issue",
                target_id=issue_id,
                facts={"holder": "Ada"},
                commit=True,
            )
    finally:
        conn.close()


# --- who receives the generation and the handoff token ------------------------------


def test_the_handoff_token_goes_exactly_where_the_handoff_itself_goes(tmp_path):
    """A private project: the seat (a member) and the operator read the issue and
    so its lease and open handoff; an outsider reads neither, and no event of the
    issue, field or prose, reaches it."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        desk = Desk(app, db_file, client, private=True)
        issue_id = desk.delegated_issue()
        lease = desk.claim(issue_id)
        start = desk.latest()
        handoff = desk.yield_claim(issue_id, lease["generation"])
        token = handoff["handoff_token"]
        # The next claim holds the open handoff, which the lease read shows.
        desk.claim(issue_id)
        for headers in (desk.as_seat(), ADMIN):
            event = desk.only("claim_yielded", start, headers=headers)
            assert event["handoff_token"] == token
            assert event["generation"] == lease["generation"]
            lease_read = client.get(f"/issues/{issue_id}/lease", headers=headers)
            assert lease_read.status_code == 200, lease_read.text
            open_handoff = lease_read.json()["open_claim_handoff"]
            assert open_handoff["handoff_token"] == token
        outsider = as_user(desk.member["id"])
        lease_read = client.get(f"/issues/{issue_id}/lease", headers=outsider)
        assert lease_read.status_code == 404
        everything = client.get(
            "/events", params={"after": 0, "limit": 200}, headers=outsider
        )
        assert everything.status_code == 200
        assert not [
            event
            for event in everything.json()["events"]
            if event["target_kind"] == "issue" and event["target_id"] == issue_id
        ]
        assert token not in everything.text
        assert lease["generation"] not in everything.text


def test_the_issue_gate_on_the_fields_holds_even_if_an_event_slips_past(desk):
    """The route states the handoff's own rule where it hands the fields out: if
    an event on an issue the caller cannot read ever reached the batch, it would
    keep its prose-free row and lose the generation and the token."""
    from athena.core import events_api

    issue_id = desk.delegated_issue()
    lease = desk.claim(issue_id)
    handoff = desk.yield_claim(issue_id, lease["generation"])
    hidden = desk.client.post(
        "/projects",
        json={"name": "Hidden", "key": "HID", "visibility": "private"},
        headers=ADMIN,
    )
    assert hidden.status_code == 201, hidden.text
    moved = desk.client.put(
        f"/issues/{issue_id}/project",
        json={"project_id": hidden.json()["id"]},
        headers=ADMIN,
    )
    assert moved.status_code == 200, moved.text
    conn = db.connect(desk.db_file)
    try:
        rows = [
            row
            for row in activity.list_events(conn, limit=1000)
            if row["verb"] == "claim_yielded"
        ]
        member = {"id": desk.member["id"], "role": "member", "is_agent": False}
        shown = events_api._with_facts(conn, rows, member)  # noqa: SLF001
        admin = events_api._with_facts(conn, rows, {"id": 1, "role": "admin"})  # noqa: SLF001
    finally:
        conn.close()
    assert [_facts(row) for row in shown] == [{}]
    assert [_facts(row) for row in admin] == [
        {"generation": lease["generation"], "handoff_token": handoff["handoff_token"]}
    ]
