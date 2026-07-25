"""Data access for projects — a named grouping of issues.

All project SQL lives here, mirroring aegis/issues.py and aegis/labels.py. A
project is a container an issue may belong to (or not). The issue<->project link
is a single nullable column on the issue (issues.project_id), so unlike labels
there is no join table — issues.py owns that column and filters on it directly.
"""

from __future__ import annotations

import re
import secrets
import sqlite3

from athena.aegis import statuses

# A valid issue-key prefix: a leading letter, then up to 9 more letters/digits.
# This is the shape both the [[ATH-12]] cross-link grammar and the addressable
# URL depend on, so it's defined once here and shared by every boundary (REST API
# and web form) that accepts a key.
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}$")


def _row_to_project(row: sqlite3.Row) -> dict:
    """Return public project values with SQLite's policy bit as a real boolean."""
    project = dict(row)
    if "block_agent_closes_when_blocked" in project:
        project["block_agent_closes_when_blocked"] = bool(
            project["block_agent_closes_when_blocked"]
        )
    return project


def normalize_key(key: str) -> str | None:
    """Return the canonical (uppercased) form of a project key, or None if it's not
    a valid key. Callers (the REST API, the web form) turn None into their own
    422/400; storing the uppercased value keeps the key case-insensitively unique."""
    key = key.strip()
    return key.upper() if _KEY_RE.match(key) else None


def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    key: str,
    created_by: int,
    description: str = "",
    commit: bool = True,
) -> dict:
    """Insert a project and return it. Raises sqlite3.IntegrityError if a project
    with this name OR key already exists (both are UNIQUE, case-insensitive) or if
    created_by isn't a real user (the foreign key refuses the orphan).

    key is the issue-key prefix (e.g. "ATH" -> issues ATH-1, ATH-2, ...). It is
    stored uppercased so it is canonical; the boundary validates its shape
    (letters/digits, leading letter) and turns a duplicate into a clean 409.

    ``commit=False`` lets the audited command fold the insert, its default statuses,
    and the 'created' event into one transaction (the project and its trail land or
    roll back together)."""
    cur = conn.execute(
        "INSERT INTO projects "
        "(id, name, key, description, created_by, activity_scope_key) "
        "SELECT next_id, ?, ?, ?, ?, ? FROM activity_target_id_sequences "
        "WHERE target_kind = 'project'",
        (name, key.upper(), description, created_by, secrets.token_hex(16)),
    )
    project_id = cur.lastrowid
    assert project_id is not None
    # Give the new project the default status set, so it has a working lifecycle
    # immediately and can be customized from there.
    statuses.seed_defaults(conn, project_id, commit=False)
    if commit:
        conn.commit()
    project = get_project(conn, project_id)
    assert project is not None
    return project


def get_project(conn: sqlite3.Connection, project_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _row_to_project(row) if row else None


def get_project_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    """Look a project up by name (case-insensitive — the column is COLLATE NOCASE)."""
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    return _row_to_project(row) if row else None


def get_project_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    """Look a project up by its issue-key prefix (case-insensitive — the column is
    COLLATE NOCASE). Used to resolve an issue ref like "ATH-12" and to detect a
    duplicate key before create returns a 409."""
    row = conn.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
    return _row_to_project(row) if row else None


def list_projects(
    conn: sqlite3.Connection, visible_project_ids: set[int] | None = None
) -> list[dict]:
    """Projects the viewer may see, alphabetical. None means no gating — an admin, or
    an internal caller that needs the full set (e.g. building a write-side dropdown);
    a set restricts to those ids; an empty set yields nothing. Callers get the set
    from access.visible_project_filter."""
    if visible_project_ids is None:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY name COLLATE NOCASE"
        ).fetchall()
    elif not visible_project_ids:
        return []
    else:
        placeholders = ",".join("?" for _ in visible_project_ids)
        rows = conn.execute(
            f"SELECT * FROM projects WHERE id IN ({placeholders}) "
            "ORDER BY name COLLATE NOCASE",
            list(visible_project_ids),
        ).fetchall()
    return [_row_to_project(row) for row in rows]


def set_blocked_close_policy(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    enabled: bool,
    commit: bool = True,
) -> dict | None:
    """Set the optional agent blocked-close policy and return the project.

    commit=False lets the governance command fold the flag and its audit event into
    one transaction. The migration's CHECK constraint remains the storage backstop
    even though callers pass a real boolean.
    """
    cur = conn.execute(
        "UPDATE projects SET block_agent_closes_when_blocked = ? WHERE id = ?",
        (1 if enabled else 0, project_id),
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_project(conn, project_id)


def update_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: str | None = None,
    key: str | None = None,
    description: str | None = None,
    commit: bool = True,
) -> dict | None:
    """Partial edit: only the fields passed as non-None are written, the rest are
    left as-is (mirrors issues.update_issue). Returns the updated project, or None
    if no project has that id. Raises sqlite3.IntegrityError if the new name OR key
    collides with another project (both are UNIQUE, case-insensitive) — the
    boundary checks for a duplicate first and returns a clean 409.

    Editing the key only changes the prefix shown in issue keys (ATH-1 -> OPS-1);
    it does NOT renumber anything, because the per-project sequence is independent
    of the prefix. ``commit=False`` lets the audited command fold the edit and its
    'edited_project' event into one transaction."""
    sets: list[str] = []
    params: list[object] = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if key is not None:
        sets.append("key = ?")
        params.append(key.upper())
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if not sets:
        return get_project(conn, project_id)
    params.append(project_id)
    cur = conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params)
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_project(conn, project_id)


def set_visibility(
    conn: sqlite3.Connection, project_id: int, visibility: str, *, commit: bool = True
) -> dict | None:
    """Flip a project public ↔ private. Returns the updated project, or None if no
    project has that id (so the boundary can 404). The boundary validates the value
    ('public' | 'private') before calling; the column's CHECK constraint is the final
    backstop. This is the ONLY writer of projects.visibility — core/access.py reads it
    but never writes it, so there is exactly one owner of the flag (the cardinal
    one-owner rule). Narrowing to 'private' takes effect immediately: every read path
    already filters through access.can_see_project, inert only because nothing was
    private yet. ``commit=False`` lets the audited project-access command fold the
    flip, the creator's roster row, and the activity event into one transaction."""
    cur = conn.execute(
        "UPDATE projects SET visibility = ? WHERE id = ?", (visibility, project_id)
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_project(conn, project_id)


def delete_project(
    conn: sqlite3.Connection, project_id: int, *, commit: bool = True
) -> bool:
    """Delete a project. Returns True if a project was deleted, False if no
    project had that id (so the boundary can 404).

    PRECONDITION: no issue belongs to this project. The caller checks
    issues.count_issues_in_project first and returns a clean 409 — we do NOT
    cascade or detach, because reassigning a pile of issues (or orphaning them) is
    a data decision a delete must not make silently. (If a stray issue somehow
    remained, the issues.project_id foreign key has no ON DELETE, so it would
    refuse the delete and raise rather than orphan it.)

    ``commit=False`` lets the audited command fold the delete and its
    'deleted_project' event into one transaction — the record of who deleted it
    OUTLIVES the project (target_id has no FK), so the trail keeps the fact."""
    cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    if commit:
        conn.commit()
    return cur.rowcount > 0


def next_seq(conn: sqlite3.Connection, project_id: int) -> int:
    """Allocate and return the next per-project issue number for this project.

    issue_counter is a monotonic high-water mark: we bump it and hand back the new
    value, so a number is NEVER reused even after the issue that held it is deleted
    or moved away. This does NOT commit — it runs inside the caller's transaction
    (e.g. create_issue's), so the counter bump and the issue write either both land
    or both roll back, and two concurrent allocators can't be handed the same
    number (the UPDATE takes a write lock on the row)."""
    conn.execute(
        "UPDATE projects SET issue_counter = issue_counter + 1 WHERE id = ?",
        (project_id,),
    )
    return conn.execute(
        "SELECT issue_counter FROM projects WHERE id = ?", (project_id,)
    ).fetchone()["issue_counter"]
