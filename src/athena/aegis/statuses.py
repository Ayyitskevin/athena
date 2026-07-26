"""Per-project issue statuses — the configurable lifecycle.

A project's statuses are an ordered menu (with categories), not a transition graph:
any status can move to any other, and validation just checks membership. Backlog
issues (no project) have no rows of their own; they use DEFAULT_STATUSES, which is
also what every project is seeded with on creation (so nothing changes until a
project customizes). This module is the one owner of "what statuses exist for a
project" and "is this status a closed/done one".
"""

from __future__ import annotations

import sqlite3

CATEGORIES = ("todo", "doing", "done")

# The built-in lifecycle: seeds every new project AND serves as the status set for
# backlog issues (which have no project to hang project_statuses rows off). Order
# matters — the first entry is the create default.
DEFAULT_STATUSES = (("open", "todo"), ("in_progress", "doing"), ("done", "done"))


def _default_rows() -> list[dict]:
    return [
        {"name": name, "category": category, "position": i}
        for i, (name, category) in enumerate(DEFAULT_STATUSES)
    ]


def seed_defaults(
    conn: sqlite3.Connection, project_id: int, *, commit: bool = True
) -> None:
    """Give a brand-new project the default status set. Called from project create.
    ``commit=False`` lets the create command fold the seed into its own
    transaction, so a project and its starting statuses land or roll back together."""
    for i, (name, category) in enumerate(DEFAULT_STATUSES):
        conn.execute(
            "INSERT INTO project_statuses (project_id, name, category, position) "
            "VALUES (?, ?, ?, ?)",
            (project_id, name, category, i),
        )
    if commit:
        conn.commit()


def list_statuses(conn: sqlite3.Connection, project_id: int | None) -> list[dict]:
    """A project's ordered statuses, or the default set for the backlog (project_id
    is None). Falls back to the default set if a project somehow has no rows, so a
    status menu is never empty."""
    if project_id is None:
        return _default_rows()
    rows = conn.execute(
        "SELECT name, category, position FROM project_statuses "
        "WHERE project_id = ? ORDER BY position, id",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows] or _default_rows()


def status_names(conn: sqlite3.Connection, project_id: int | None) -> list[str]:
    return [s["name"] for s in list_statuses(conn, project_id)]


def is_valid(conn: sqlite3.Connection, project_id: int | None, name: str) -> bool:
    return any(s["name"] == name for s in list_statuses(conn, project_id))


def category_of(
    conn: sqlite3.Connection, project_id: int | None, name: str
) -> str | None:
    for s in list_statuses(conn, project_id):
        if s["name"] == name:
            return s["category"]
    return None


def is_done(conn: sqlite3.Connection, project_id: int | None, name: str) -> bool:
    """Whether a status is a CLOSED one — its category is 'done'. This replaces the
    old literal `status == 'done'` check, so a project's custom closed state (e.g.
    'shipped') counts as done too."""
    return category_of(conn, project_id, name) == "done"


def first_status(conn: sqlite3.Connection, project_id: int | None) -> str:
    """The default status for a new issue in this project (its first, by position)."""
    options = list_statuses(conn, project_id)
    return options[0]["name"] if options else DEFAULT_STATUSES[0][0]


_DEFAULT_CATEGORY = dict(DEFAULT_STATUSES)


def global_category(conn: sqlite3.Connection, name: str) -> str:
    """A best-effort category for a status NAME without knowing its project — used
    only to order the global board's columns, which mixes issues from projects with
    different status sets. Prefers the built-in default mapping, then any project's
    row for that name, then the neutral middle ('doing')."""
    if name in _DEFAULT_CATEGORY:
        return _DEFAULT_CATEGORY[name]
    row = conn.execute(
        "SELECT category FROM project_statuses WHERE name = ? LIMIT 1", (name,)
    ).fetchone()
    return row["category"] if row else "doing"


# --- management (per-project; the backlog's default set is fixed) -----------

# The refusal reasons, named so the command owner (aegis/status_commands.py) can
# map each to a status code by identity rather than by matching a substring of
# prose that a later edit would silently reword.
REASON_NAME_REQUIRED = "status name is required"
REASON_BAD_CATEGORY = f"category must be one of: {', '.join(CATEGORIES)}"
REASON_DUPLICATE = "a status with that name already exists"
REASON_UNKNOWN = "no such status"
REASON_LAST_STATUS = "a project must keep at least one status"
REASON_IN_USE = "reassign the issues using this status first"


def _name_taken(conn: sqlite3.Connection, project_id: int, name: str) -> bool:
    """Whether the project already has this status name, under the collation the
    UNIQUE constraint actually uses.

    Migration 0024 declares ``UNIQUE (project_id, name COLLATE NOCASE)``, but
    ``is_valid`` compares with Python ``==``. Adding "Open" to a project that
    already had "open" therefore passed validation and died on the constraint —
    an unhandled sqlite3.IntegrityError, i.e. a 500 on both transports. The
    NOCASE probe is deliberately LOCAL to this write rather than folded into
    ``is_valid``: ``is_valid`` is the validator for issue status writes, and
    making it case-insensitive would let "Open" pass there and then fall off
    ``category_of`` (which still compares exactly), turning this 500 into a worse
    one inside an issue's write transaction.
    """
    row = conn.execute(
        "SELECT 1 FROM project_statuses "
        "WHERE project_id = ? AND name = ? COLLATE NOCASE LIMIT 1",
        (project_id, name),
    ).fetchone()
    return row is not None


def add_status(
    conn: sqlite3.Connection,
    project_id: int,
    name: str,
    category: str,
    *,
    commit: bool = True,
) -> str | None:
    """Append a status to a project. Returns None on success, else one of the
    REASON_* constants the caller turns into an error (same predicate shape as
    pages.validate_move). ``commit=False`` lets ``status_commands`` fold this into
    the transaction that also carries its authorization check and audit event."""
    name = name.strip()
    if not name:
        return REASON_NAME_REQUIRED
    if category not in CATEGORIES:
        return REASON_BAD_CATEGORY
    if _name_taken(conn, project_id, name):
        return REASON_DUPLICATE
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM project_statuses "
        "WHERE project_id = ?",
        (project_id,),
    ).fetchone()["p"]
    conn.execute(
        "INSERT INTO project_statuses (project_id, name, category, position) "
        "VALUES (?, ?, ?, ?)",
        (project_id, name, category, position),
    )
    if commit:
        conn.commit()
    return None


def remove_status(
    conn: sqlite3.Connection, project_id: int, name: str, *, commit: bool = True
) -> str | None:
    """Remove a status from a project. Refuses to remove the last one, or one that
    issues in the project still use (reassign them first). Returns None on success,
    else one of the REASON_* constants.

    The in-use count includes ARCHIVED issues on purpose: an archived issue can be
    restored, and restoring one into a status the project no longer has would leave
    a row that fails ``is_valid`` and so cannot be written by any issue command.
    ``commit=False`` lets ``status_commands`` own the transaction, which also closes
    the window where an issue could be moved INTO this status between the count and
    the DELETE."""
    current = list_statuses(conn, project_id)
    if not any(s["name"] == name for s in current):
        return REASON_UNKNOWN
    if len(current) <= 1:
        return REASON_LAST_STATUS
    in_use = conn.execute(
        "SELECT COUNT(*) AS n FROM issues WHERE project_id = ? AND status = ?",
        (project_id, name),
    ).fetchone()["n"]
    if in_use > 0:
        return REASON_IN_USE
    conn.execute(
        "DELETE FROM project_statuses WHERE project_id = ? AND name = ?",
        (project_id, name),
    )
    if commit:
        conn.commit()
    return None
