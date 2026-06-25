"""Data access for labels and the issue<->label join.

All label SQL lives here, mirroring aegis/issues.py and aegis/comments.py. A
label is shared vocabulary reused across issues; the pairing of a label to an
issue lives in the issue_labels join table. Attaching/detaching is idempotent so
callers don't have to check first.
"""
from __future__ import annotations

import re
import sqlite3

_DEFAULT_COLOR = "#6b7280"
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_color(color: str = _DEFAULT_COLOR) -> str:
    """Return a canonical safe label color or reject unsafe CSS text."""
    candidate = (color or _DEFAULT_COLOR).strip()
    if not _HEX_COLOR_RE.fullmatch(candidate):
        raise ValueError("label color must be a #RRGGBB hex color")
    return candidate.lower()


def create_label(
    conn: sqlite3.Connection, *, name: str, color: str = _DEFAULT_COLOR
) -> dict:
    """Insert a label and return it. Raises sqlite3.IntegrityError if a label
    with this name already exists (name is UNIQUE, case-insensitive)."""
    color = normalize_color(color)
    cur = conn.execute(
        "INSERT INTO labels (name, color) VALUES (?, ?)", (name, color)
    )
    conn.commit()
    return get_label(conn, cur.lastrowid)


def get_label(conn: sqlite3.Connection, label_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM labels WHERE id = ?", (label_id,)
    ).fetchone()
    return dict(row) if row else None


def get_label_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    """Look a label up by name (case-insensitive — the column is COLLATE NOCASE)."""
    row = conn.execute(
        "SELECT * FROM labels WHERE name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def get_or_create_label(
    conn: sqlite3.Connection, *, name: str, color: str = _DEFAULT_COLOR
) -> dict:
    """Return the existing label with this name, or create it. Lets the web layer
    attach a label by typing its name without a separate "create label" step."""
    existing = get_label_by_name(conn, name)
    if existing is not None:
        return existing
    return create_label(conn, name=name, color=color)


def list_labels(conn: sqlite3.Connection) -> list[dict]:
    """Every label, alphabetical."""
    rows = conn.execute(
        "SELECT * FROM labels ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [dict(row) for row in rows]


def add_label_to_issue(
    conn: sqlite3.Connection, issue_id: int, label_id: int
) -> bool:
    """Attach a label to an issue. Idempotent: re-attaching the same pair is a
    no-op (the composite PK + OR IGNORE swallow the duplicate). Returns True if a
    new pairing was created, False if it was already attached — so the caller can
    record the audit event only on a real change. Raises sqlite3.IntegrityError
    if the issue or label doesn't exist (the FKs)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO issue_labels (issue_id, label_id) VALUES (?, ?)",
        (issue_id, label_id),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_label_from_issue(
    conn: sqlite3.Connection, issue_id: int, label_id: int
) -> bool:
    """Detach a label from an issue. Returns True if a pairing was removed, False
    if the pair wasn't attached (so the caller can 404)."""
    cur = conn.execute(
        "DELETE FROM issue_labels WHERE issue_id = ? AND label_id = ?",
        (issue_id, label_id),
    )
    conn.commit()
    return cur.rowcount > 0


def issue_ids_for_label(conn: sqlite3.Connection, name: str) -> list[int]:
    """The ids of every issue carrying the named label (case-insensitive). Lets a
    caller filter the issue list by label without coupling issues.py to the join
    — issues.list_issues takes the resolved ids, this resolves them. Returns []
    for an unknown label (so the filter naturally matches nothing)."""
    rows = conn.execute(
        "SELECT il.issue_id FROM issue_labels il "
        "JOIN labels l ON l.id = il.label_id WHERE l.name = ?",
        (name,),
    ).fetchall()
    return [row["issue_id"] for row in rows]


def labels_for_issue(conn: sqlite3.Connection, issue_id: int) -> list[dict]:
    """The labels attached to one issue, alphabetical."""
    rows = conn.execute(
        "SELECT l.* FROM labels l "
        "JOIN issue_labels il ON il.label_id = l.id "
        "WHERE il.issue_id = ? ORDER BY l.name COLLATE NOCASE",
        (issue_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def labels_for_issues(
    conn: sqlite3.Connection, issue_ids: list[int]
) -> dict[int, list[dict]]:
    """Labels for many issues in ONE query, returned as {issue_id: [labels]}.
    Avoids the N+1 that per-issue lookups would cause on list/board views. Issues
    with no labels are simply absent from the dict (caller defaults to [])."""
    if not issue_ids:
        return {}
    placeholders = ",".join("?" for _ in issue_ids)
    rows = conn.execute(
        f"SELECT il.issue_id, l.* FROM labels l "
        f"JOIN issue_labels il ON il.label_id = l.id "
        f"WHERE il.issue_id IN ({placeholders}) "
        f"ORDER BY l.name COLLATE NOCASE",
        issue_ids,
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for row in rows:
        d = dict(row)
        issue_id = d.pop("issue_id")
        out.setdefault(issue_id, []).append(d)
    return out
