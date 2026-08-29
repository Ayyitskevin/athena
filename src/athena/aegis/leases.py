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
import json
import secrets
import sqlite3
from typing import TypeGuard

from athena.aegis import statuses

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
    "l.claimed_at, l.expires_at, l.generation, l.declared_paths, "
    "(l.expires_at > datetime('now')) AS active "
    "FROM issue_leases l JOIN users u ON u.id = l.holder_id"
)

#: The same projection as ``_SELECT`` with the clock supplied as a PARAMETER
#: rather than ``datetime('now')``, so a holder read derives ``active`` from the
#: caller's injected stamp — the one the desk already uses for its other lanes.
#: Callers append their own WHERE/ORDER/LIMIT and pass the stamp first.
_HELD_SELECT = (
    "SELECT l.issue_id, l.holder_id, u.name AS holder_name, "
    "l.claimed_at, l.expires_at, l.generation, l.declared_paths, "
    "(l.expires_at > ?) AS active "
    "FROM issue_leases l JOIN users u ON u.id = l.holder_id "
)


def _clock_stamp(now: datetime | None) -> str:
    """The comparison stamp in the storage format ``expires_at`` uses."""
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S")


def _parse_declared_paths(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _row_to_lease(row: sqlite3.Row) -> dict:
    lease = dict(row)
    lease["active"] = bool(lease["active"])  # SQLite returns 0/1 for the comparison
    lease["declared_paths"] = _parse_declared_paths(lease.get("declared_paths"))
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
        "l.claimed_at, l.expires_at, l.generation, l.declared_paths, "
        "(l.expires_at > ?) AS active "
        "FROM issue_leases l JOIN users u ON u.id = l.holder_id "
        "WHERE l.holder_id = ? ORDER BY l.claimed_at DESC, l.issue_id DESC LIMIT ?",
        (stamp, holder_id, bounded),
    ).fetchall()
    return [_row_to_lease(row) for row in rows]


def list_active_leases(
    conn: sqlite3.Connection, *, except_issue_id: int | None = None
) -> list[dict]:
    """Every currently active lease, optionally excluding one issue.

    Used to fence declared paths across issues. ``active`` is the clock, same as
    ``get_lease``.
    """
    sql = f"{_SELECT} WHERE l.expires_at > datetime('now')"
    params: list[object] = []
    if except_issue_id is not None:
        sql += " AND l.issue_id != ?"
        params.append(except_issue_id)
    return [_row_to_lease(row) for row in conn.execute(sql, params).fetchall()]


def active_leases_held_by(
    conn: sqlite3.Connection,
    *,
    holder_id: int,
    limit: int = 20,
    now: datetime | None = None,
) -> list[dict]:
    """One holder's leases that are STILL LIVE by the clock — "what am I holding
    right now?", most recently claimed first.

    The strict half of ``leases_held_by``. That reader deliberately includes
    expired rows and hands the caller an ``active`` flag to sort out; this one
    answers the narrower question a caller has when the word "held" has to be
    true — a row here is a possession no one else can take. ``active`` is still
    returned (always True) so the projection stays identical to the other holder
    reads rather than a special shape callers have to branch on.
    """
    bounded = max(1, min(int(limit), 100))
    stamp = _clock_stamp(now)
    rows = conn.execute(
        _HELD_SELECT + "WHERE l.holder_id = ? AND l.expires_at > ? "
        "ORDER BY l.claimed_at DESC, l.issue_id DESC LIMIT ?",
        (stamp, holder_id, stamp, bounded),
    ).fetchall()
    return [_row_to_lease(row) for row in rows]


def count_active_leases_held_by(
    conn: sqlite3.Connection,
    *,
    holder_id: int,
    now: datetime | None = None,
) -> int:
    """How many leases this holder still holds by the clock, so a clipped list
    can say so. Takes the same injectable ``now`` as the list read: a total
    counted against a different instant than the items it bounds is a total that
    can disagree with its own list."""
    stamp = _clock_stamp(now)
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM issue_leases "
            "WHERE holder_id = ? AND expires_at > ?",
            (holder_id, stamp),
        ).fetchone()["n"]
    )


def lapsed_leases_held_by(
    conn: sqlite3.Connection,
    *,
    holder_id: int,
    limit: int = 20,
    now: datetime | None = None,
) -> tuple[list[dict], int]:
    """This holder's EXPIRED rows on issues that are still open, newest first,
    with the count of everything that passed the filter.

    A lease row outlives its window: nothing sweeps it, and only the next
    acquisition or the holder's own release removes it. So "the clock released
    this and nobody took it back" is a real fact its last holder has to act on —
    renew it, or clear it — and it belongs on a surface that says so, not folded
    in among the leases they actually hold.

    Rows on issues whose status category is ``done`` are omitted: there is no
    possession left to lose on finished work, and surfacing it would grow an
    unclearable list on every seat. The row itself is not hidden — it stays
    readable at ``get_lease`` for as long as it exists, which is where "who held
    this last" is answered.

    The returned count is the count AFTER that filter, not the number of expired
    rows: a total that counts rows the list can never show would be the same
    lie in a smaller font. The filter runs in Python (a holder has at most one
    row per issue, so this is bounded by their own lease count) rather than
    duplicating the delegation inbox's category SQL, which would put a second
    copy of "what closed means" in the tree.
    """
    bounded = max(1, min(int(limit), 100))
    stamp = _clock_stamp(now)
    rows = conn.execute(
        _HELD_SELECT + "WHERE l.holder_id = ? AND l.expires_at <= ? "
        "ORDER BY l.claimed_at DESC, l.issue_id DESC",
        (stamp, holder_id, stamp),
    ).fetchall()
    still_open = []
    for row in rows:
        issue = conn.execute(
            "SELECT project_id, status FROM issues WHERE id = ?",
            (row["issue_id"],),
        ).fetchone()
        if issue is None:
            continue
        if statuses.is_done(conn, issue["project_id"], issue["status"]):
            continue
        still_open.append(_row_to_lease(row))
    return still_open[:bounded], len(still_open)


def upsert_lease(
    conn: sqlite3.Connection,
    issue_id: int,
    holder_id: int,
    lease_seconds: int,
    generation: str | None = None,
    declared_paths: list[str] | None = None,
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
    paths_json = json.dumps(list(declared_paths or []))
    conn.execute(
        "INSERT INTO issue_leases "
        "(issue_id, holder_id, claimed_at, expires_at, generation, declared_paths) "
        "VALUES (?, ?, datetime('now'), datetime('now', ?), ?, ?) "
        "ON CONFLICT(issue_id) DO UPDATE SET "
        "holder_id = excluded.holder_id, claimed_at = excluded.claimed_at, "
        "expires_at = excluded.expires_at, generation = excluded.generation, "
        "declared_paths = excluded.declared_paths",
        (
            issue_id,
            holder_id,
            f"+{lease_seconds} seconds",
            lease_generation,
            paths_json,
        ),
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
