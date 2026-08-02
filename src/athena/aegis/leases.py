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

from datetime import UTC, datetime
import secrets
import sqlite3
from typing import TypeGuard

# A claim lasts this long by default before it must be renewed (re-claimed). Long enough
# for a real work session, short enough that an abandoned claim frees the work within the
# hour. Callers may pass their own window up to the max.
DEFAULT_LEASE_SECONDS = 1800  # 30 minutes
MIN_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 86_400  # 24 hours
GENERATION_HEX_CHARS = 32


def is_valid_generation(value: object) -> TypeGuard[str]:
    """Whether value is one canonical opaque lease-generation token."""
    return (
        isinstance(value, str)
        and len(value) == GENERATION_HEX_CHARS
        and all(char in "0123456789abcdef" for char in value)
    )


_SELECT = (
    "SELECT l.issue_id, l.holder_id, u.name AS holder_name, "
    "l.claimed_at, l.expires_at, l.generation, "
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
    row = conn.execute(f"{_SELECT} WHERE l.issue_id = ?", (issue_id,)).fetchone()
    return _row_to_lease(row) if row is not None else None


def leases_held_by(
    conn: sqlite3.Connection,
    *,
    holder_id: int,
    limit: int = 20,
    now: datetime | None = None,
) -> list[dict]:
    """One holder's leases, most recently claimed first — "what am I holding?".

    The self-read twin of ``get_lease``: that answers "who holds this issue",
    this answers "which issues do I hold". Both derive ``active`` from
    ``expires_at`` against the clock rather than a stored flag, so an expired
    lease reads as expired the instant it lapses without anything having run.
    ``now`` is injectable for the same reason the worker staleness clock is.

    EXPIRED ROWS ARE INCLUDED on purpose. A holder whose lease just lapsed
    still needs to see it — that is the fact they must act on (renew, or accept
    it is gone) — and hiding it would make a silent loss look like work that
    never existed. The ``active`` flag is the caller's to read.
    """
    bounded = max(1, min(int(limit), 100))
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT l.issue_id, l.holder_id, u.name AS holder_name, "
        "l.claimed_at, l.expires_at, l.generation, "
        "(l.expires_at > ?) AS active "
        "FROM issue_leases l JOIN users u ON u.id = l.holder_id "
        "WHERE l.holder_id = ? ORDER BY l.claimed_at DESC, l.issue_id DESC LIMIT ?",
        (stamp, holder_id, bounded),
    ).fetchall()
    return [_row_to_lease(row) for row in rows]


def count_leases_held_by(conn: sqlite3.Connection, *, holder_id: int) -> int:
    """How many leases this holder has rows for, so a clipped list can say so."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM issue_leases WHERE holder_id = ?",
            (holder_id,),
        ).fetchone()["n"]
    )


def upsert_lease(
    conn: sqlite3.Connection,
    issue_id: int,
    holder_id: int,
    lease_seconds: int,
    generation: str | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Acquire or renew the lease with one opaque possession generation.

    A fresh acquisition omits ``generation`` and receives a newly minted value. An
    active same-holder renewal supplies its current generation so the epoch remains
    stable. The command owns that mode decision under the same writer transaction.
    This function performs the unconditional upsert once those checks pass.

    Set ``holder_id`` and an ``expires_at`` of now + ``lease_seconds``, replacing
    any existing row (the PRIMARY KEY makes this one lease).
    The CONFLICT check — refusing to steal another holder's ACTIVE lease — is the
    command's job, run under the same write lock; this is the unconditional write it
    performs once that check passes. ``commit=False`` folds it into the command's
    transaction with the audit event. Returns the fresh lease."""
    lease_generation = secrets.token_hex(16) if generation is None else generation
    if not is_valid_generation(lease_generation):
        raise ValueError("invalid issue lease generation")
    conn.execute(
        "INSERT INTO issue_leases "
        "(issue_id, holder_id, claimed_at, expires_at, generation) "
        "VALUES (?, ?, datetime('now'), datetime('now', ?), ?) "
        "ON CONFLICT(issue_id) DO UPDATE SET "
        "holder_id = excluded.holder_id, claimed_at = excluded.claimed_at, "
        "expires_at = excluded.expires_at, generation = excluded.generation",
        (issue_id, holder_id, f"+{lease_seconds} seconds", lease_generation),
    )
    if commit:
        conn.commit()
    lease = get_lease(conn, issue_id)
    assert lease is not None
    return lease


def delete_lease(
    conn: sqlite3.Connection,
    issue_id: int,
    generation: str,
    *,
    commit: bool = True,
) -> bool:
    """Release exactly one lease possession.

    Returns True if the generation-matched row was removed, False if the current row
    belongs to another epoch or is absent. ``commit=False`` folds the fenced release
    into the command's transaction with its event.
    """
    cur = conn.execute(
        "DELETE FROM issue_leases WHERE issue_id = ? AND generation = ?",
        (issue_id, generation),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0
