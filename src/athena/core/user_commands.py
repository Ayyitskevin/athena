"""Application commands for audited user privilege changes.

Changing a user's role (a privilege escalation or demotion) or their agent flag are
exactly the kind of administrative actions the append-only log exists to attribute —
yet both were bare UPDATEs with no activity event, so an admin (human or agent)
could promote a user to admin with no trace. These commands own that write: the row
change and its audit event run in one db.transaction, and REST and the browser are
thin callers that translate the single UserCommandError.

Same shape as aegis/issue_commands.py and core/agent_commands.py. The
unauthenticated bootstrap eligibility check and password-reset authorization live
inside this command boundary. The admin-only gate for ordinary user creation and
privilege edits remains at the transport boundary; the command owns the
last-admin safety, normalization, write, and audit emission.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from athena.core import activity, db, identity, passwords, sessions, tokens, users

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_CREATED_USER = "created_user"
VERB_CHANGED_ROLE = "changed_role"
VERB_MARKED_AGENT = "marked_agent"
VERB_UNMARKED_AGENT = "unmarked_agent"
VERB_PASSWORD_CHANGED = "password_changed"
VERB_PASSWORD_RESET = "password_reset"


ErrorKind = Literal[
    "unauthorized",
    "forbidden",
    "bad_request",
    "not_found",
    "conflict",
    "invalid",
]


class UserCommandError(Exception):
    """A transport-neutral rejection.

    Carries a closed ``kind`` vocabulary, never an HTTP status. REST and browser
    adapters map the same refusal to their own transport-specific response.
    """

    def __init__(self, kind: ErrorKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _require_admin_actor(actor: dict | None) -> dict:
    """Resolve the administrative authorization boundary for a credential write.

    Mirrors ``agent_commands._require_admin_actor``: browser sessions and the
    explicitly trusted actor-header path carry no token scope cap, while a bearer
    actor must hold the admin scope in addition to the admin role. Keeping both
    checks inside the command means a new adapter cannot turn a password reset —
    which hands over an account — into an authorization bypass.
    """
    if actor is None:
        raise UserCommandError("unauthorized", "authentication required")
    if not identity.is_admin(actor):
        raise UserCommandError("forbidden", "admin role required")
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise UserCommandError(
            "forbidden", f"token scope required: {tokens.ADMIN_SCOPE}"
        )
    return actor


def _create_detail(user: dict) -> str:
    agent = ", agent" if user.get("is_agent") else ""
    return f"{user['email']} ({user['role']}{agent})"


def _create_user_in_transaction(
    conn: sqlite3.Connection,
    *,
    actor_id: int | None,
    email: str,
    name: str,
    password: str | None,
    role: str | None,
    is_agent: bool,
) -> dict:
    """Insert and audit one user while the caller owns the write transaction."""
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
    a user, or None for a SELF-provisioning path such as an SSO first-login — None
    records the event against the new user itself, since there is no other actor to
    name. Unauthenticated administrator bootstrap must use ``bootstrap_user`` so its
    eligibility and forced role cannot be bypassed. The password hash is never in the
    detail (it records email, role, and the agent flag only). Raises
    sqlite3.IntegrityError for a duplicate email and ValueError for an invalid role,
    unchanged from the bare call, so the transports keep translating them exactly as
    they do today."""
    with db.transaction(conn, immediate=True):
        return _create_user_in_transaction(
            conn,
            actor_id=actor_id,
            email=email,
            name=name,
            password=password,
            role=role,
            is_agent=is_agent,
        )


def bootstrap_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    name: str,
    password: str | None = None,
    is_agent: bool = False,
) -> dict:
    """Create the one unauthenticated bootstrap administrator atomically.

    The public adapter calls this on a fresh connection after observing an empty
    database. Eligibility is then checked after ``BEGIN IMMEDIATE`` acquires
    SQLite's write lock. Two bootstrap callers may both reach this command, but
    only the first can create an administrator. Fresh-account SSO provisioning is
    separately refused until this administrator exists.
    """
    if conn.in_transaction:
        raise RuntimeError("bootstrap_user requires a top-level transaction")
    with db.transaction(conn, immediate=True):
        if users.count_admins(conn) != 0:
            raise UserCommandError("unauthorized", "authentication required")
        return _create_user_in_transaction(
            conn,
            actor_id=None,
            email=email,
            name=name,
            password=password,
            role=users.BOOTSTRAP_ROLE,
            is_agent=is_agent,
        )


def set_user_role(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int, role: str
) -> dict:
    """Change target_user_id's role and record a 'changed_role' event atomically.

    Refuses to demote the last admin (mirrors the guard the routes carried) so a
    deploy can't lock itself out. Raises ``not_found`` for an unknown target,
    ``conflict`` for the last-admin case, and ``invalid`` for an unknown role. No
    event is recorded when the role is unchanged (a no-op edit is not a lifecycle
    moment)."""
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise UserCommandError("not_found", "no such user")
        try:
            normalized = users.normalize_role(role)
        except ValueError as exc:
            raise UserCommandError("invalid", str(exc)) from exc
        if (
            target["role"] == users.ADMIN_ROLE
            and normalized != users.ADMIN_ROLE
            and users.count_admins(conn) <= 1
        ):
            raise UserCommandError("conflict", "cannot remove the last admin")
        updated = users.set_role(conn, target_user_id, normalized, commit=False)
        if updated is None:
            raise UserCommandError("not_found", "no such user")
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

    Raises ``unauthorized`` when unauthenticated, ``invalid`` for a blank current
    or new password, ``bad_request`` when the current password does not match, and
    ``not_found`` if the account vanished before the write.
    """
    if actor is None:
        raise UserCommandError("unauthorized", "authentication required")
    new_password = new_password.strip()
    if not current_password.strip():
        raise UserCommandError("invalid", "current password is required")
    if not new_password:
        raise UserCommandError("invalid", "new password is required")
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, actor["id"])
        if target is None:
            raise UserCommandError("not_found", "no such user")
        # Verify against the stored hash directly instead of users.verify_credentials:
        # that helper transparently re-hashes at a newer cost AND COMMITS, which would
        # end this transaction mid-command — and the upgrade is pointless when the very
        # next statement replaces the hash. An account with no password (SSO-only) has
        # nothing to prove ownership with and is refused, exactly as before.
        if not passwords.verify_password(current_password, target["password_hash"]):
            raise UserCommandError("bad_request", "current password is incorrect")
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
    ever recorded. Raises ``unauthorized``/``forbidden`` for authorization,
    ``invalid`` for a blank password, and ``not_found`` for an unknown target.
    """
    actor = _require_admin_actor(actor)
    password = password.strip()
    if not password:
        raise UserCommandError("invalid", "password is required")
    with db.transaction(conn, immediate=True):
        if users.get_user(conn, target_user_id) is None:
            raise UserCommandError("not_found", "no such user")
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
    Raises ``not_found`` for an unknown target. No event when unchanged."""
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise UserCommandError("not_found", "no such user")
        updated = users.set_agent(conn, target_user_id, is_agent, commit=False)
        if updated is None:
            raise UserCommandError("not_found", "no such user")
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
