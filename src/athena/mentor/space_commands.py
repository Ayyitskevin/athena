"""Application commands for audited, atomic space-container writes.

The Mentor twin of aegis/project_commands.py. Today this owns the space
create / edit / hard-delete lifecycle: each folds the mutation (mentor.spaces) and
its activity event (mentor.space_activity) into one ``db.transaction`` so the row
change and its audit fact commit or roll back together — previously they ran in
SEPARATE commits, so a crash between them could create a space with no
``space_created`` event, or delete one with no ``space_deleted``.

It also owns the space access lifecycle — the visibility flip (public↔private, which
also inserts the creator as a member when going private) and membership add/remove —
each folding its mutation(s) and event into one transaction.

Transport-shape validation and authorization (key normalization + uniqueness, the
creator-only delete gate, the creator-or-admin access-management gate, the
space-holds-no-pages precondition, and target-user existence) stay at the transport
boundary, as they do for the page commands — the command owns atomicity + audit.
"""

from __future__ import annotations

import sqlite3

from athena.core import access, db
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


def set_space_visibility(
    conn: sqlite3.Connection, *, actor_id: int, space_id: int, visibility: str
) -> dict:
    """Flip a space public↔private atomically with its ``space_made_private`` /
    ``space_made_public`` event. Going private also inserts the creator as an explicit
    member (they keep access via ``created_by`` regardless, but this surfaces them in the
    roster) — folded into the SAME transaction, so the flip, the membership row, and the
    event can't half-land. The boundary validates the value and skips a no-op
    set-to-same, so this is only called on a real transition. Raises
    ``SpaceCommandError(404)`` if the space vanished."""
    with db.transaction(conn, immediate=True):
        before = spaces.get_space(conn, space_id)
        if before is None:
            raise SpaceCommandError("no such space", status_code=404)
        updated = spaces.set_visibility(conn, space_id, visibility, commit=False)
        if updated is None:
            raise SpaceCommandError("no such space", status_code=404)
        if visibility == "private":
            access.add_space_member(
                conn, space_id, before["created_by"], added_by=actor_id, commit=False
            )
        space_activity.record_space_visibility_changed(
            conn,
            actor_id=actor_id,
            space_id=space_id,
            name=updated["name"],
            visibility=visibility,
            commit=False,
        )
        return updated


def add_space_member(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    user_id: int,
    member_name: str,
) -> bool:
    """Grant a user read access to a private space atomically with its
    ``space_member_added`` event. Idempotent: a re-add records nothing and returns
    False. The boundary validates the user exists (422) and passes the member's name for
    the audit detail."""
    with db.transaction(conn, immediate=True):
        added = access.add_space_member(
            conn, space_id, user_id, added_by=actor_id, commit=False
        )
        if added:
            space_activity.record_space_member_added(
                conn, actor_id=actor_id, space_id=space_id, member_name=member_name,
                commit=False,
            )
        return added


def remove_space_member(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    space_id: int,
    user_id: int,
    member_name: str,
) -> bool:
    """Revoke a user's access to a private space atomically with its
    ``space_member_removed`` event. Returns True if a row was removed (the boundary 404s
    a non-member); no event for a removal that didn't happen. The boundary passes the
    member's name for the audit detail (the membership row is gone by the time it reads)."""
    with db.transaction(conn, immediate=True):
        removed = access.remove_space_member(conn, space_id, user_id, commit=False)
        if removed:
            space_activity.record_space_member_removed(
                conn, actor_id=actor_id, space_id=space_id, member_name=member_name,
                commit=False,
            )
        return removed
