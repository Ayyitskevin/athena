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

from athena.core import activity, db, identity, passwords, sessions, tokens, users

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_CREATED_USER = "created_user"
VERB_CHANGED_ROLE = "changed_role"
VERB_MARKED_AGENT = "marked_agent"
VERB_UNMARKED_AGENT = "unmarked_agent"
VERB_PASSWORD_CHANGED = "password_changed"
VERB_PASSWORD_RESET = "password_reset"


class UserCommandError(Exception):
    """A transport-neutral rejection. ``status_code`` lets each adapter map it (404 for
    an unknown target, 409 for the last-admin guard, 422 for an invalid role)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_admin_actor(actor: dict | None) -> dict:
    """Resolve the administrative authorization boundary for a credential write.

    Mirrors ``agent_commands._require_admin_actor``: browser sessions and the
    explicitly trusted actor-header path carry no token scope cap, while a bearer
    actor must hold the admin scope in addition to the admin role. Keeping both
    checks inside the command means a new adapter cannot turn a password reset —
    which hands over an account — into an authorization bypass.
    """
    if actor is None:
        raise UserCommandError("authentication required", status_code=401)
    if not identity.is_admin(actor):
        raise UserCommandError("admin role required", status_code=403)
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise UserCommandError(
            f"token scope required: {tokens.ADMIN_SCOPE}", status_code=403
        )
    return actor


def _create_detail(user: dict) -> str:
    agent = ", agent" if user.get("is_agent") else ""
    return f"{user['email']} ({user['role']}{agent})"


def create_user(
    conn: sqlite3.Connection,
    *,
    actor_id: int | None,
    email: str,
    name: str,
    password: str | None = None,
    role: str | None = None,
    is_agent: bool = False,
) -> dict:
    """Create a user and record a 'created_user' event atomically — a new actor
    entering the system is exactly the kind of privilege moment the append-only log
    exists to attribute.

    ``actor_id`` attributes the creation: pass the acting admin's id when an admin adds
    a user, or None for a SELF-provisioning path (the unauthenticated bootstrap of the
    first user, or an SSO first-login) — None records the event against the new user
    itself, since there is no other actor to name. The password hash is never in the
    detail (it records email, role, and the agent flag only). Raises sqlite3.IntegrityError
    for a duplicate email and ValueError for an invalid role, unchanged from the bare
    call, so the transports keep translating them exactly as they do today."""
    with db.transaction(conn, immediate=True):
        user = users.create_user(
            conn,
            email=email,
            name=name,
            password=password,
            role=role,
            is_agent=is_agent,
            commit=False,
        )
        activity.record(
            conn,
            actor_id=actor_id if actor_id is not None else user["id"],
            verb=VERB_CREATED_USER,
            target_kind="user",
            target_id=user["id"],
            detail=_create_detail(user),
            commit=False,
        )
        return user


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


def change_own_password(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    current_password: str,
    new_password: str,
    keep_session_raw: str | None = None,
) -> dict:
    """Rotate the CALLER's own password, revoke the sessions that rotation
    invalidates, and record a 'password_changed' event — all in one transaction.

    Ownership is proven by ``current_password``, verified here under the write lock
    rather than at the transport, so the check and the write cannot straddle a
    concurrent rotation. Every OTHER session of the account is revoked so a device
    signed in on the old password cannot keep riding a live cookie for up to
    SESSION_TTL_DAYS; the caller's own session (``keep_session_raw``) survives, so
    they stay signed in here. A missing cookie revokes everything — the safe
    direction.

    Previously the hash write and the session revocation were two independent
    commits with no audit event at all, so a crash between them left a rotated
    password with live sessions and nothing on the append-only trail. Neither the
    password nor its hash is ever recorded.

    Raises UserCommandError(401) when unauthenticated, (422) for a blank current or
    new password, (400) when the current password does not match, and (404) if the
    account vanished before the write.
    """
    if actor is None:
        raise UserCommandError("authentication required", status_code=401)
    new_password = new_password.strip()
    if not current_password.strip():
        raise UserCommandError("current password is required", status_code=422)
    if not new_password:
        raise UserCommandError("new password is required", status_code=422)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, actor["id"])
        if target is None:
            raise UserCommandError("no such user", status_code=404)
        # Verify against the stored hash directly instead of users.verify_credentials:
        # that helper transparently re-hashes at a newer cost AND COMMITS, which would
        # end this transaction mid-command — and the upgrade is pointless when the very
        # next statement replaces the hash. An account with no password (SSO-only) has
        # nothing to prove ownership with and is refused, exactly as before.
        if not passwords.verify_password(current_password, target["password_hash"]):
            raise UserCommandError("current password is incorrect", status_code=400)
        updated = users.set_password(conn, target["id"], new_password, commit=False)
        # Never None: the row was read under this same write lock.
        assert updated is not None
        revoked = sessions.revoke_other_sessions(
            conn, target["id"], keep_session_raw, commit=False
        )
        activity.record(
            conn,
            actor_id=target["id"],
            verb=VERB_PASSWORD_CHANGED,
            target_kind="user",
            target_id=target["id"],
            detail=f"{revoked} other session(s) revoked",
            commit=False,
        )
        return updated


def reset_user_password(
    conn: sqlite3.Connection, *, actor: dict | None, target_user_id: int, password: str
) -> dict:
    """Reset a user's password as an admin, revoke EVERY session of that account, and
    record a 'password_reset' event — all in one transaction.

    This is a privilege operation, not a preference: afterwards the admin can sign in
    as the target, and every later write is attributed to the target's account. It was
    the one admin lever with no audit trail at all (role change, agent flag, pause,
    token kill switch, and offboard are all audited commands), so the log could not
    answer who reset a password or when. Unlike the self-service change, no session of
    the target is kept — resetting a compromised or departing user's credential must
    actually end their access now rather than after SESSION_TTL_DAYS.

    Authorization is enforced here (admin role, plus the admin scope for a bearer
    token) so no adapter can reach it with less. Neither the password nor its hash is
    ever recorded. Raises UserCommandError(401/403) for authorization, (422) for a
    blank password, and (404) for an unknown target.
    """
    actor = _require_admin_actor(actor)
    password = password.strip()
    if not password:
        raise UserCommandError("password is required", status_code=422)
    with db.transaction(conn, immediate=True):
        if users.get_user(conn, target_user_id) is None:
            raise UserCommandError("no such user", status_code=404)
        updated = users.set_password(conn, target_user_id, password, commit=False)
        # Never None: the row was read under this same write lock.
        assert updated is not None
        revoked = sessions.revoke_all_sessions(conn, target_user_id, commit=False)
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_PASSWORD_RESET,
            target_kind="user",
            target_id=target_user_id,
            detail=f"{revoked} session(s) revoked",
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
