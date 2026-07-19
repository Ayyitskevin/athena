"""Data access for issue contributors — the delegation / teammate set on an issue.

An issue has ONE primary assignee (the accountable owner) and any number of
CONTRIBUTORS: additional people or agents working it. Delegating an issue to a
teammate — a human, or a token-driven agent — is adding them as a contributor while
the assignee stays accountable. The pairing lives in issue_contributors; attaching
and detaching are idempotent so callers never have to pre-check.

Mirrors aegis/labels.py (an issue↔X join with a name resolved on read).
"""
from __future__ import annotations

import sqlite3

# Every read resolves the contributor's display name (and who added them), so the
# UI never has to look ids up separately.
_SELECT = (
    "SELECT ic.user_id, u.name AS name, u.is_agent AS is_agent, "
    "ic.added_by, ic.added_at "
    "FROM issue_contributors ic JOIN users u ON u.id = ic.user_id"
)


def add_contributor(
    conn: sqlite3.Connection,
    issue_id: int,
    user_id: int,
    added_by: int,
    *,
    commit: bool = True,
) -> bool:
    """Add a contributor to an issue. Idempotent: re-adding the same pair is a no-op
    (composite PK + OR IGNORE swallow the duplicate). Returns True if a NEW pairing
    was created, False if it was already there — so the caller records the audit
    event only on a real change. Raises sqlite3.IntegrityError if the issue or user
    doesn't exist (the FKs). ``commit=False`` lets the audited command fold the add
    and its event into one transaction."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO issue_contributors (issue_id, user_id, added_by) "
        "VALUES (?, ?, ?)",
        (issue_id, user_id, added_by),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def remove_contributor(
    conn: sqlite3.Connection, issue_id: int, user_id: int, *, commit: bool = True
) -> bool:
    """Remove a contributor. Returns True if a pairing was removed, False if the
    user wasn't a contributor (so the caller can 404). ``commit=False`` lets the
    audited command fold the removal and its event into one transaction."""
    cur = conn.execute(
        "DELETE FROM issue_contributors WHERE issue_id = ? AND user_id = ?",
        (issue_id, user_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def is_contributor(conn: sqlite3.Connection, issue_id: int, user_id: int) -> bool:
    """Whether user_id is on this issue's contributor set — i.e. the issue was
    delegated to them (or they were added as a teammate). The write-permission
    gate (issues.can_act_on) uses this so a delegated agent can actually finish
    the work it was handed."""
    return (
        conn.execute(
            "SELECT 1 FROM issue_contributors WHERE issue_id = ? AND user_id = ?",
            (issue_id, user_id),
        ).fetchone()
        is not None
    )


def list_contributors(conn: sqlite3.Connection, issue_id: int) -> list[dict]:
    """An issue's contributors, alphabetical by display name."""
    rows = conn.execute(
        f"{_SELECT} WHERE ic.issue_id = ? ORDER BY u.name COLLATE NOCASE",
        (issue_id,),
    ).fetchall()
    return [dict(row) for row in rows]
