"""Data access for the activity log: an append-only audit trail.

Every meaningful write in Athena (an issue created, a status changed, an
assignment) records one row here through record(), stamped with WHO did it. This
is the history the architecture promises — "Grok closed AEGIS-88" as a recorded
fact, not a guess. All activity SQL lives here, mirroring aegis/issues.py and
aegis/comments.py.
"""

from __future__ import annotations

import csv
from io import StringIO
import sqlite3

# Every read returns the actor's display name alongside the row, so a feed can
# render "Kevin closed AEGIS-12" without a second lookup.
_SELECT = (
    "SELECT a.*, u.name AS actor_name FROM activity a JOIN users u ON u.id = a.actor_id"
)


def _like_pattern(value: str) -> str:
    """Return a literal LIKE pattern for operator search text."""
    escaped = (
        value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


_CSV_FIELDS = [
    "id",
    "created_at",
    "actor_id",
    "actor_name",
    "verb",
    "target_kind",
    "target_id",
    "detail",
]


def to_csv(rows: list[dict]) -> str:
    """Serialize activity rows to a stable operator-export CSV."""
    out = StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=_CSV_FIELDS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def record(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    verb: str,
    target_kind: str,
    target_id: int,
    detail: str = "",
) -> dict:
    """Append one activity row and return it. Raises sqlite3.IntegrityError if
    actor_id isn't a real user (the foreign key refuses the orphan). Callers pass
    a controlled verb; this layer only writes."""
    cur = conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (actor_id, verb, target_kind, target_id, detail),
    )
    conn.commit()
    return get_activity(conn, cur.lastrowid)


def get_activity(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE a.id = ?", (activity_id,)).fetchone()
    return dict(row) if row else None


def list_activity(
    conn: sqlite3.Connection,
    *,
    target_kind: str | None = None,
    target_id: int | None = None,
    actor_id: int | None = None,
    verb: str | None = None,
    search: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Activity newest first. Every filter is optional and independent: pass
    target_kind+target_id for one target's timeline, target_kind alone to scope
    the global feed to a kind, actor_id/verb/search to narrow who/what. before_id
    is the paging cursor — only rows older than it (a.id < before_id), so the
    caller can walk back through history one page at a time on a stable,
    append-only ordering."""
    clauses: list[str] = []
    params: list = []
    if target_kind is not None:
        clauses.append("a.target_kind = ?")
        params.append(target_kind)
    if target_id is not None:
        clauses.append("a.target_id = ?")
        params.append(target_id)
    if actor_id is not None:
        clauses.append("a.actor_id = ?")
        params.append(actor_id)
    if verb is not None:
        clauses.append("a.verb = ?")
        params.append(verb)
    if search is not None and search.strip():
        pattern = _like_pattern(search)
        clauses.append(
            "("
            "u.name LIKE ? ESCAPE '\\' OR "
            "a.verb LIKE ? ESCAPE '\\' OR "
            "a.target_kind LIKE ? ESCAPE '\\' OR "
            "CAST(a.target_id AS TEXT) LIKE ? ESCAPE '\\' OR "
            "a.detail LIKE ? ESCAPE '\\' OR "
            "a.created_at LIKE ? ESCAPE '\\' OR "
            "(a.target_kind || ' #' || a.target_id) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern] * 7)
    if before_id is not None:
        clauses.append("a.id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id DESC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def distinct_verbs(conn: sqlite3.Connection) -> list[str]:
    """The verbs that actually occur in the trail, alphabetical. Powers the feed's
    verb filter from real data — never a hardcoded list that could drift from what
    the recorders emit."""
    rows = conn.execute("SELECT DISTINCT verb FROM activity ORDER BY verb").fetchall()
    return [row["verb"] for row in rows]


def distinct_target_kinds(conn: sqlite3.Connection) -> list[str]:
    """The target kinds that actually occur in the trail, alphabetical. Same
    honesty rule as distinct_verbs — only kinds something has recorded against."""
    rows = conn.execute(
        "SELECT DISTINCT target_kind FROM activity ORDER BY target_kind"
    ).fetchall()
    return [row["target_kind"] for row in rows]
