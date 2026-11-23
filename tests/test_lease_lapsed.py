"""A lapsed lease is an event (C4, decision D11).

WHY: a lease that lapses with nobody settling it was a fact only the clock knew.
The read paths derived ``lease_expired`` and showed it to the holder and to
admins, and nobody else: a conductor that delegated the work, waiting on the
event feed, saw nothing for as long as it waited (FLEET-2). These tests hold the
behaviour that replaces that silence:

* the first request that observes an expired, unsettled lease on an issue its
  account is accountable for (the assignee, or the account that delegated the
  work to the holder) records ONE ``lease_lapsed`` event for that lease
  generation; a second observer, racing or later, records nothing;
* a lease that is reacquired and lapses again under a new generation is a new
  lapse; a lease settled before anyone observed it is no lapse at all;
* the assignee and the delegating account are notified, even when one of them
  is the observer, and both see an attention row for the claim;
* an account that cannot see the issue gets no event, notification or attention
  row, and neither a count nor a cursor tells it the lapse happened;
* automation never fires on the verb, and the verb does not reset a schedule's
  inactivity clock;
* the observing read never holds SQLite's writer lock while it reads: the
  lapse is written after the read, in its own immediate transaction.

Nothing here releases or sweeps a lease (ruling K1): the row stays until a
claimer or an operator settles it as before. The app has no injectable clock,
so a lease lapses by rewriting its stored expiry (``lapse_lease``).
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    add_user,
    as_user,
    bearer,
    bootstrap,
    events,
    lapse_lease,
    make_app,
    onboard_agent,
)

from athena.core import db

VERB = "lease_lapsed"


# --- the cast -----------------------------------------------------------------------


class Board:
    """An operator (admin, user 1), a conductor agent that delegates, a seat agent
    that holds, a human assignee and an unrelated member who can see everything."""

    def __init__(self, app, db_file, client: TestClient, *, private: bool = False):
        self.app = app
        self.db_file = db_file
        self.client = client
        bootstrap(client)
        body: dict[str, Any] = {"name": "Work", "key": "WRK"}
        if private:
            body["visibility"] = "private"
        project = client.post("/projects", json=body, headers=ADMIN)
        assert project.status_code == 201, project.text
        self.project_id = project.json()["id"]
        self.conductor = onboard_agent(client, email="cora@e.com", name="Cora")
        self.seat = onboard_agent(client, email="ada@e.com", name="Ada")
        self.assignee = add_user(client, "mia@e.com", name="Mia")
        self.bystander = add_user(client, "bob@e.com", name="Bob")
        if private:
            for user_id in (
                self.conductor_id,
                self.seat_id,
                self.assignee["id"],
                self.bystander["id"],
            ):
                added = client.post(
                    f"/projects/{self.project_id}/members",
                    json={"user_id": user_id},
                    headers=ADMIN,
                )
                assert added.status_code in (200, 201), added.text

    @property
    def conductor_id(self) -> int:
        return self.conductor["user"]["id"]

    @property
    def seat_id(self) -> int:
        return self.seat["user"]["id"]

    def as_conductor(self) -> dict[str, str]:
        return bearer(self.conductor)

    def as_seat(self) -> dict[str, str]:
        return bearer(self.seat)

    def as_assignee(self) -> dict[str, str]:
        return as_user(self.assignee["id"])

    def as_bystander(self) -> dict[str, str]:
        return as_user(self.bystander["id"])

    def delegated_issue(self, title: str = "Abandoned work") -> int:
        """Filed by the conductor, assigned to Mia by the operator, delegated to
        Ada BY the conductor, and claimed by Ada."""
        made = self.client.post(
            "/issues",
            json={"title": title, "project_id": self.project_id},
            headers=self.as_conductor(),
        )
        assert made.status_code == 201, made.text
        issue_id = made.json()["id"]
        assigned = self.client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": self.assignee["id"]},
            headers=ADMIN,
        )
        assert assigned.status_code == 200, assigned.text
        delegated = self.client.post(
            f"/issues/{issue_id}/delegate",
            json={"user_id": self.seat_id},
            headers=self.as_conductor(),
        )
        assert delegated.status_code == 201, delegated.text
        self.claim(issue_id)
        return issue_id

    def claim(self, issue_id: int, headers: dict[str, str] | None = None) -> dict:
        headers = headers or self.as_seat()
        read = self.client.get(f"/issues/{issue_id}", headers=headers)
        assert read.status_code == 200, read.text
        claimed = self.client.post(
            f"/issues/{issue_id}/claim",
            headers={**headers, "If-Match": read.headers["ETag"]},
        )
        assert claimed.status_code in (200, 201), claimed.text
        return claimed.json()

    def generation(self, issue_id: int) -> str:
        lease = self.client.get(f"/issues/{issue_id}/lease", headers=ADMIN).json()
        return lease["generation"]

    def lapse(self, issue_id: int) -> None:
        lapse_lease(self.db_file, issue_id)

    def lapses(self, issue_id: int) -> list[dict]:
        """Every lease_lapsed row on the issue, ungated, oldest first."""
        return [
            row
            for row in reversed(events(self.db_file, VERB))
            if row["target_kind"] == "issue" and row["target_id"] == issue_id
        ]

    def feed(self, headers: dict[str, str], after: int = 0, **params: Any) -> dict:
        answer = self.client.get(
            "/events", params={"after": after, "limit": 200, **params}, headers=headers
        )
        assert answer.status_code == 200, answer.text
        return answer.json()

    def inbox(self, headers: dict[str, str]) -> list[dict]:
        answer = self.client.get(
            "/notifications", params={"unread": True, "limit": 100}, headers=headers
        )
        assert answer.status_code == 200, answer.text
        return answer.json()

    def unread(self, headers: dict[str, str]) -> int:
        answer = self.client.get("/notifications/unread_count", headers=headers)
        assert answer.status_code == 200, answer.text
        return answer.json()["count"]

    def ranking(self, headers: dict[str, str]) -> dict:
        answer = self.client.get(
            "/attention/ranking",
            params={"signals": "claim_needs_attention", "limit": 100},
            headers=headers,
        )
        assert answer.status_code == 200, answer.text
        return answer.json()

    def user(self, user_id: int) -> dict:
        conn = db.connect(self.db_file)
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return dict(row)
        finally:
            conn.close()


def lapsed_in(feed: dict, issue_id: int) -> list[dict]:
    return [
        event
        for event in feed["events"]
        if event["verb"] == VERB and event["target_id"] == issue_id
    ]


def lapsed_rows(rows: list[dict], issue_id: int) -> list[dict]:
    return [row for row in rows if row["verb"] == VERB and row["target_id"] == issue_id]


def issue_rows(ranking: dict, issue_id: int) -> list[dict]:
    return [
        row
        for row in ranking["items"]
        if row["source_kind"] == "issue" and row["source_id"] == issue_id
    ]


@pytest.fixture
def board(tmp_path):
    app, db_file = make_app(tmp_path, "lapse.db")
    with TestClient(app) as client:
        yield Board(app, db_file, client)


@pytest.fixture
def private_board(tmp_path):
    app, db_file = make_app(tmp_path, "lapse-private.db")
    with TestClient(app) as client:
        yield Board(app, db_file, client, private=True)


# --- the observer in wait_for_events (GET /events) ------------------------------------


def test_the_delegating_conductor_learns_of_a_lapse_in_one_feed_read(board):
    issue_id = board.delegated_issue()
    generation = board.generation(issue_id)
    cursor = board.feed(board.as_conductor())["next_after"]
    board.lapse(issue_id)

    page = board.feed(board.as_conductor(), after=cursor)

    [event] = lapsed_in(page, issue_id)
    # The observer is the actor; the holder is the subject, named in the detail
    # beside the generation that lapsed.
    assert event["actor_id"] == board.conductor_id
    assert event["detail"].startswith(f"generation {generation}; ")
    assert f"; holder_id {board.seat_id};" in event["detail"]
    assert event["imported_at"] is None
    # The lapse is recorded, not swept (K1): the lease row is still there.
    lease = board.client.get(f"/issues/{issue_id}/lease", headers=ADMIN).json()
    assert lease["generation"] == generation and lease["active"] is False


def test_a_parked_wait_answers_with_the_lapse_at_once(board):
    issue_id = board.delegated_issue()
    cursor = board.feed(board.as_conductor())["next_after"]
    board.lapse(issue_id)

    page = board.feed(board.as_conductor(), after=cursor, wait=5)

    assert len(lapsed_in(page, issue_id)) == 1
    assert page["wait"]["outcome"] == "event"


def test_the_assignee_is_an_observer_too(board):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)

    page = board.feed(board.as_assignee())

    [event] = lapsed_in(page, issue_id)
    assert event["actor_id"] == board.assignee["id"]


def test_a_second_observer_records_nothing(board):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)

    board.feed(board.as_conductor())
    board.feed(board.as_assignee())
    board.feed(board.as_conductor())

    assert len(board.lapses(issue_id)) == 1


def test_an_account_not_accountable_for_the_lease_observes_nothing(board):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)

    # The bystander can read the issue but neither owns nor delegated it; the
    # holder's own reads are not an observer here either.
    board.feed(board.as_bystander())
    board.feed(board.as_seat())

    assert board.lapses(issue_id) == []


def test_a_live_lease_is_no_lapse(board):
    issue_id = board.delegated_issue()

    board.feed(board.as_conductor())

    assert board.lapses(issue_id) == []


def test_a_lease_settled_before_anyone_observed_it_records_nothing(board):
    settled = board.delegated_issue("Settled in time")
    generation = board.generation(settled)
    done = board.client.post(
        f"/issues/{settled}/complete",
        json={"generation": generation},
        headers=board.as_seat(),
    )
    assert done.status_code == 200, done.text
    # The holder clears its own lapsed row: also a settlement.
    cleared = board.delegated_issue("Cleared by its holder")
    board.lapse(cleared)
    released = board.client.post(
        f"/issues/{cleared}/complete", json={}, headers=board.as_seat()
    )
    assert released.status_code == 200, released.text
    assert released.json()["lapsed"] is True

    board.feed(board.as_conductor())

    assert board.lapses(settled) == [] and board.lapses(cleared) == []


def test_finished_or_archived_work_does_not_lapse(board):
    finished = board.delegated_issue("Finished")
    closed = board.client.patch(
        f"/issues/{finished}", json={"status": "done"}, headers=ADMIN
    )
    assert closed.status_code == 200, closed.text
    archived = board.delegated_issue("Archived")
    gone = board.client.post(f"/issues/{archived}/archive", headers=ADMIN)
    assert gone.status_code in (200, 204), gone.text
    board.lapse(finished)
    board.lapse(archived)

    board.feed(board.as_conductor())

    assert board.lapses(finished) == [] and board.lapses(archived) == []


def test_a_reacquired_lease_that_lapses_again_is_a_new_lapse(board):
    issue_id = board.delegated_issue()
    first = board.generation(issue_id)
    board.lapse(issue_id)
    board.feed(board.as_conductor())
    # Ada takes the work back: a fresh possession, a new generation.
    board.claim(issue_id)
    second = board.generation(issue_id)
    assert second != first
    board.feed(board.as_conductor())  # live again: nothing new
    assert len(board.lapses(issue_id)) == 1
    board.lapse(issue_id)

    board.feed(board.as_conductor())
    board.feed(board.as_conductor())

    recorded = board.lapses(issue_id)
    assert [row["detail"].split(";")[0] for row in recorded] == [
        f"generation {first}",
        f"generation {second}",
    ]


def test_a_renewal_keeps_the_generation_so_its_lapse_is_one_event(board):
    issue_id = board.delegated_issue()
    generation = board.generation(issue_id)
    read = board.client.get(f"/issues/{issue_id}", headers=board.as_seat())
    renewed = board.client.post(
        f"/issues/{issue_id}/claim",
        json={"generation": generation},
        headers={**board.as_seat(), "If-Match": read.headers["ETag"]},
    )
    assert renewed.status_code in (200, 201), renewed.text
    board.lapse(issue_id)

    board.feed(board.as_conductor())
    board.feed(board.as_assignee())

    assert len(board.lapses(issue_id)) == 1


# --- the observer in the attention ranking (GET /attention/ranking) --------------------


def test_the_attention_read_shows_the_lapse_and_records_it_after_reading(board):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)

    ranking = board.ranking(board.as_conductor())

    [row] = issue_rows(ranking, issue_id)
    assert row["signal"] == "claim_needs_attention"
    assert "lease_expired" in row["reason"]
    assert row["owner_id"] == board.seat_id
    assert len(board.lapses(issue_id)) == 1
    board.ranking(board.as_assignee())
    assert len(board.lapses(issue_id)) == 1


def test_the_assignee_sees_an_attention_row_for_the_claim(board):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)

    assert issue_rows(board.ranking(board.as_assignee()), issue_id)
    # Someone who is neither the holder, the assignee nor the delegator does not.
    assert issue_rows(board.ranking(board.as_bystander()), issue_id) == []


# --- who is notified --------------------------------------------------------------------


def test_the_assignee_and_the_delegating_account_are_notified(board):
    issue_id = board.delegated_issue()
    before = board.unread(board.as_bystander())
    board.lapse(issue_id)

    # The conductor observes: it is the event's actor and still gets the
    # notification, because it did not cause the lapse.
    board.feed(board.as_conductor())

    assert len(lapsed_rows(board.inbox(board.as_conductor()), issue_id)) == 1
    assert len(lapsed_rows(board.inbox(board.as_assignee()), issue_id)) == 1
    # An account that is not accountable and does not watch is not notified.
    assert lapsed_rows(board.inbox(board.as_bystander()), issue_id) == []
    assert board.unread(board.as_bystander()) == before


def test_a_second_observer_notifies_nobody_again(board):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)

    board.feed(board.as_conductor())
    board.feed(board.as_assignee())
    board.ranking(board.as_conductor())

    assert len(lapsed_rows(board.inbox(board.as_conductor()), issue_id)) == 1
    assert len(lapsed_rows(board.inbox(board.as_assignee()), issue_id)) == 1


# --- visibility -----------------------------------------------------------------------


def test_an_account_that_cannot_see_the_issue_learns_nothing(private_board):
    board = private_board
    issue_id = board.delegated_issue()
    # The conductor filed the issue, so it watches it; it stops watching, so
    # the only inbox row it could get is the one the lapse command addresses
    # to the delegating account.
    unwatched = board.client.delete(
        f"/watches/issue/{issue_id}", headers=board.as_conductor()
    )
    assert unwatched.status_code == 204, unwatched.text
    # The conductor delegated the work, then lost access to the project.
    removed = board.client.delete(
        f"/projects/{board.project_id}/members/{board.conductor_id}", headers=ADMIN
    )
    assert removed.status_code in (200, 204), removed.text
    cursor = board.feed(board.as_conductor())["next_after"]
    unread = board.unread(board.as_conductor())
    board.lapse(issue_id)

    # Its own reads observe nothing it cannot see: no write happens at all.
    hidden = board.feed(board.as_conductor(), after=cursor)
    assert hidden["events"] == [] and hidden["next_after"] == cursor
    assert issue_rows(board.ranking(board.as_conductor()), issue_id) == []
    assert board.lapses(issue_id) == []

    # The assignee, who can see it, observes it; the conductor still learns
    # nothing: no event, no row, no notification, no count, no cursor move.
    board.feed(board.as_assignee())
    assert len(board.lapses(issue_id)) == 1
    after = board.feed(board.as_conductor(), after=cursor)
    assert after["events"] == [] and after["next_after"] == cursor
    assert board.unread(board.as_conductor()) == unread
    assert lapsed_rows(board.inbox(board.as_conductor()), issue_id) == []
    assert issue_rows(board.ranking(board.as_conductor()), issue_id) == []
    desk = board.client.get("/desk", headers=board.as_conductor())
    assert desk.status_code == 200, desk.text
    latest = desk.json()["signals"]["latest_visible_event_id"]
    assert latest is None or latest <= cursor
    # The command checks each accountable recipient can see the issue before
    # it writes an inbox row (a watcher's row, as for every event, is written
    # ungated and gated when read).
    conn = db.connect(board.db_file)
    try:
        written = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications n JOIN activity a "
            "ON a.id = n.event_id WHERE a.verb = ? AND n.user_id = ?",
            (VERB, board.conductor_id),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert written == 0
    # The assignee, who can see it, is told.
    assert len(lapsed_rows(board.inbox(board.as_assignee()), issue_id)) == 1


# --- the command: once per generation ---------------------------------------------------


def test_the_command_records_once_per_generation(board):
    from athena.aegis import lease_lapse_commands

    issue_id = board.delegated_issue()
    generation = board.generation(issue_id)
    board.lapse(issue_id)
    conductor = board.user(board.conductor_id)
    conn = db.connect(board.db_file)
    try:
        first = lease_lapse_commands.record_lease_lapse(
            conn, actor=conductor, issue_id=issue_id, generation=generation
        )
        second = lease_lapse_commands.record_lease_lapse(
            conn, actor=conductor, issue_id=issue_id, generation=generation
        )
    finally:
        conn.close()

    assert first is not None and first["verb"] == VERB
    assert second is None
    assert len(board.lapses(issue_id)) == 1


def test_the_command_refuses_a_generation_that_is_not_the_lapsed_one(board):
    from athena.aegis import lease_lapse_commands

    issue_id = board.delegated_issue()
    live = board.generation(issue_id)
    conductor = board.user(board.conductor_id)
    conn = db.connect(board.db_file)
    try:
        # A live lease has not lapsed; a stale generation names no current row.
        assert (
            lease_lapse_commands.record_lease_lapse(
                conn, actor=conductor, issue_id=issue_id, generation=live
            )
            is None
        )
        board.lapse(issue_id)
        assert (
            lease_lapse_commands.record_lease_lapse(
                conn, actor=conductor, issue_id=issue_id, generation="0" * 32
            )
            is None
        )
    finally:
        conn.close()
    assert board.lapses(issue_id) == []


def test_two_observers_that_both_saw_the_lapse_write_one_event(board):
    # Deterministic interleaving: both connections read the candidate before
    # either writes; the second writer re-checks under the writer lock.
    from athena.aegis import lease_lapse_commands

    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    conductor = board.user(board.conductor_id)
    assignee = board.user(board.assignee["id"])
    first, second = db.connect(board.db_file), db.connect(board.db_file)
    try:
        seen_first = lease_lapse_commands.unrecorded_lapses(first, actor=conductor)
        seen_second = lease_lapse_commands.unrecorded_lapses(second, actor=assignee)
        assert [c["issue_id"] for c in seen_first] == [issue_id]
        assert [c["issue_id"] for c in seen_second] == [issue_id]
        wrote = [
            lease_lapse_commands.record_lease_lapse(
                conn, actor=actor, issue_id=issue_id, generation=c[0]["generation"]
            )
            for conn, actor, c in (
                (first, conductor, seen_first),
                (second, assignee, seen_second),
            )
        ]
    finally:
        first.close()
        second.close()

    assert wrote[0] is not None and wrote[1] is None
    assert len(board.lapses(issue_id)) == 1


def test_racing_observers_on_threads_write_one_event(board):
    from athena.aegis import lease_lapse_commands

    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    actors = [board.user(board.conductor_id), board.user(board.assignee["id"])] * 4
    start = threading.Barrier(len(actors))
    recorded: list[list[dict]] = []
    failures: list[BaseException] = []

    def observe(actor: dict) -> None:
        conn = db.connect(board.db_file)
        try:
            start.wait()
            recorded.append(lease_lapse_commands.observe_lapses(conn, actor))
        except BaseException as exc:  # noqa: BLE001 — surfaced in the assertion below
            failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=observe, args=(a,)) for a in actors]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sum(len(batch) for batch in recorded) == 1
    assert len(board.lapses(issue_id)) == 1


# --- automation does not fire on the verb ---------------------------------------------


def test_automation_never_fires_on_a_lapse(board):
    from athena.aegis import automation

    issue_id = board.delegated_issue()
    conn = db.connect(board.db_file)
    try:
        automation.create_rule(
            conn,
            name="anything",
            trigger_verb="*",
            action_type="comment",
            action_params={"body": "fired"},
            created_by=1,
            commit=True,
        )
        automation.process_pending(conn, executor=lambda *_args: None, max_batch=500)
        board.lapse(issue_id)
        board.feed(board.as_conductor())
        assert len(board.lapses(issue_id)) == 1
        fired: list[str] = []
        automation.process_pending(
            conn,
            executor=lambda _c, _r, event: fired.append(event["verb"]),
            max_batch=500,
        )
        assert fired == []
        assert automation.get_cursor(conn) >= board.lapses(issue_id)[0]["id"]
        # A rule cannot name the verb either.
        assert "trigger_verb" in (
            automation.validate_rule(
                trigger_verb=VERB,
                action_type="comment",
                conditions={},
                action_params={"body": "x"},
            )
            or ""
        )
    finally:
        conn.close()


def test_a_lapse_does_not_reset_a_schedules_inactivity_clock(board):
    from datetime import UTC, datetime

    from athena.aegis import automation

    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    board.feed(board.as_conductor())
    conn = db.connect(board.db_file)
    try:
        conn.execute(
            "UPDATE activity SET created_at = '2029-12-31 22:00:00' "
            "WHERE target_kind = 'issue' AND target_id = ? AND verb != ?",
            (issue_id, VERB),
        )
        conn.execute(
            "UPDATE activity SET created_at = '2030-01-01 00:00:00' "
            "WHERE target_kind = 'issue' AND target_id = ? AND verb = ?",
            (issue_id, VERB),
        )
        conn.commit()
        targets, _total = automation._schedule_targets(
            conn,
            {"conditions": {"inactive_for_seconds": 3600}},
            datetime(2030, 1, 1, tzinfo=UTC),
            max_targets=50,
        )
    finally:
        conn.close()
    assert issue_id in targets


# --- the reader never holds the writer lock while it reads -------------------------------


def _traced_connections(monkeypatch, statements: list[str]):
    """Record every statement every request connection runs, in order."""
    from athena.core import deps

    original = db.connect

    def connect(path):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(deps.db, "connect", connect)


def _writer_windows(statements: list[str]) -> list[tuple[int, int]]:
    """Index pairs (BEGIN IMMEDIATE, its COMMIT/ROLLBACK) in the trace."""
    windows: list[tuple[int, int]] = []
    opened: int | None = None
    for index, sql in enumerate(statements):
        word = sql.strip().upper()
        if word.startswith("BEGIN IMMEDIATE"):
            opened = index
        elif opened is not None and word.startswith(("COMMIT", "ROLLBACK")):
            windows.append((opened, index))
            opened = None
    return windows


def test_the_feed_read_runs_outside_the_lapse_writers_transaction(board, monkeypatch):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    statements: list[str] = []
    _traced_connections(monkeypatch, statements)

    page = board.feed(board.as_conductor())

    assert len(lapsed_in(page, issue_id)) == 1
    windows = _writer_windows(statements)
    inserts = [i for i, sql in enumerate(statements) if "INSERT INTO activity" in sql]
    feed_reads = [
        i
        for i, sql in enumerate(statements)
        if "FROM activity a" in sql and "ORDER BY a.id ASC" in sql
    ]
    candidate_reads = [
        i for i, sql in enumerate(statements) if "FROM issue_leases" in sql
    ]
    assert inserts and feed_reads and candidate_reads and windows
    # The lapse is written inside one immediate transaction...
    assert all(any(lo < i < hi for lo, hi in windows) for i in inserts)
    # ...and neither the observation read nor the feed read runs inside one.
    for index in (*feed_reads, candidate_reads[0]):
        assert not any(lo < index < hi for lo, hi in windows), statements[index]
    # The observation precedes the write, which precedes the feed read.
    assert candidate_reads[0] < inserts[0] < feed_reads[-1]


def test_the_attention_read_finishes_before_the_lapse_is_written(board, monkeypatch):
    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    statements: list[str] = []
    _traced_connections(monkeypatch, statements)

    ranking = board.ranking(board.as_conductor())

    assert issue_rows(ranking, issue_id)
    windows = _writer_windows(statements)
    inserts = [i for i, sql in enumerate(statements) if "INSERT INTO activity" in sql]
    ranking_reads = [i for i, sql in enumerate(statements) if "claim_event" in sql]
    assert inserts and ranking_reads and windows
    assert all(any(lo < i < hi for lo, hi in windows) for i in inserts)
    assert not any(lo < i < hi for i in ranking_reads for lo, hi in windows)
    assert max(ranking_reads) < min(inserts)


def test_a_held_writer_lock_does_not_stop_the_read(board, monkeypatch):
    # Another writer holds SQLite's writer lock for the whole request. The feed
    # still answers: its reads need no lock, and the lapse write, which does,
    # gives up quietly and is left for the next observer.
    from athena.core import deps

    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    original = db.connect

    def impatient(path):
        conn = original(path)
        conn.execute("PRAGMA busy_timeout = 50")
        return conn

    blocker = db.connect(board.db_file)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(deps.db, "connect", impatient)
        # The trusted actor header: a bearer token's last-use stamp is itself a
        # write, which would need the lock before the route ever ran.
        page = board.feed(as_user(board.conductor_id))
        assert lapsed_in(page, issue_id) == []
        assert page["next_after"] is not None
    finally:
        blocker.rollback()
        blocker.close()
    monkeypatch.setattr(deps.db, "connect", original)

    page = board.feed(board.as_conductor())

    assert len(lapsed_in(page, issue_id)) == 1


def test_an_observer_inside_an_open_transaction_writes_nothing(board):
    # Writing there would upgrade the caller's read snapshot to the writer lock.
    from athena.aegis import lease_lapse_commands

    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    conn = db.connect(board.db_file)
    try:
        conn.execute("BEGIN")
        assert (
            lease_lapse_commands.observe_lapses(conn, board.user(board.conductor_id))
            == []
        )
        conn.rollback()
    finally:
        conn.close()
    assert board.lapses(issue_id) == []


def test_the_observers_read_starts_from_the_leases_not_from_every_issue(board):
    """The event feed runs this read on entry, on every call. Leases are few and
    issues accumulate, so the plan must start from the lease table and reach
    each issue by its key; a plan that walks the issues grows with history."""
    from athena.aegis import leases

    issue_id = board.delegated_issue()
    board.lapse(issue_id)
    conn = db.connect(board.db_file)
    statements: list[tuple[str, object]] = []

    class Recording:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def execute(self, sql, params=()):
            statements.append((sql, params))
            return self._inner.execute(sql, params)

    try:
        found = leases.unrecorded_lapses(
            Recording(conn),
            viewer=board.user(board.conductor_id),
            accountable_id=board.conductor_id,
        )
        assert [row["issue_id"] for row in found] == [issue_id]
        (sql, params) = next(s for s in statements if "FROM issue_leases l" in s[0])
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    finally:
        conn.close()
    assert plan[0].startswith("SCAN l"), plan
    issue_steps = [step for step in plan if step.split()[1:2] == ["i"]]
    assert issue_steps == ["SEARCH i USING INTEGER PRIMARY KEY (rowid=?)"], plan
