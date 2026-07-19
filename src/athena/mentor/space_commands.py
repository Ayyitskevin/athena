"""Application commands for audited, atomic space-container writes.

The Mentor twin of aegis/project_commands.py. Today this owns the space
create / edit / hard-delete lifecycle: each folds the mutation (mentor.spaces) and
its activity event (mentor.space_activity) into one ``db.transaction`` so the row
change and its audit fact commit or roll back together — previously they ran in
SEPARATE commits, so a crash between them could create a space with no
``space_created`` event, or delete one with no ``space_deleted``.

Transport-shape validation and authorization (key normalization + uniqueness, the
creator-only delete gate, the space-holds-no-pages precondition) stay at the transport
boundary, as they do for the page commands — the command owns atomicity + audit.
Space visibility and membership are separate commands (a later slice).
"""

from __future__ import annotations

import sqlite3

from athena.core import db
from athena.mentor import space_activity, spaces


class SpaceCommandError(Exception):
    """A transport-neutral rejection. ``status_code`` lets each adapter map it (404 for
    a space that vanished between the boundary's checks and the write)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def create_space(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    key: str,
    name: str,
    description: str = "",
) -> dict:
    """Create a space atomically with its ``space_created`` event. The boundary
    normalizes/validates the key + name and checks key uniqueness first (a clean 409);
    the UNIQUE index is the backstop. Returns the new space."""
    with db.transaction(conn, immediate=True):
        space = spaces.create_space(
            conn,
            key=key,
            name=name,
            description=description,
            created_by=actor_id,
            commit=False,
        )
        space_activity.record_space_created(
            conn, actor_id=actor_id, space_id=space["id"], name=space["name"], commit=False
        )
        return space


def edit_space(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    key: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Edit a space's key/name/description atomically with its ``space_edited`` event
    (a no-op change records nothing). Only the fields passed as non-None are written.
    Raises ``SpaceCommandError(404)`` if the space vanished. The boundary validates the
    fields and checks a key change against a DIFFERENT space (409) before calling."""
    with db.transaction(conn, immediate=True):
        before = spaces.get_space(conn, space_id)
        if before is None:
            raise SpaceCommandError("no such space", status_code=404)
        after = spaces.update_space(
            conn, space_id, key=key, name=name, description=description, commit=False
        )
        if after is None:
            raise SpaceCommandError("no such space", status_code=404)
        space_activity.record_space_edited(
            conn, actor_id=actor_id, before=before, after=after, commit=False
        )
        return after


def delete_space(
    conn: sqlite3.Connection, *, actor_id: int, space_id: int, name: str
) -> bool:
    """Hard-delete a space atomically with its ``space_deleted`` event. Returns True if
    a space was removed, False if it had already vanished (a race — no event). The
    caller enforces the creator-only gate and the space-holds-no-pages precondition
    (409), and passes the space's ``name`` (the row is gone by the time the event is
    read, so the name is preserved in the audit detail)."""
    with db.transaction(conn, immediate=True):
        removed = spaces.delete_space(conn, space_id, commit=False)
        if removed:
            space_activity.record_space_deleted(
                conn, actor_id=actor_id, space_id=space_id, name=name, commit=False
            )
        return removed
