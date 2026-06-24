"""Data access for projects — a named grouping of issues.

All project SQL lives here, mirroring aegis/issues.py and aegis/labels.py. A
project is a container an issue may belong to (or not). The issue<->project link
is a single nullable column on the issue (issues.project_id), so unlike labels
there is no join table — issues.py owns that column and filters on it directly.
"""
from __future__ import annotations

import sqlite3


def create_project(
    conn: sqlite3.Connection, *, name: str, created_by: int, description: str = ""
) -> dict:
    """Insert a project and return it. Raises sqlite3.IntegrityError if a project
    with this name already exists (name is UNIQUE, case-insensitive) or if
    created_by isn't a real user (the foreign key refuses the orphan)."""
    cur = conn.execute(
        "INSERT INTO projects (name, description, created_by) VALUES (?, ?, ?)",
        (name, description, created_by),
    )
    conn.commit()
    return get_project(conn, cur.lastrowid)


def get_project(conn: sqlite3.Connection, project_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row else None


def get_project_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    """Look a project up by name (case-insensitive — the column is COLLATE NOCASE)."""
    row = conn.execute(
        "SELECT * FROM projects WHERE name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    """Every project, alphabetical."""
    rows = conn.execute(
        "SELECT * FROM projects ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [dict(row) for row in rows]


def update_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict | None:
    """Partial edit: only the fields passed as non-None are written, the rest are
    left as-is (mirrors issues.update_issue). Returns the updated project, or None
    if no project has that id. Raises sqlite3.IntegrityError if the new name
    collides with another project (name is UNIQUE, case-insensitive) — the
    boundary checks for a duplicate first and returns a clean 409."""
    sets, params = [], []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if not sets:
        return get_project(conn, project_id)
    params.append(project_id)
    cur = conn.execute(
        f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_project(conn, project_id)


def delete_project(conn: sqlite3.Connection, project_id: int) -> bool:
    """Delete a project. Returns True if a project was deleted, False if no
    project had that id (so the boundary can 404).

    PRECONDITION: no issue belongs to this project. The caller checks
    issues.count_issues_in_project first and returns a clean 409 — we do NOT
    cascade or detach, because reassigning a pile of issues (or orphaning them) is
    a data decision a delete must not make silently. (If a stray issue somehow
    remained, the issues.project_id foreign key has no ON DELETE, so it would
    refuse the delete and raise rather than orphan it.)"""
    cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    return cur.rowcount > 0
