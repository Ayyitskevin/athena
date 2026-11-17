"""Writer reservation orders replacement, review, and active-possession time."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest

from athena.aegis import lease_commands, review_commands
from athena.aegis.issue_commands import IssueCommandError
from athena.core import db
from test_review_queue import (
    board as _board,
    claim,
    complete,
    issue,
    queue,
    submission,
    submit,
)

board = _board


@pytest.mark.parametrize("first", ["replacement", "review"])
def test_writer_order_decides_exact_candidate_without_retargeting(
    board, monkeypatch, first
):
    conn, path, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    old = submit(conn, build)
    builder, reviewer = claim(conn, build), claim(conn, check, 3)
    reserved, other_attempted, release = Event(), Event(), Event()
    hook = "prepare_submission" if first == "replacement" else "prepare_review"
    original = getattr(review_commands, hook)

    def stop_after_reservation(*args, **kwargs):
        reserved.set()
        assert release.wait(15), "test did not release first writer"
        return original(*args, **kwargs)

    monkeypatch.setattr(review_commands, hook, stop_after_reservation)

    def work(which, *, signal=False):
        connection = db.connect(path)
        if signal:
            connection.set_trace_callback(
                lambda sql: other_attempted.set() if sql == "BEGIN IMMEDIATE" else None
            )
        try:
            if which == "replacement":
                return complete(
                    connection,
                    build,
                    builder,
                    candidate_submission=submission(old["candidate_id"], "b" * 40),
                )
            return complete(
                connection,
                check,
                reviewer,
                3,
                review_candidate_id=old["candidate_id"],
                verdict="PASS",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(work, first)
        try:
            assert reserved.wait(15)
            second_future = pool.submit(
                work, "review" if first == "replacement" else "replacement", signal=True
            )
            assert other_attempted.wait(15)
        finally:
            release.set()
        first_future.result(timeout=15)
        if first == "replacement":
            with pytest.raises(IssueCommandError, match="no longer current"):
                second_future.result(timeout=15)
            assert (
                conn.execute(
                    "SELECT generation FROM issue_leases WHERE issue_id=?",
                    (check["id"],),
                ).fetchone()[0]
                == reviewer["generation"]
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM issue_candidate_reviews").fetchone()[
                    0
                ]
                == 0
            )
        else:
            second_future.result(timeout=15)
            assert (
                conn.execute(
                    "SELECT candidate_id FROM issue_candidate_reviews"
                ).fetchone()[0]
                == old["candidate_id"]
            )
    assert queue(conn, project)["items"][0]["review_state"] == "awaiting_review"


def test_competing_same_possession_submissions_create_one_candidate(board):
    conn, path, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    start = Event()

    def work():
        connection = db.connect(path)
        try:
            assert start.wait(15)
            try:
                return complete(
                    connection, build, held, candidate_submission=submission()
                )
            except IssueCommandError as exc:
                return exc.kind
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work) for _ in range(2)]
        start.set()
        results = [future.result(timeout=15) for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "conflict" in results
    assert (
        conn.execute("SELECT COUNT(*) FROM issue_review_candidates").fetchone()[0] == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM activity WHERE verb='claim_completed'"
        ).fetchone()[0]
        == 1
    )


def test_extension_clock_and_expiry_are_observed_after_waiting_writer(
    board, monkeypatch
):
    conn, path, project = board
    build = issue(conn, project)
    held = claim(conn, build)
    attempted, unlocked = Event(), Event()

    class ControlledClock:
        @classmethod
        def now(cls, tz):
            assert unlocked.is_set(), "sampled before acquiring writer reservation"
            return datetime(2035, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(lease_commands, "datetime", ControlledClock)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE issue_leases SET expires_at='2034-12-31 23:59:59'")

    def waiting_completion():
        connection = db.connect(path)
        connection.set_trace_callback(
            lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None
        )
        try:
            return complete(connection, build, held, candidate_submission=submission())
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiting_completion)
        try:
            assert attempted.wait(15)
        finally:
            unlocked.set()
            conn.commit()
        with pytest.raises(IssueCommandError, match="own active claim"):
            future.result(timeout=15)
    assert (
        conn.execute("SELECT generation FROM issue_leases").fetchone()[0]
        == held["generation"]
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM issue_review_candidates").fetchone()[0] == 0
    )


def test_queue_counts_rows_and_reviews_share_one_wal_snapshot(board, monkeypatch):
    from athena.aegis import review_queue

    conn, path, project = board
    build = issue(conn, project)
    first = submit(conn, build)
    held = claim(conn, build)
    original = review_queue._rows
    wrote = False

    def concurrent_write_after_count_and_rows(*args, **kwargs):
        nonlocal wrote
        result = original(*args, **kwargs)
        if not wrote:
            wrote = True
            writer = db.connect(path)
            try:
                complete(
                    writer,
                    build,
                    held,
                    candidate_submission=submission(first["candidate_id"], "b" * 40),
                )
            finally:
                writer.close()
        return result

    monkeypatch.setattr(review_queue, "_rows", concurrent_write_after_count_and_rows)
    packet = queue(conn, project)
    assert packet["visible_total"] == packet["returned"] == 1
    assert packet["items"][0]["candidate"]["candidate_id"] == first["candidate_id"]
    assert (
        queue(conn, project)["items"][0]["candidate"]["candidate_id"]
        != first["candidate_id"]
    )
