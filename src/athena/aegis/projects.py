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
