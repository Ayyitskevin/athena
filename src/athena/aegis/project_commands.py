"""Application commands for the audited project lifecycle.

A project is a workspace container: creating one, renaming/re-keying it, or
deleting it are exactly the "a container appeared or vanished — who did it?"
moments the append-only log exists to answer, yet the bare data-layer writes
recorded nothing. These commands own each write: the row change and its activity
event run in one db.transaction, so a project can never be created, edited, or
destroyed without its trail entry.

Same shape as the other command modules (user_commands, comment_commands, ...).
Authorization and the duplicate-name/key and non-empty-issues preconditions stay
at the transport boundary (the REST/web routes already enforce them and turn a
clash into a clean 409); the command owns the atomic write + audit. Visibility
changes and membership already have their own audited path in project_activity;
this fills the create / edit / delete gap.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import project_activity, projects
from athena.core import db


def create_project(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    name: str,
    key: str,
    description: str = "",
) -> dict:
    """Create a project (with its default statuses) and record a 'created_project'
    event atomically. Raises sqlite3.IntegrityError for a duplicate name/key —
    unchanged from the bare call, so the boundary keeps turning it into a 409."""
    with db.transaction(conn, immediate=True):
        project = projects.create_project(
            conn,
            name=name,
            key=key,
            created_by=actor_id,
            description=description,
            commit=False,
        )
        project_activity.record_project_created(
            conn,
            actor_id=actor_id,
            project_id=project["id"],
            name=project["name"],
            key=project["key"],
        )
        return project


def _edit_summary(before: dict, after: dict) -> str:
    """Human-readable diff of the fields an edit can touch, for the event detail.
    Empty when nothing actually changed (the caller then records nothing)."""
    parts = []
    if before["name"] != after["name"]:
        parts.append(f"name {before['name']!r} → {after['name']!r}")
    if before["key"] != after["key"]:
        parts.append(f"key {before['key']} → {after['key']}")
    if before.get("description", "") != after.get("description", ""):
        parts.append("description")
    return ", ".join(parts)


def update_project(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    project_id: int,
    name: str | None = None,
    key: str | None = None,
    description: str | None = None,
) -> dict | None:
    """Edit a project and record an 'edited_project' event atomically. Returns the
    updated project, or None if no project has that id (so the boundary can 404).
    No event when nothing actually changed — a no-op edit is not a lifecycle
    moment. Raises sqlite3.IntegrityError for a name/key collision, unchanged."""
    with db.transaction(conn, immediate=True):
        before = projects.get_project(conn, project_id)
        if before is None:
            return None
        updated = projects.update_project(
            conn,
            project_id,
            name=name,
            key=key,
            description=description,
            commit=False,
        )
        summary = _edit_summary(before, updated)
        if summary:
            project_activity.record_project_edited(
                conn, actor_id=actor_id, project_id=project_id, changes=summary
            )
        return updated


def delete_project(conn: sqlite3.Connection, *, actor_id: int, project_id: int) -> bool:
    """Delete a project and record a 'deleted_project' event atomically. Returns
    True if a project was deleted, False if none had that id.

    The event is recorded BEFORE the row is removed so its visibility scope still
    resolves from the live project; the event then OUTLIVES the target it names
    (activity.target_id has no FK), preserving who deleted which container."""
    with db.transaction(conn, immediate=True):
        project = projects.get_project(conn, project_id)
        if project is None:
            return False
        project_activity.record_project_deleted(
            conn,
            actor_id=actor_id,
            project_id=project_id,
            name=project["name"],
            key=project["key"],
        )
        return projects.delete_project(conn, project_id, commit=False)
