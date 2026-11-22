"""Tests for the database layer: the migration runner and the schema's rules.

Each test gets its own throwaway database file via pytest's `tmp_path`, so the
tests never touch real data and never interfere with each other.
"""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from athena.core import db


def test_migrate_creates_tables_and_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "test.db")

    applied = db.migrate(conn)
    assert "0001_initial.sql" in applied

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"users", "issues", "schema_migrations"} <= tables

    # Running again must apply nothing — that's the whole point of remembering.
    assert db.migrate(conn) == []


def test_foreign_key_is_enforced(tmp_path):
    # WHY this test matters: it fails the moment someone removes
    # `PRAGMA foreign_keys = ON` from connect(). It guards the rule that an
    # issue can never reference a user that doesn't exist.
    conn = db.connect(tmp_path / "fk.db")
    db.migrate(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO issues (title, created_by) VALUES (?, ?)",
            ("orphan issue", 999),  # no user #999 exists
        )


def test_connect_sets_a_busy_timeout(tmp_path):
    # WHY this test matters: it fails the moment someone removes
    # `PRAGMA busy_timeout` from connect(). Without it, a second concurrent
    # writer gets an instant "database is locked" (a 500) instead of waiting
    # its turn behind the write lock — fine in single-process dogfood, a real
    # error the day the fleet hits the API in parallel as actors.
    conn = db.connect(tmp_path / "busy.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connection_can_follow_a_request_across_worker_threads(tmp_path):
    # WHY: FastAPI's synchronous yield dependency can create the connection in one
    # worker and execute/close it in another. The handoff is sequential, not shared
    # concurrency, but SQLite's creator-thread default rejects it in real Uvicorn.
    conn = db.connect(tmp_path / "thread-handoff.db")
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(lambda: conn.execute("SELECT 1").fetchone()[0]).result()
    assert result == 1
    conn.close()


def test_can_insert_a_user_and_their_issue(tmp_path):
    conn = db.connect(tmp_path / "happy.db")
    db.migrate(conn)

    cur = conn.execute(
        "INSERT INTO users (email, name) VALUES (?, ?)",
        ("kevin@example.com", "Kevin"),
    )
    user_id = cur.lastrowid

    conn.execute(
        "INSERT INTO issues (title, created_by) VALUES (?, ?)",
        ("first issue", user_id),
    )

    issue = conn.execute("SELECT title, status FROM issues").fetchone()
    assert issue["title"] == "first issue"
    assert issue["status"] == "open"  # the DEFAULT kicked in


# --- db.transaction: the outer block owns the only commit (B5) ---------------
#
# WHY: a command's writes and its activity event must land together or not at
# all. A writer that commits underneath the block (a forgotten commit=False)
# splits that unit silently; the outer exit now refuses to pretend otherwise.


def _ledger(tmp_path):
    conn = db.connect(tmp_path / "txn.db")
    conn.execute("CREATE TABLE ledger (note TEXT NOT NULL)")
    conn.commit()
    return conn


def _notes(conn):
    return [
        row["note"] for row in conn.execute("SELECT note FROM ledger ORDER BY rowid")
    ]


def test_a_committing_call_inside_a_transaction_raises(tmp_path):
    from athena.core import activity

    conn = db.connect(tmp_path / "record.db")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()

    with pytest.raises(db.TransactionEndedEarly, match="commit=False"):
        with db.transaction(conn):
            conn.execute("UPDATE users SET name = 'renamed' WHERE id = 1")
            activity.record(  # commit=True: the ARCH-1 shape
                conn,
                actor_id=1,
                verb="created",
                target_kind="user",
                target_id=1,
                commit=True,
            )

    # The early commit cannot be taken back; the guard's job is to make it loud.
    assert not conn.in_transaction
    assert conn.execute("SELECT name FROM users WHERE id = 1").fetchone()[0] == (
        "renamed"
    )


def test_a_commit_followed_by_more_writes_still_raises_and_rolls_the_rest_back(
    tmp_path,
):
    # After a stray commit the next write silently opens a new implicit
    # transaction, so "still in a transaction" alone would pass. The guard
    # savepoint does not survive the commit.
    conn = _ledger(tmp_path)

    with pytest.raises(db.TransactionEndedEarly):
        with db.transaction(conn, immediate=True):
            conn.execute("INSERT INTO ledger VALUES ('before')")
            conn.commit()
            conn.execute("INSERT INTO ledger VALUES ('after')")

    assert _notes(conn) == ["before"]
    assert not conn.in_transaction


def test_a_normal_block_commits_once_at_exit(tmp_path):
    conn = _ledger(tmp_path)
    with db.transaction(conn):
        conn.execute("INSERT INTO ledger VALUES ('one')")
        conn.execute("INSERT INTO ledger VALUES ('two')")
        assert conn.in_transaction
    assert not conn.in_transaction
    other = db.connect(tmp_path / "txn.db")
    assert _notes(other) == ["one", "two"]


def test_a_nested_block_is_a_savepoint_inside_the_outer_transaction(tmp_path):
    conn = _ledger(tmp_path)
    with db.transaction(conn):
        conn.execute("INSERT INTO ledger VALUES ('outer')")
        with pytest.raises(ValueError):
            with db.transaction(conn):
                conn.execute("INSERT INTO ledger VALUES ('inner, undone')")
                raise ValueError("inner fails")
        with db.transaction(conn):
            conn.execute("INSERT INTO ledger VALUES ('inner, kept')")
        assert conn.in_transaction
    assert _notes(conn) == ["outer", "inner, kept"]


def test_a_failing_block_rolls_back_and_raises_its_own_error(tmp_path):
    conn = _ledger(tmp_path)
    with pytest.raises(ValueError) as raised:
        with db.transaction(conn):
            conn.execute("INSERT INTO ledger VALUES ('undone')")
            raise ValueError("the block's own failure")
    assert str(raised.value) == "the block's own failure"
    assert not getattr(raised.value, "__notes__", [])
    assert _notes(conn) == []
    assert not conn.in_transaction


def test_a_failing_block_after_a_stray_commit_keeps_its_own_error(tmp_path):
    # The guard must never mask the exception that is already unwinding the
    # block; it only adds a note saying part of the work committed early.
    conn = _ledger(tmp_path)
    with pytest.raises(ValueError) as raised:
        with db.transaction(conn):
            conn.execute("INSERT INTO ledger VALUES ('committed early')")
            conn.commit()
            conn.execute("INSERT INTO ledger VALUES ('undone')")
            raise ValueError("the block's own failure")
    assert str(raised.value) == "the block's own failure"
    assert any("committed or rolled back" in note for note in raised.value.__notes__)
    assert _notes(conn) == ["committed early"]
    assert not conn.in_transaction
