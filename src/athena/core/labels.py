"""Data access for labels — the shared label vocabulary and its joins.

A label is shared vocabulary (e.g. "bug", "frontend") reused across the app. It
lives in core/, not in a feature module, because it is a CROSS-CUTTING concern —
the same shape as core/links and core/attachments, which any kind of thing can
point at. The pairing of a label to an issue lives in the issue_labels join table;
other kinds (e.g. pages) attach through the same vocabulary via their own join.
All label SQL lives here. Attaching/detaching is idempotent so callers don't have
to check first.
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


def _grouped_counts(conn: sqlite3.Connection, sql: str, params: list) -> dict[int, int]:
    return {r["label_id"]: r["n"] for r in conn.execute(sql, params).fetchall()}


def label_usage(
    conn: sqlite3.Connection,
    *,
    visible_project_ids: set[int] | None = None,
    visible_space_ids: set[int] | None = None,
) -> list[dict]:
    """Every label with how many issues and pages carry it — the data behind the
    label index. Two grouped counts (one per join) folded onto the vocabulary, so
    it stays O(1) queries regardless of how many labels there are. Counts include
    archived issues (the label join doesn't know about archival); the per-label view
    is where that distinction, if wanted, would live.

    The counts respect visibility: visible_project_ids / visible_space_ids restrict the
    issue / page tallies to what the viewer may see (a label's count never reveals how
    many hidden items carry it). None means no gating (an admin, or an internal caller)
    — the bare grouped count; a set restricts (issues keep the backlog; an empty set
    means only the backlog for issues, no pages). Labels are shared vocabulary, so a
    label whose usages are all hidden still appears, just at zero."""
    if visible_project_ids is None:
        issue_counts = _grouped_counts(
            conn, "SELECT label_id, COUNT(*) AS n FROM issue_labels GROUP BY label_id", []
        )
    else:
        if visible_project_ids:
            ph = ",".join("?" for _ in visible_project_ids)
            cond, params = f"(i.project_id IS NULL OR i.project_id IN ({ph}))", list(visible_project_ids)
        else:
            cond, params = "i.project_id IS NULL", []
        issue_counts = _grouped_counts(
            conn,
            "SELECT il.label_id AS label_id, COUNT(*) AS n FROM issue_labels il "
            f"JOIN issues i ON i.id = il.issue_id WHERE {cond} GROUP BY il.label_id",
            params,
        )

    if visible_space_ids is None:
        page_counts = _grouped_counts(
            conn, "SELECT label_id, COUNT(*) AS n FROM page_labels GROUP BY label_id", []
        )
    elif visible_space_ids:
        ph = ",".join("?" for _ in visible_space_ids)
        page_counts = _grouped_counts(
            conn,
            "SELECT pl.label_id AS label_id, COUNT(*) AS n FROM page_labels pl "
            f"JOIN pages pg ON pg.id = pl.page_id WHERE pg.space_id IN ({ph}) GROUP BY pl.label_id",
            list(visible_space_ids),
        )
    else:
        page_counts = {}  # no visible spaces → no page tallies
    return [
        {
            **label,
            "issue_count": issue_counts.get(label["id"], 0),
            "page_count": page_counts.get(label["id"], 0),
        }
        for label in list_labels(conn)
    ]


def add_label_to_issue(
    conn: sqlite3.Connection, issue_id: int, label_id: int, *, commit: bool = True
) -> bool:
    """Attach a label to an issue. Idempotent: re-attaching the same pair is a
    no-op (the composite PK + OR IGNORE swallow the duplicate). Returns True if a
    new pairing was created, False if it was already attached — so the caller can
    record the audit event only on a real change. Raises sqlite3.IntegrityError
    if the issue or label doesn't exist (the FKs). ``commit=False`` lets the
    audited command fold the attach and its event into one transaction."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO issue_labels (issue_id, label_id) VALUES (?, ?)",
        (issue_id, label_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def remove_label_from_issue(
    conn: sqlite3.Connection, issue_id: int, label_id: int, *, commit: bool = True
) -> bool:
    """Detach a label from an issue. Returns True if a pairing was removed, False
    if the pair wasn't attached (so the caller can 404). ``commit=False`` lets the
    audited command fold the detach and its event into one transaction."""
    cur = conn.execute(
        "DELETE FROM issue_labels WHERE issue_id = ? AND label_id = ?",
        (issue_id, label_id),
    )
    if commit:
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


def _labels_for_many(
    conn: sqlite3.Connection, join_table: str, id_col: str, ids: list[int]
) -> dict[int, list[dict]]:
    """Labels for many targets in ONE query, returned as {target_id: [labels]}.
    The shared body of labels_for_issues/labels_for_pages — the two joins differ
    only in table and id column, and those are hardcoded literals supplied by the
    wrappers below (never caller input), so the f-string is safe; values stay
    parameterized. Targets with no labels are simply absent from the dict (caller
    defaults to [])."""
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT j.{id_col}, l.* FROM labels l "
        f"JOIN {join_table} j ON j.label_id = l.id "
        f"WHERE j.{id_col} IN ({placeholders}) "
        f"ORDER BY l.name COLLATE NOCASE",
        ids,
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for row in rows:
        d = dict(row)
        target_id = d.pop(id_col)
        out.setdefault(target_id, []).append(d)
    return out


def labels_for_issues(
    conn: sqlite3.Connection, issue_ids: list[int]
) -> dict[int, list[dict]]:
    """Labels for many issues in ONE query. Avoids the N+1 that per-issue lookups
    would cause on list/board views."""
    return _labels_for_many(conn, "issue_labels", "issue_id", issue_ids)


# --- The page<->label join (the Mentor twin of the issue join above) ---------


def add_label_to_page(
    conn: sqlite3.Connection, page_id: int, label_id: int
) -> bool:
    """Attach a label to a page. Idempotent (composite PK + OR IGNORE). Returns True
    if a new pairing was created, False if it was already attached — so the caller
    records the audit event only on a real change. Raises sqlite3.IntegrityError if
    the page or label doesn't exist (the FKs)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO page_labels (page_id, label_id) VALUES (?, ?)",
        (page_id, label_id),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_label_from_page(
    conn: sqlite3.Connection, page_id: int, label_id: int
) -> bool:
    """Detach a label from a page. Returns True if a pairing was removed, False if
    the pair wasn't attached (so the caller can 404)."""
    cur = conn.execute(
        "DELETE FROM page_labels WHERE page_id = ? AND label_id = ?",
        (page_id, label_id),
    )
    conn.commit()
    return cur.rowcount > 0


def page_ids_for_label(conn: sqlite3.Connection, name: str) -> list[int]:
    """The ids of every page carrying the named label (case-insensitive). [] for an
    unknown label, so a label filter naturally matches nothing."""
    rows = conn.execute(
        "SELECT pl.page_id FROM page_labels pl "
        "JOIN labels l ON l.id = pl.label_id WHERE l.name = ?",
        (name,),
    ).fetchall()
    return [row["page_id"] for row in rows]


def labels_for_page(conn: sqlite3.Connection, page_id: int) -> list[dict]:
    """The labels attached to one page, alphabetical."""
    rows = conn.execute(
        "SELECT l.* FROM labels l "
        "JOIN page_labels pl ON pl.label_id = l.id "
        "WHERE pl.page_id = ? ORDER BY l.name COLLATE NOCASE",
        (page_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def labels_for_pages(
    conn: sqlite3.Connection, page_ids: list[int]
) -> dict[int, list[dict]]:
    """Labels for many pages in ONE query — the Mentor twin of labels_for_issues,
    so a space's page list doesn't pay one label query per page."""
    return _labels_for_many(conn, "page_labels", "page_id", page_ids)
