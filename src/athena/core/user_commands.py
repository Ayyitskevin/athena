"""Application commands for audited user privilege changes.

Changing a user's role (a privilege escalation or demotion) or their agent flag are
exactly the kind of administrative actions the append-only log exists to attribute —
yet both were bare UPDATEs with no activity event, so an admin (human or agent)
could promote a user to admin with no trace. These commands own that write: the row
change and its audit event run in one db.transaction, and REST and the browser are
thin callers that translate the single UserCommandError.

Same shape as aegis/issue_commands.py and core/agent_commands.py. Authorization
(admin-only) stays at the transport boundary, as it does for those; the command
owns the last-admin safety, the normalization, the write, and the audit emission.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, users

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_CHANGED_ROLE = "changed_role"
VERB_MARKED_AGENT = "marked_agent"
VERB_UNMARKED_AGENT = "unmarked_agent"


class UserCommandError(Exception):
    """A transport-neutral rejection. ``status_code`` lets each adapter map it (404 for
    an unknown target, 409 for the last-admin guard, 422 for an invalid role)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def set_user_role(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int, role: str
) -> dict:
    """Change target_user_id's role and record a 'changed_role' event atomically.

    Refuses to demote the last admin (mirrors the guard the routes carried) so a
    deploy can't lock itself out. Raises UserCommandError(404) for an unknown target,
    (409) for the last-admin case, (422) for an unknown role. No event is recorded
    when the role is unchanged (a no-op edit is not a lifecycle moment)."""
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise UserCommandError("no such user", status_code=404)
        try:
            normalized = users.normalize_role(role)
        except ValueError as exc:
            raise UserCommandError(str(exc), status_code=422) from exc
        if (
            target["role"] == users.ADMIN_ROLE
            and normalized != users.ADMIN_ROLE
            and users.count_admins(conn) <= 1
        ):
            raise UserCommandError("cannot remove the last admin", status_code=409)
        updated = users.set_role(conn, target_user_id, normalized, commit=False)
        if updated is None:
            raise UserCommandError("no such user", status_code=404)
        if target["role"] != updated["role"]:
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_CHANGED_ROLE,
                target_kind="user",
                target_id=target_user_id,
                detail=f"{target['role']} → {updated['role']}",
                commit=False,
            )
        return updated


def set_user_agent(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int, is_agent: bool
) -> dict:
    """Mark or unmark target_user_id as an agent and record the change atomically.
    Raises UserCommandError(404) for an unknown target. No event when unchanged."""
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise UserCommandError("no such user", status_code=404)
        updated = users.set_agent(conn, target_user_id, is_agent, commit=False)
        if updated is None:
            raise UserCommandError("no such user", status_code=404)
        if target["is_agent"] != updated["is_agent"]:
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_MARKED_AGENT if updated["is_agent"] else VERB_UNMARKED_AGENT,
                target_kind="user",
                target_id=target_user_id,
                commit=False,
            )
        return updated
