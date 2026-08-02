"""Application commands for the audited sprint lifecycle.

A sprint is a project's iteration container: creating one, renaming or re-dating
it, starting it, completing it, or deleting it are all durable writes that shape
what the fleet is working on — yet every one of them was a bare data-layer write
with NO activity event on ANY transport. The append-only trail could not answer
"who started this sprint", "who moved its dates", or "who deleted the iteration
that held last week's work", and the sprint surface was absent from the
command-migration inventory entirely.

These commands own each write: the row change and its activity event run in one
``db.transaction``, so a sprint's lifecycle can never move without its trail
entry. REST and the browser are thin callers.

Same shape as the neighbouring command modules. Authorization (the project-write
gate) and the non-empty-sprint delete precondition stay at the transport
boundary, as they do for ``project_commands``; the command owns the atomic
write + audit emission. Events are recorded here directly rather than through a
separate recorder module — the sprint verbs carry no derived projections, so the
indirection ``project_activity`` needs would buy nothing.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import sprints
from athena.core import activity, db

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_CREATED = "sprint_created"
VERB_EDITED = "sprint_edited"
VERB_STARTED = "sprint_started"
VERB_COMPLETED = "sprint_completed"
VERB_DELETED = "sprint_deleted"


class SprintCommandError(Exception):
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND``, so the
    command layer states what went wrong and the transport decides how to say
    it — the same shape every command module written since uses."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _record(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    verb: str,
    sprint_id: int,
    detail: str,
) -> None:
    activity.record(
        conn,
        actor_id=actor_id,
        verb=verb,
        target_kind="sprint",
        target_id=sprint_id,
        detail=detail,
        commit=False,
    )


def create_sprint(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    project_id: int,
    name: str,
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Create a sprint atomically with its ``sprint_created`` event. The detail is
    the sprint's name, so the feed reads "created <name>". Transport-shape
    validation (a non-empty name, a project the actor may write) is the boundary's
    job; the foreign key is the backstop and still raises
    sqlite3.IntegrityError for an unknown project, unchanged."""
    with db.transaction(conn, immediate=True):
        sprint = sprints.create_sprint(
            conn,
            project_id=project_id,
            name=name,
            goal=goal,
            start_date=start_date,
            end_date=end_date,
            commit=False,
        )
        _record(
            conn,
            actor_id=actor_id,
            verb=VERB_CREATED,
            sprint_id=sprint["id"],
            detail=sprint["name"],
        )
        return sprint


def _edit_summary(before: dict, after: dict) -> str:
    """Name which descriptive fields moved, so the trail says what changed without
    copying the whole row. A rename shows both names; date/goal changes are named
    but not quoted (they are visible on the sprint itself)."""
    parts: list[str] = []
    if before["name"] != after["name"]:
        parts.append(f"{before['name']} → {after['name']}")
    for field in ("goal", "start_date", "end_date"):
        if before[field] != after[field]:
            parts.append(f"{field} changed")
    return ", ".join(parts)


def update_sprint(
    conn: sqlite3.Connection, *, actor_id: int, sprint_id: int, fields: dict
) -> dict:
    """Edit a sprint's descriptive fields atomically with its ``sprint_edited``
    event. ``fields`` carries only the keys the caller set (the data layer's
    partial-update rule). Raises ``SprintCommandError(404)`` if the sprint vanished
    before the write. An edit that changes nothing records no event — a no-op is
    not a lifecycle moment."""
    with db.transaction(conn, immediate=True):
        before = sprints.get_sprint(conn, sprint_id)
        if before is None:
            raise SprintCommandError("not_found", "no such sprint")
        after = sprints.update_sprint(conn, sprint_id, commit=False, **fields)
        if after is None:
            raise SprintCommandError("not_found", "no such sprint")
        summary = _edit_summary(before, after)
        if summary:
            _record(
                conn,
                actor_id=actor_id,
                verb=VERB_EDITED,
                sprint_id=sprint_id,
                detail=summary,
            )
        return after


def start_sprint(conn: sqlite3.Connection, *, actor_id: int, sprint_id: int) -> dict:
    """Move a sprint planned → active atomically with its ``sprint_started`` event.
    The command's ``BEGIN IMMEDIATE`` preserves the data layer's one-active-sprint
    serialization: two concurrent starts still cannot both win, and the loser's
    ``SprintStateError`` propagates with nothing recorded. Raises
    ``SprintCommandError(404)`` if the sprint vanished."""
    with db.transaction(conn, immediate=True):
        started = sprints.start_sprint(conn, sprint_id, commit=False)
        if started is None:
            raise SprintCommandError("not_found", "no such sprint")
        _record(
            conn,
            actor_id=actor_id,
            verb=VERB_STARTED,
            sprint_id=sprint_id,
            detail=started["name"],
        )
        return started


def complete_sprint(conn: sqlite3.Connection, *, actor_id: int, sprint_id: int) -> dict:
    """Move a sprint active → completed atomically with its ``sprint_completed``
    event. A ``SprintStateError`` from an illegal transition propagates with
    nothing recorded. Raises ``SprintCommandError(404)`` if the sprint vanished."""
    with db.transaction(conn, immediate=True):
        completed = sprints.complete_sprint(conn, sprint_id, commit=False)
        if completed is None:
            raise SprintCommandError("not_found", "no such sprint")
        _record(
            conn,
            actor_id=actor_id,
            verb=VERB_COMPLETED,
            sprint_id=sprint_id,
            detail=completed["name"],
        )
        return completed


def delete_sprint(conn: sqlite3.Connection, *, actor_id: int, sprint_id: int) -> bool:
    """Delete a sprint atomically with its ``sprint_deleted`` event. Returns True if
    one was removed, False if it had already vanished (no event for a delete that
    didn't happen). The caller enforces the no-issues precondition (409) and the
    authorization gate. The name is read inside the transaction and preserved in the
    audit detail, since the row is gone by the time the event is read."""
    with db.transaction(conn, immediate=True):
        sprint = sprints.get_sprint(conn, sprint_id)
        if sprint is None:
            return False
        if not sprints.delete_sprint(conn, sprint_id, commit=False):
            return False
        _record(
            conn,
            actor_id=actor_id,
            verb=VERB_DELETED,
            sprint_id=sprint_id,
            detail=sprint["name"],
        )
        return True
