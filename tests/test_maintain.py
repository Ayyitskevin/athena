"""``athena-maintain`` removes what has no further use, and only that (H20).

WHY (OPS-12): nothing documented growth or retention. Read notifications
were kept for ever, a browser session past its expiry was removed only by
logging out, and an idempotency receipt past its expiry only when a later
keyed write happened to sweep it. Decision D26: an explicit command the
operator schedules, with documented ages, and never the activity trail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from athena import ops
from athena.core import activity, db, idempotency, maintenance_commands

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
TS = "%Y-%m-%d %H:%M:%S"


def _ago(**delta) -> str:
    return (NOW - timedelta(**delta)).strftime(TS)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "athena.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name, role) VALUES ('a@e.com','A','admin')")
    conn.commit()
    yield path, conn
    conn.close()


def _notification(conn, *, read_at: str | None) -> int:
    event = activity.record(
        conn, actor_id=1, verb="created", target_kind="issue", target_id=1, commit=True
    )
    cur = conn.execute(
        "INSERT INTO notifications (user_id, event_id, read_at) VALUES (1, ?, ?)",
        (event["id"], read_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def _session(conn, name: str, *, expires_at: str) -> None:
    conn.execute(
        "INSERT INTO sessions (user_id, session_hash, expires_at, csrf_token) "
        "VALUES (1, ?, ?, 'c')",
        (name, expires_at),
    )
    conn.commit()


def _receipt(conn, key: str, *, completed_at: datetime | None, ttl: int = 3600) -> None:
    """A key claimed now; completed at ``completed_at`` with ``ttl``, or left
    executing when that is ``None``."""
    claim = idempotency.claim_or_read(
        conn,
        key=key,
        identity="actor:1",
        method="POST",
        path="/labels",
        request_fingerprint="f",
        lease_seconds=60,
        now=int(NOW.timestamp()) - 10_000_000,
    )
    if completed_at is not None:
        idempotency.complete(
            conn,
            key=key,
            identity="actor:1",
            owner_token=claim.record["owner_token"],
            status_code=201,
            content_type="application/json",
            response_headers="{}",
            response_body=b"{}",
            ttl_seconds=ttl,
            now=int(completed_at.timestamp()),
        )


def _ids(conn, table: str, column: str = "id") -> list:
    return [row[0] for row in conn.execute(f"SELECT {column} FROM {table} ORDER BY 1")]


def _everything(conn) -> dict:
    return {
        "notifications": _ids(conn, "notifications"),
        "sessions": _ids(conn, "sessions", "session_hash"),
        "keys": _ids(conn, "idempotency_keys", "idempotency_key"),
        "trail": conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0],
    }


@pytest.fixture
def seeded(database):
    path, conn = database
    kept = {
        "notifications": [
            _notification(conn, read_at=None),  # unread, however old
            _notification(conn, read_at=_ago(days=89, hours=23)),  # read too lately
        ],
        "sessions": ["live"],
        "keys": ["executing", "unexpired"],
    }
    conn.execute("UPDATE notifications SET created_at = ?", (_ago(days=900),))
    _notification(conn, read_at=_ago(days=90))  # read exactly the age ago
    _notification(conn, read_at=_ago(days=400))
    _session(conn, "live", expires_at=_ago(seconds=-1))
    _session(conn, "expired", expires_at=_ago(seconds=0))
    _session(conn, "long-expired", expires_at=_ago(days=30))
    _receipt(conn, "executing", completed_at=None)
    _receipt(conn, "unexpired", completed_at=NOW, ttl=1)
    _receipt(conn, "expired", completed_at=NOW - timedelta(hours=2))
    return path, conn, kept


def test_it_removes_what_is_old_and_reports_each_kind(seeded):
    _path, conn, kept = seeded
    trail = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    counts = maintenance_commands.prune(conn, now=NOW)

    assert counts == {
        "read_notifications": 2,
        "expired_sessions": 2,
        "expired_idempotency_keys": 1,
    }
    assert _everything(conn) == {**kept, "trail": trail}
    # Run again: there is nothing left to remove.
    assert set(maintenance_commands.prune(conn, now=NOW).values()) == {0}


def test_a_dry_run_counts_the_same_and_removes_nothing(seeded):
    _path, conn, _kept = seeded
    before = _everything(conn)

    counts = maintenance_commands.prune(conn, now=NOW, dry_run=True)

    assert counts == {
        "read_notifications": 2,
        "expired_sessions": 2,
        "expired_idempotency_keys": 1,
    }
    assert _everything(conn) == before


def test_the_age_of_a_read_notification_is_the_callers(seeded):
    _path, conn, kept = seeded

    assert (
        maintenance_commands.prune(
            conn, now=NOW, read_notification_days=0, dry_run=True
        )["read_notifications"]
        == 3
    )
    assert (
        maintenance_commands.prune(
            conn, now=NOW, read_notification_days=401, dry_run=True
        )["read_notifications"]
        == 0
    )
    # An unread notification is never removed, at any age.
    maintenance_commands.prune(conn, now=NOW, read_notification_days=0)
    assert _ids(conn, "notifications") == kept["notifications"][:1]


def test_many_rows_go_in_batches_and_rows_between_them_stay(database, monkeypatch):
    _path, conn = database
    monkeypatch.setattr(maintenance_commands, "BATCH_ROWS", 2)
    stays, goes = [], []
    # Alternating, so every batch of two spans a row that must stay.
    for number in range(11):
        if number % 2 == 0:
            stays.append(_notification(conn, read_at=None))
        else:
            goes.append(_notification(conn, read_at=_ago(days=200)))
    commits = []
    conn.set_trace_callback(
        lambda sql: commits.append(sql) if sql.startswith("COMMIT") else None
    )

    counts = maintenance_commands.prune(conn, now=NOW)

    conn.set_trace_callback(None)
    assert counts["read_notifications"] == len(goes) == 5
    assert _ids(conn, "notifications") == stays
    # Three batches of at most two rows, each its own transaction.
    assert len(commits) == 3


# --- the command ------------------------------------------------------------------


def test_the_command_reports_what_it_removed(seeded, capsys):
    path, conn, kept = seeded
    # The command reads the clock: place the rows around the real present.
    conn.execute(
        "UPDATE notifications SET read_at = datetime('now', '-91 days') "
        "WHERE read_at IS NOT NULL AND id NOT IN (?, ?)",
        kept["notifications"],
    )
    conn.execute(
        "UPDATE notifications SET read_at = datetime('now', '-89 days') WHERE id = ?",
        (kept["notifications"][1],),
    )
    conn.execute(
        "UPDATE sessions SET expires_at = datetime('now', '+1 day') "
        "WHERE session_hash = 'live'"
    )
    conn.commit()

    assert ops.maintain_main([str(path), "--dry-run"]) == 0
    dry = capsys.readouterr()
    assert ops.maintain_main([str(path)]) == 0
    done = capsys.readouterr()

    assert done.err == dry.err == ""
    assert done.out.splitlines() == [
        "removed 2 notifications read more than 90 days ago",
        "removed 2 browser sessions past their expiry",
        "removed 2 idempotency receipts past their expiry",
        "the activity trail is never pruned",
    ]
    assert dry.out == done.out.replace("removed", "would remove")
    assert _ids(conn, "notifications") == kept["notifications"]
    assert _ids(conn, "sessions", "session_hash") == ["live"]
    assert _ids(conn, "idempotency_keys", "idempotency_key") == ["executing"]


def test_the_command_takes_the_age_in_days(seeded, capsys):
    path, conn, kept = seeded
    # Read a day ago by the real clock: the default age would keep it.
    conn.execute(
        "UPDATE notifications SET read_at = datetime('now', '-1 day') WHERE id = ?",
        (kept["notifications"][1],),
    )
    conn.commit()

    assert ops.maintain_main([str(path), "--read-notification-days", "0"]) == 0

    assert capsys.readouterr().out.startswith(
        "removed 3 notifications read more than 0 days ago\n"
    )
    assert _ids(conn, "notifications") == kept["notifications"][:1]


@pytest.mark.parametrize("days", ["-1", "ninety", "1.5"])
def test_an_age_that_is_not_a_number_of_days_is_a_usage_error(seeded, capsys, days):
    path, conn, _kept = seeded
    before = _everything(conn)

    with pytest.raises(SystemExit) as stopped:
        ops.maintain_main([str(path), "--read-notification-days", days])

    assert stopped.value.code == 2
    assert "must be a whole number of days, 0 or more" in capsys.readouterr().err
    assert _everything(conn) == before


def test_a_database_that_is_not_there_is_refused_and_not_created(tmp_path, capsys):
    missing = tmp_path / "nowhere.db"

    assert ops.maintain_main([str(missing)]) == 1

    assert capsys.readouterr().err.startswith(
        f"athena-maintain: database does not exist: {missing}"
    )
    assert not missing.exists()


def test_a_database_that_is_not_athenas_is_refused_untouched(tmp_path, capsys):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY, read_at TEXT)")
    conn.execute("INSERT INTO notifications (read_at) VALUES ('2000-01-01 00:00:00')")
    conn.commit()
    conn.close()

    assert ops.maintain_main([str(other)]) == 1

    assert capsys.readouterr().err.startswith("athena-maintain: ")
    conn = sqlite3.connect(other)
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    conn.close()
