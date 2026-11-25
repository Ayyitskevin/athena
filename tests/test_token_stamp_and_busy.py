"""A bearer read does not take the writer lock, and a lock timeout is not a
500 (H7, PERF-10).

Before: every bearer-authenticated request wrote the token's last use, so every
read queued for the one writer lock, and a wait past five seconds was an
unhandled error: 500. Now the stamp is written at most once a minute per
token and never waits for a busy writer, and a statement that does time out
on the lock answers 503 ``busy`` with ``Retry-After``.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient

from support import bearer, bootstrap, make_app, onboard_agent

from athena.core import db, tokens


def _last_used(db_file, token_id: int) -> str | None:
    conn = db.connect(db_file)
    try:
        row = conn.execute(
            "SELECT last_used_at FROM api_tokens WHERE id = ?", (token_id,)
        ).fetchone()
        return row["last_used_at"]
    finally:
        conn.close()


def _set_last_used(db_file, token_id: int, seconds_ago: int) -> str:
    conn = db.connect(db_file)
    try:
        conn.execute(
            "UPDATE api_tokens SET last_used_at = datetime('now', ?) WHERE id = ?",
            (f"-{seconds_ago} seconds", token_id),
        )
        conn.commit()
    finally:
        conn.close()
    stamped = _last_used(db_file, token_id)
    assert stamped is not None
    return stamped


class _Writer:
    """Another process holding the one writer lock."""

    def __init__(self, db_file):
        self._conn = sqlite3.connect(db_file, isolation_level=None)

    def __enter__(self):
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, *exc):
        self._conn.execute("ROLLBACK")
        self._conn.close()


def test_a_tokens_last_use_is_stamped_at_most_once_a_minute(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        token_id = sol["token"]["id"]
        never = _last_used(db_file, token_id)
        assert client.get("/issues", headers=bearer(sol)).status_code == 200
        first = _last_used(db_file, token_id)
        recent = _set_last_used(db_file, token_id, tokens.LAST_USED_STAMP_SECONDS - 20)
        assert client.get("/issues", headers=bearer(sol)).status_code == 200
        within_the_minute = _last_used(db_file, token_id)
        old = _set_last_used(db_file, token_id, tokens.LAST_USED_STAMP_SECONDS + 20)
        assert client.get("/issues", headers=bearer(sol)).status_code == 200
        after_the_minute = _last_used(db_file, token_id)

    assert never is None and first is not None
    assert within_the_minute == recent
    assert after_the_minute != old and after_the_minute > old


def test_a_bearer_read_does_not_wait_for_the_writer(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        token_id = sol["token"]["id"]
        assert client.get("/issues", headers=bearer(sol)).status_code == 200
        with _Writer(db_file):
            started = time.monotonic()
            stamped_recently = client.get("/issues", headers=bearer(sol))
        old = _set_last_used(db_file, token_id, tokens.LAST_USED_STAMP_SECONDS + 20)
        # A stamp that is due is skipped while the database is busy, after a
        # short wait; the read still answers.
        with _Writer(db_file):
            stamp_due = client.get("/issues", headers=bearer(sol))
            elapsed = time.monotonic() - started
            unstamped = _last_used(db_file, token_id)
        later = client.get("/issues", headers=bearer(sol))
        stamped = _last_used(db_file, token_id)

    assert stamped_recently.status_code == 200, stamped_recently.text
    assert stamp_due.status_code == 200, stamp_due.text
    # Both reads together took far less than one five-second lock wait.
    assert elapsed < db.BUSY_TIMEOUT_MS / 1000
    assert unstamped == old
    assert later.status_code == 200 and stamped != old


def test_a_write_that_times_out_on_the_lock_answers_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 150)
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        assert client.get("/issues", headers=bearer(sol)).status_code == 200
        before = client.get("/issues", headers=bearer(sol)).json()["issues"]
        with _Writer(db_file):
            refused = client.post(
                "/issues", json={"title": "while busy"}, headers=bearer(sol)
            )
        retried = client.post(
            "/issues", json={"title": "while busy"}, headers=bearer(sol)
        )
        after = client.get("/issues", headers=bearer(sol)).json()["issues"]

    assert refused.status_code == 503, refused.text
    assert refused.json()["code"] == "busy"
    assert int(refused.headers["Retry-After"]) >= 1
    # Nothing was applied by the refused write; the retry is the first one.
    assert retried.status_code == 201, retried.text
    assert len(after) == len(before) + 1


def test_another_database_error_is_still_a_fault():
    assert db.is_lock_timeout(sqlite3.OperationalError("database is locked"))
    assert not db.is_lock_timeout(sqlite3.OperationalError("no such table: nope"))
    assert not db.is_lock_timeout(RuntimeError("database is locked"))
