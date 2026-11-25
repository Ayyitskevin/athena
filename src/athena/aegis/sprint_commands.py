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

Each command takes the resolved ``actor`` and owns its gate (slice rule 1 of
docs/COMMAND_MIGRATION.md), as the comment commands do: the sprint's project must
be visible to the actor (a hidden one reads as missing) and the actor must be its
creator — managing a project's cadence is the creator's call — checked INSIDE the
write transaction, so the check and the write it guards cannot straddle a
boundary, and a caller reaching a command directly is refused rather than
trusted. The transports still run the same gate first, for their own wire
statuses and ordering, and keep role and token scope (``issue_write_actor``, the
cockpit's read-only refusal), as they do for the comment commands. The
non-empty-sprint delete precondition stays at the transport boundary.

Events are recorded here directly rather than through a separate recorder
module — the sprint verbs carry no derived projections, so the indirection
``project_activity`` needs would buy nothing.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import projects, sprints
from athena.core import access, activity, db

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


def _managed_project_or_refuse(
    conn: sqlite3.Connection, actor: dict, project_id: int
) -> dict:
    """The project, if ``actor`` may manage its sprints — else a refusal.

    ``not_found`` when the project is missing or hidden from the actor, so a
    private project's existence never leaks through a sprint write; ``forbidden``
    when it is visible but the actor is not its creator. The rule is
    ``access.container_write_reason``, the one both transports apply first.
    """
    project = projects.get_project(conn, project_id)
    if project is None:
        raise SprintCommandError("not_found", "no such project")
    reason = access.container_write_reason(
        conn,
        actor,
        kind="project",
        container_id=project_id,
        created_by=project["created_by"],
    )
    if reason == "not_visible":
        raise SprintCommandError("not_found", "no such project")
    if reason == "not_owner":
        raise SprintCommandError(
            "forbidden", "only the project creator may manage its sprints"
        )
    return project


def _managed_sprint_or_refuse(
    conn: sqlite3.Connection, actor: dict, sprint_id: int
) -> dict:
    """The sprint, if it exists and ``actor`` may manage its project's sprints."""
    sprint = sprints.get_sprint(conn, sprint_id)
    if sprint is None:
        raise SprintCommandError("not_found", "no such sprint")
    _managed_project_or_refuse(conn, actor, sprint["project_id"])
    return sprint


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
    actor: dict,
    project_id: int,
    name: str,
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Create a sprint atomically with its ``sprint_created`` event. The detail is
    the sprint's name, so the feed reads "created <name>". Owns its gate: the
    project must exist, be visible to ``actor`` and be theirs, checked inside this
    transaction. A non-empty name stays the transport's guard — that is request
    shape, not a trust boundary."""
    with db.transaction(conn, immediate=True):
        _managed_project_or_refuse(conn, actor, project_id)
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
            actor_id=actor["id"],
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
    conn: sqlite3.Connection, *, actor: dict, sprint_id: int, fields: dict
) -> dict:
    """Edit a sprint's descriptive fields atomically with its ``sprint_edited``
    event. ``fields`` carries only the keys the caller set (the data layer's
    partial-update rule). Owns its gate: ``not_found`` if the sprint vanished or
    its project is hidden from ``actor``, ``forbidden`` for a non-creator. An edit
    that changes nothing records no event — a no-op is not a lifecycle moment."""
    with db.transaction(conn, immediate=True):
        before = _managed_sprint_or_refuse(conn, actor, sprint_id)
        after = sprints.update_sprint(conn, sprint_id, commit=False, **fields)
        if after is None:
            raise SprintCommandError("not_found", "no such sprint")
        summary = _edit_summary(before, after)
        if summary:
            _record(
                conn,
                actor_id=actor["id"],
                verb=VERB_EDITED,
                sprint_id=sprint_id,
                detail=summary,
            )
        return after


def start_sprint(conn: sqlite3.Connection, *, actor: dict, sprint_id: int) -> dict:
    """Move a sprint planned → active atomically with its ``sprint_started`` event.
    The command's ``BEGIN IMMEDIATE`` preserves the data layer's one-active-sprint
    serialization: two concurrent starts still cannot both win, and the loser's
    ``SprintStateError`` propagates with nothing recorded. Owns its gate, checked
    first: ``not_found`` if the sprint vanished or its project is hidden,
    ``forbidden`` for a non-creator."""
    with db.transaction(conn, immediate=True):
        _managed_sprint_or_refuse(conn, actor, sprint_id)
        started = sprints.start_sprint(conn, sprint_id, commit=False)
        if started is None:
            raise SprintCommandError("not_found", "no such sprint")
        _record(
            conn,
            actor_id=actor["id"],
            verb=VERB_STARTED,
            sprint_id=sprint_id,
            detail=started["name"],
        )
        return started


def complete_sprint(conn: sqlite3.Connection, *, actor: dict, sprint_id: int) -> dict:
    """Move a sprint active → completed atomically with its ``sprint_completed``
    event. A ``SprintStateError`` from an illegal transition propagates with
    nothing recorded. Owns its gate, checked first: ``not_found`` if the sprint
    vanished or its project is hidden, ``forbidden`` for a non-creator."""
    with db.transaction(conn, immediate=True):
        _managed_sprint_or_refuse(conn, actor, sprint_id)
        completed = sprints.complete_sprint(conn, sprint_id, commit=False)
        if completed is None:
            raise SprintCommandError("not_found", "no such sprint")
        _record(
            conn,
            actor_id=actor["id"],
            verb=VERB_COMPLETED,
            sprint_id=sprint_id,
            detail=completed["name"],
        )
        return completed


def delete_sprint(conn: sqlite3.Connection, *, actor: dict, sprint_id: int) -> bool:
    """Delete a sprint atomically with its ``sprint_deleted`` event. Returns True if
    one was removed, False if it had already vanished (no event for a delete that
    didn't happen). Owns its gate for a sprint that exists: ``not_found`` if its
    project is hidden from ``actor``, ``forbidden`` for a non-creator. The caller
    enforces the no-issues precondition (409). The name is read inside the
    transaction and preserved in the audit detail, since the row is gone by the
    time the event is read."""
    with db.transaction(conn, immediate=True):
        sprint = sprints.get_sprint(conn, sprint_id)
        if sprint is None:
            return False
        _managed_project_or_refuse(conn, actor, sprint["project_id"])
        if not sprints.delete_sprint(conn, sprint_id, commit=False):
            return False
        _record(
            conn,
            actor_id=actor["id"],
            verb=VERB_DELETED,
            sprint_id=sprint_id,
            detail=sprint["name"],
        )
        return True
