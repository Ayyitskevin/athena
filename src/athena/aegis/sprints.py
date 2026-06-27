"""Data access for sprints — a project's time-boxed iterations.

All sprint SQL lives here, mirroring aegis/projects.py. A sprint belongs to one
project and moves through planned → active → completed. The issue↔sprint link is a
single nullable column on the issue (issues.sprint_id), owned by issues.py (like
issues.project_id), so there is no join table — this module owns the sprint ENTITY
and its lifecycle, issues.py owns the membership column.

The lifecycle rules are enforced here, not in the schema:
  * a sprint starts only from `planned`, and completing only from `active`;
  * a project has at most ONE active sprint at a time (scrum's single cadence);
  * a sprint can't be deleted while issues are still in it (the caller's 409, backed
    by the issues.sprint_id foreign key).
"""
from __future__ import annotations

import sqlite3

PLANNED = "planned"
ACTIVE = "active"
COMPLETED = "completed"
STATES = (PLANNED, ACTIVE, COMPLETED)

# Sentinel for update_sprint: distinguishes "leave this field alone" from an explicit
# None (which clears a nullable date).
_UNSET = object()


class SprintStateError(ValueError):
    """An illegal lifecycle transition (start a non-planned sprint, complete a
    non-active one, or start a second active sprint in a project). The boundary turns
    this into a 409."""


def create_sprint(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    name: str,
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Create a sprint (state `planned`) and return it. Raises sqlite3.IntegrityError
    if project_id isn't a real project (the foreign key)."""
    cur = conn.execute(
        "INSERT INTO sprints (project_id, name, goal, start_date, end_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, name, goal, start_date, end_date),
    )
    conn.commit()
    return get_sprint(conn, cur.lastrowid)


def get_sprint(conn: sqlite3.Connection, sprint_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
    return dict(row) if row else None


def list_sprints(
    conn: sqlite3.Connection,
    *,
    project_id: int | None = None,
    state: str | None = None,
) -> list[dict]:
    """Sprints, newest first. Optionally scoped to one project and/or one state."""
    clauses: list[str] = []
    params: list = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM sprints{where} ORDER BY id DESC", params
    ).fetchall()
    return [dict(row) for row in rows]


def active_sprint(conn: sqlite3.Connection, project_id: int) -> dict | None:
    """The project's active sprint, or None. There is at most one (start_sprint
    enforces it)."""
    row = conn.execute(
        "SELECT * FROM sprints WHERE project_id = ? AND state = ? ORDER BY id LIMIT 1",
        (project_id, ACTIVE),
    ).fetchone()
    return dict(row) if row else None


def update_sprint(
    conn: sqlite3.Connection,
    sprint_id: int,
    *,
    name=_UNSET,
    goal=_UNSET,
    start_date=_UNSET,
    end_date=_UNSET,
) -> dict | None:
    """Partial edit of a sprint's descriptive fields (name/goal/dates); only fields
    passed are written. State is NOT editable here — it moves via start/complete.
    Returns the updated sprint, or None if no sprint has that id. A date may be set
    to None to clear it (the sentinel distinguishes that from 'unchanged')."""
    sets: list[str] = []
    params: list = []
    for column, value in (
        ("name", name),
        ("goal", goal),
        ("start_date", start_date),
        ("end_date", end_date),
    ):
        if value is not _UNSET:
            sets.append(f"{column} = ?")
            params.append(value)
    if not sets:
        return get_sprint(conn, sprint_id)
    params.append(sprint_id)
    cur = conn.execute(
        f"UPDATE sprints SET {', '.join(sets)} WHERE id = ?", params
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_sprint(conn, sprint_id)


def start_sprint(conn: sqlite3.Connection, sprint_id: int) -> dict | None:
    """Move a sprint planned → active. Returns the updated sprint, or None if no
    sprint has that id. Raises SprintStateError if the sprint isn't planned, or if
    its project already has an active sprint (one cadence at a time). Stamps
    start_date with today if it wasn't set."""
    sprint = get_sprint(conn, sprint_id)
    if sprint is None:
        return None
    if sprint["state"] != PLANNED:
        raise SprintStateError("only a planned sprint can be started")
    if active_sprint(conn, sprint["project_id"]) is not None:
        raise SprintStateError("this project already has an active sprint")
    conn.execute(
        "UPDATE sprints SET state = ?, start_date = COALESCE(start_date, date('now')) "
        "WHERE id = ?",
        (ACTIVE, sprint_id),
    )
    conn.commit()
    return get_sprint(conn, sprint_id)


def complete_sprint(conn: sqlite3.Connection, sprint_id: int) -> dict | None:
    """Move a sprint active → completed. Returns the updated sprint, or None if no
    sprint has that id. Raises SprintStateError if the sprint isn't active. Stamps
    end_date with today if it wasn't set. Issues keep their sprint_id — a completed
    sprint stays a record of what it held (moving incomplete work is a later choice,
    not something a completion silently does)."""
    sprint = get_sprint(conn, sprint_id)
    if sprint is None:
        return None
    if sprint["state"] != ACTIVE:
        raise SprintStateError("only an active sprint can be completed")
    conn.execute(
        "UPDATE sprints SET state = ?, end_date = COALESCE(end_date, date('now')) "
        "WHERE id = ?",
        (COMPLETED, sprint_id),
    )
    conn.commit()
    return get_sprint(conn, sprint_id)


def count_issues_in_sprint(conn: sqlite3.Connection, sprint_id: int) -> int:
    """How many issues are in this sprint — the precondition the delete guard checks."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM issues WHERE sprint_id = ?", (sprint_id,)
    ).fetchone()["n"]


def delete_sprint(conn: sqlite3.Connection, sprint_id: int) -> bool:
    """Delete a sprint. Returns True if one was deleted, False if no sprint had that
    id (so the boundary can 404).

    PRECONDITION: no issue is in this sprint. The caller checks count_issues_in_sprint
    first and returns a clean 409 — we do NOT cascade or detach, because moving a
    pile of issues is a data decision a delete must not make silently. (If a stray
    issue remained, the issues.sprint_id foreign key would refuse the delete anyway.)
    """
    cur = conn.execute("DELETE FROM sprints WHERE id = ?", (sprint_id,))
    conn.commit()
    return cur.rowcount > 0
