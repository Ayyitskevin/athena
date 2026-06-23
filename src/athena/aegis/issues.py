"""Data access for Aegis issues.

All issue SQL lives here. HTTP handlers call these functions instead of writing
queries, so if the storage ever changes, only this file does.
"""
from __future__ import annotations

import sqlite3

from athena.core import links

# The lifecycle an issue moves through. This is the canonical set the whole app
# agrees on — the REST API and the web forms both validate against it, and the
# boards view lays out one column per status. 'open' is the create default
# (matches the schema). Keep this in sync with templates' status <option>s.
STATUSES = ("open", "in_progress", "done")

# How urgent an issue is, lowest → highest. 'medium' is the create default and
# the value existing rows backfilled to (migration 0006). Like STATUSES, this is
# the one canonical set the REST API and the web forms both validate against.
PRIORITIES = ("low", "medium", "high", "urgent")

# Every read returns the assignee's display name and the project's name
# alongside the row (NULL when unassigned / no project), so callers never resolve
# the ids themselves. LEFT JOINs, not JOINs: an unassigned or project-less issue
# must still come back.
_SELECT = (
    "SELECT i.*, u.name AS assignee_name, p.name AS project_name "
    "FROM issues i "
    "LEFT JOIN users u ON u.id = i.assignee_id "
    "LEFT JOIN projects p ON p.id = i.project_id"
)


def create_issue(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    created_by: int,
    status: str = "open",
    priority: str = "medium",
    project_id: int | None = None,
) -> dict:
    """Insert an issue and return it. Raises sqlite3.IntegrityError if
    created_by isn't a real user, or if project_id is a non-NULL id with no
    matching project (the foreign keys refuse the orphan). project_id is
    optional — None means the issue starts with no project."""
    cur = conn.execute(
        "INSERT INTO issues (title, body, status, priority, created_by, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (title, body, status, priority, created_by, project_id),
    )
    conn.commit()
    # Index any [[issue:N]]/[[page:N]] references this issue's body makes.
    links.sync_links(conn, source_kind="issue", source_id=cur.lastrowid, body=body)
    return get_issue(conn, cur.lastrowid)


def update_issue(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    priority: str | None = None,
) -> dict | None:
    """Partial update: only the fields passed as non-None change. Returns the
    updated issue, or None if no issue has that id (so the caller can 404).
    Field validation (status in STATUSES, priority in PRIORITIES, non-empty
    title) is the boundary's job.

    The column names below are hardcoded literals, never caller input, so the
    f-string assembles a safe SET clause; the values stay parameterized."""
    fields = {
        col: val
        for col, val in (
            ("title", title),
            ("body", body),
            ("status", status),
            ("priority", priority),
        )
        if val is not None
    }
    if not fields:
        # Nothing to change — still distinguish "no such issue" (None) from a
        # real but unchanged issue, so the boundary's 404 stays correct.
        return get_issue(conn, issue_id)
    assignments = ", ".join(f"{col} = ?" for col in fields)
    cur = conn.execute(
        f"UPDATE issues SET {assignments} WHERE id = ?",
        (*fields.values(), issue_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    # Re-index references only when the body actually changed (the only field
    # that carries [[...]] tokens); a status/priority/title edit leaves them be.
    if "body" in fields:
        links.sync_links(
            conn, source_kind="issue", source_id=issue_id, body=fields["body"]
        )
    return get_issue(conn, issue_id)


def update_status(
    conn: sqlite3.Connection, issue_id: int, status: str
) -> dict | None:
    """Move an issue to a new status. Thin wrapper over update_issue, kept as a
    named operation for the status-change call sites (web route + API)."""
    return update_issue(conn, issue_id, status=status)


def set_assignee(
    conn: sqlite3.Connection, issue_id: int, assignee_id: int | None
) -> dict | None:
    """Assign the issue to a user, or clear it (assignee_id=None -> Unassigned).
    Returns the updated issue, or None if no issue has that id. Checking that
    assignee_id is a real user is the boundary's job; the DB's foreign key is
    the backstop (raises sqlite3.IntegrityError on an unknown non-NULL id)."""
    cur = conn.execute(
        "UPDATE issues SET assignee_id = ? WHERE id = ?", (assignee_id, issue_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def set_project(
    conn: sqlite3.Connection, issue_id: int, project_id: int | None
) -> dict | None:
    """Move the issue into a project, or remove it from one (project_id=None ->
    no project). Returns the updated issue, or None if no issue has that id.
    Checking that project_id is a real project is the boundary's job; the DB's
    foreign key is the backstop (raises sqlite3.IntegrityError on an unknown
    non-NULL id). Mirrors set_assignee — a single nullable column, so a dedicated
    operation keeps None ('remove') distinct from PATCH's 'leave unchanged'."""
    cur = conn.execute(
        "UPDATE issues SET project_id = ? WHERE id = ?", (project_id, issue_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def can_modify(issue: dict, actor_id: int) -> bool:
    """Whether an actor may modify this issue (change status, edit, assign).
    The rule: the issue's creator OR its current assignee. An unassigned issue
    (assignee_id is None) can only be modified by its creator until someone is
    assigned. Reads and commenting are open to all authenticated actors and do
    NOT pass through here — this gate is for writes only."""
    return actor_id == issue["created_by"] or actor_id == issue["assignee_id"]


def get_issue(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE i.id = ?", (issue_id,)).fetchone()
    return dict(row) if row else None


def list_issues(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    search: str | None = None,
    project_id: int | None = None,
    ids: list[int] | None = None,
) -> list[dict]:
    """List issues, optionally filtered. This is the ONE filtering path the API
    and the web list both use, so the two never disagree on what matches.

    - status: exact status match.
    - search: case-insensitive substring in title or body (SQLite LIKE).
    - project_id: restrict to issues in this project (a direct column on the
      issue, so unlike labels this module filters it itself).
    - ids: restrict to these issue ids. Generic on purpose — the caller resolves
      *what* the ids mean (e.g. labels.py turns a label name into ids), so this
      module stays decoupled from labels. An empty list means "match nothing".

    Column names below are hardcoded literals; all values stay parameterized."""
    if ids is not None and not ids:
        return []  # an empty id set can't match — and "IN ()" isn't valid SQL
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("i.status = ?")
        params.append(status)
    if search:
        clauses.append("(i.title LIKE ? OR i.body LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    if project_id is not None:
        clauses.append("i.project_id = ?")
        params.append(project_id)
    if ids is not None:
        placeholders = ",".join("?" for _ in ids)
        clauses.append(f"i.id IN ({placeholders})")
        params.extend(ids)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"{_SELECT}{where} ORDER BY i.id", params).fetchall()
    return [dict(row) for row in rows]
