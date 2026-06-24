"""Tests for the database layer: the migration runner and the schema's rules.

Each test gets its own throwaway database file via pytest's `tmp_path`, so the
tests never touch real data and never interfere with each other.
"""
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
