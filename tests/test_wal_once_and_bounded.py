"""The journal mode is set once per database, and the write-ahead log has a
size limit (H9, PERF-13).

Before: every connection asked for WAL mode, though WAL is a property of the
file and was already set; a request opens a connection, so every request paid
for it. And the write-ahead log kept the size of the largest burst of writes
it had ever held: 59.8 MB was measured, with nothing to bring it back.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from athena.core import db


class _Recording(sqlite3.Connection):
    """A connection that remembers every statement run on it."""

    statements: list[str]

    def execute(self, sql, *args):  # type: ignore[override]
        if not hasattr(self, "statements"):
            self.statements = []
        self.statements.append(" ".join(sql.split()))
        return super().execute(sql, *args)


@pytest.fixture
def recorded(monkeypatch):
    real = sqlite3.connect

    def connect(path, **kwargs):
        return real(path, factory=_Recording, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", connect)


def _sets_wal(conn) -> bool:
    return any("journal_mode" in sql for sql in conn.statements)


def _mode(conn) -> str:
    return sqlite3.Connection.execute(conn, "PRAGMA journal_mode").fetchone()[0]


def test_the_journal_mode_is_asked_for_once_per_database_file(tmp_path, recorded):
    path = tmp_path / "athena.db"
    opened = []
    for _ in range(4):
        conn = db.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
        conn.commit()
        opened.append((_sets_wal(conn), _mode(conn)))
        conn.close()

    # The first connection finds an empty file and sets it; after that the
    # file's own header says WAL and nothing asks again.
    assert [sets for sets, _ in opened] == [True, False, False, False]
    assert {mode for _, mode in opened} == {"wal"}


def test_a_new_database_at_a_path_seen_before_is_set_again(tmp_path, recorded):
    path = tmp_path / "athena.db"
    for _ in range(3):
        conn = db.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
        conn.commit()
        conn.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(f"{path}{suffix}"):
            os.unlink(f"{path}{suffix}")
    # Another tool writes a database there, in the rollback-journal mode.
    other = sqlite3.connect(path)
    other.execute("CREATE TABLE t (x)")
    other.commit()
    assert other.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    other.close()

    conn = db.connect(path)
    try:
        assert _mode(conn) == "wal"
    finally:
        conn.close()


def test_a_database_that_is_not_a_file_is_set_on_every_connection(recorded):
    for _ in range(2):
        conn = db.connect(":memory:")
        try:
            assert _sets_wal(conn)
        finally:
            conn.close()


def test_every_connection_carries_the_size_limit(tmp_path):
    path = tmp_path / "athena.db"
    for _ in range(3):
        conn = db.connect(path)
        try:
            limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        finally:
            conn.close()
        assert limit == db.JOURNAL_SIZE_LIMIT_BYTES == 32 * 1024 * 1024


def test_the_log_is_cut_back_after_a_burst(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "JOURNAL_SIZE_LIMIT_BYTES", 64 * 1024)
    path = tmp_path / "athena.db"
    wal = tmp_path / "athena.db-wal"
    conn = db.connect(path)
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.executemany("INSERT INTO t VALUES (?)", [("x" * 10_000,)] * 200)
        conn.commit()
        burst = wal.stat().st_size
        conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchall()
        conn.execute("INSERT INTO t VALUES ('one more')")
        conn.commit()
        after = wal.stat().st_size
    finally:
        conn.close()

    assert burst > 1_000_000
    assert after <= 64 * 1024
