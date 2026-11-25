"""Retention: the rows ``athena-maintain`` removes (H20).

Three kinds of row have no use once they are old, and none was ever removed:
a notification its reader read long ago, a browser session past its expiry
(logging out removed one; closing the browser did not), and an idempotency
receipt past its expiry. An operator schedules ``athena-maintain`` to remove
them. It is an explicit command with documented ages (decision D26); no
request removes them as a side effect. One bounded exception predates this
and stays: a keyed write removes up to 100 expired receipts while it claims
its own key.

Never removed, here or anywhere: the activity trail and its hash chain,
unread notifications, live sessions, a receipt that has not expired, and an
idempotency key whose outcome is unknown (it has no expiry on purpose).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from athena.core import db

#: A read notification is kept this many days after it was read.
READ_NOTIFICATION_DAYS = 90

# Rows removed per transaction, so a run beside a serving Athena holds the
# writer lock for one short batch at a time.
BATCH_ROWS = 500

_TS_FMT = "%Y-%m-%d %H:%M:%S"

#: What is removed, in the order it is reported: name, table, and the
#: condition that makes a row removable (one ``?``, the bound).
_RULES = (
    (
        "read_notifications",
        "notifications",
        "read_at IS NOT NULL AND read_at <= ?",
    ),
    ("expired_sessions", "sessions", "expires_at <= ?"),
    (
        "expired_idempotency_keys",
        "idempotency_keys",
        "expires_at IS NOT NULL AND expires_at <= ?",
    ),
)


def _remove(
    conn: sqlite3.Connection, table: str, removable: str, bound: object, dry_run: bool
) -> int:
    """Remove every row of ``table`` that is ``removable``, a batch at a time
    in row order, so the table is walked once however many batches it takes."""
    if dry_run:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {removable}", (bound,)
            ).fetchone()[0]
        )
    removed, after = 0, 0
    while True:
        rows = conn.execute(
            f"SELECT rowid FROM {table} WHERE rowid > ? AND {removable} "
            "ORDER BY rowid LIMIT ?",
            (after, bound, BATCH_ROWS),
        ).fetchall()
        if not rows:
            return removed
        after = rows[-1][0]
        with db.transaction(conn):
            removed += conn.execute(
                f"DELETE FROM {table} WHERE rowid BETWEEN ? AND ? AND {removable}",
                (rows[0][0], after, bound),
            ).rowcount


def prune(
    conn: sqlite3.Connection,
    *,
    read_notification_days: int = READ_NOTIFICATION_DAYS,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Remove what retention allows and return how many rows of each kind
    were removed (or, with ``dry_run``, would be)."""
    current = now or datetime.now(timezone.utc)
    bounds = {
        "read_notifications": (
            current - timedelta(days=read_notification_days)
        ).strftime(_TS_FMT),
        "expired_sessions": current.strftime(_TS_FMT),
        "expired_idempotency_keys": int(current.timestamp()),
    }
    return {
        name: _remove(conn, table, removable, bounds[name], dry_run)
        for name, table, removable in _RULES
    }
