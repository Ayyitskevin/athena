"""Data access for issue leases — the delegation claim/lease protocol.

A lease is the exclusive "an agent is actively working this issue right now" token: at
most one per issue (the ``issue_leases`` PRIMARY KEY guarantees it). It is deliberately
NOT the assignee (the accountable owner) nor the contributor set (everyone the issue was
delegated to) — it is the thing that stops two delegated agents from silently pulling the
same work. A lease carries an expiry, so "active" is a pure function of the clock: a
crashed holder's lease simply expires and the work becomes reclaimable, with no background
sweeper.

All lease SQL lives here; the conflict/eligibility rules and audit events live in
aegis/lease_commands.py, the same split every other Aegis write uses (data layer owns the
row, the command owns the policy + event).
"""
from __future__ import annotations

import sqlite3

# A claim lasts this long by default before it must be renewed (re-claimed). Long enough
# for a real work session, short enough that an abandoned claim frees the work within the
# hour. Callers may pass their own window up to the max.
DEFAULT_LEASE_SECONDS = 1800  # 30 minutes
MIN_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 86_400  # 24 hours

_SELECT = (
    "SELECT l.issue_id, l.holder_id, u.name AS holder_name, "
    "l.claimed_at, l.expires_at, "
    "(l.expires_at > datetime('now')) AS active "
    "FROM issue_leases l JOIN users u ON u.id = l.holder_id"
)


def _row_to_lease(row: sqlite3.Row) -> dict:
    lease = dict(row)
    lease["active"] = bool(lease["active"])  # SQLite returns 0/1 for the comparison
    return lease


def get_lease(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    """The issue's lease row (with the holder's name and a computed ``active`` flag), or
    None if it was never claimed. A row whose ``active`` is False is an EXPIRED lease —
    still present until the next claim overwrites it, but no longer holding the issue."""
    row = conn.execute(
        f"{_SELECT} WHERE l.issue_id = ?", (issue_id,)
    ).fetchone()
    return _row_to_lease(row) if row is not None else None


def upsert_lease(
    conn: sqlite3.Connection,
    issue_id: int,
    holder_id: int,
    lease_seconds: int,
    *,
    commit: bool = True,
) -> dict:
    """Acquire or renew the lease: set ``holder_id`` and an ``expires_at`` of now +
    ``lease_seconds``, replacing any existing row (the PRIMARY KEY makes this one lease).
    The CONFLICT check — refusing to steal another holder's ACTIVE lease — is the
    command's job, run under the same write lock; this is the unconditional write it
    performs once that check passes. ``commit=False`` folds it into the command's
    transaction with the audit event. Returns the fresh lease."""
    conn.execute(
        "INSERT INTO issue_leases (issue_id, holder_id, claimed_at, expires_at) "
        "VALUES (?, ?, datetime('now'), datetime('now', ?)) "
        "ON CONFLICT(issue_id) DO UPDATE SET "
        "holder_id = excluded.holder_id, claimed_at = excluded.claimed_at, "
        "expires_at = excluded.expires_at",
        (issue_id, holder_id, f"+{lease_seconds} seconds"),
    )
    if commit:
        conn.commit()
    return get_lease(conn, issue_id)


def delete_lease(
    conn: sqlite3.Connection, issue_id: int, *, commit: bool = True
) -> bool:
    """Release the lease. Returns True if a row was removed, False if there was none.
    ``commit=False`` folds the release into the command's transaction with its event."""
    cur = conn.execute("DELETE FROM issue_leases WHERE issue_id = ?", (issue_id,))
    if commit:
        conn.commit()
    return cur.rowcount > 0
