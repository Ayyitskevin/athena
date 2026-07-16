"""Application commands for operator control over an agent's credentials.

The scoped-token thesis implies a kill switch: when an agent is compromised or
running away, the operator must be able to STOP it, not merely watch it. Today the
admin cockpit can display an agent's live tokens but offers no lever to disable
them, and the only revoke path is owner-scoped (you can revoke your own tokens,
not another user's). These commands close that gap.

They are the *authoritative* levers:

- ``revoke_agent_tokens`` — the kill switch: revoke every live token a user holds.
- ``offboard_user`` — the full lockout: demote to viewer, revoke every session,
  revoke every token, in one move.

Each runs as one atomic, audited transaction (the cardinal "one command owns each
write" rule): the credential change and the append-only activity event land or
roll back together, so a lockout can never persist without its record. Web, REST,
and MCP are thin callers that translate the single ``AgentCommandError`` into their
own error shape — the authorization guard and the last-admin safety live here, in
one place, not copied across three transports.

Callers MUST gate these on admin themselves (they reach another user's credentials);
the command trusts the resolved ``actor_id`` it is given for attribution.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, sessions, tokens, users

# Free-form activity verbs. activity.verb is plain TEXT with no enum (see
# migrations/0017_activity.sql), so a new verb is just a constant — no migration.
VERB_REVOKE_TOKENS = "revoked_agent_tokens"
VERB_OFFBOARD = "offboarded_user"


class AgentCommandError(Exception):
    """A transport-neutral rejection. ``status_code`` lets each adapter map it (404
    for an unknown target, 409 for the last-admin guard) without the command having
    to know about HTTP."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def revoke_agent_tokens(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int
) -> dict:
    """Revoke EVERY live API token held by ``target_user_id`` — the credential kill
    switch — and record one audited event attributed to the acting admin.

    Idempotent: a second call revokes nothing and records that (revoked_token_count
    0). Raises AgentCommandError(404) if the target user does not exist. Reads and the
    write share one immediate transaction so a concurrent mint cannot slip a token in
    between the count and the revoke."""
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("no such user", status_code=404)
        revoked = tokens.revoke_all_tokens_for_user(
            conn, user_id=target_user_id, commit=False
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_REVOKE_TOKENS,
            target_kind="user",
            target_id=target_user_id,
            detail=f"revoked {revoked} live token(s)",
            commit=False,
        )
    return {"user_id": target_user_id, "revoked_token_count": revoked}


def offboard_user(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int
) -> dict:
    """Lock a user out in one atomic move: demote to viewer, revoke every session,
    revoke every token — the full offboarding lever — and record one audited event.

    Refuses to strip the last admin (mirrors the role-change guard) so a deploy can't
    lock itself out. Raises AgentCommandError(404) for an unknown target, (409) for the
    last-admin case. Idempotent enough to retry safely: re-running on an already-viewer
    account with no sessions/tokens simply revokes zero of each."""
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("no such user", status_code=404)
        if target["role"] == users.ADMIN_ROLE and users.count_admins(conn) <= 1:
            raise AgentCommandError(
                "cannot offboard the last admin", status_code=409
            )
        users.set_role(conn, target_user_id, users.VIEWER_ROLE, commit=False)
        revoked_sessions = sessions.revoke_all_sessions(
            conn, target_user_id, commit=False
        )
        revoked_tokens = tokens.revoke_all_tokens_for_user(
            conn, user_id=target_user_id, commit=False
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_OFFBOARD,
            target_kind="user",
            target_id=target_user_id,
            detail=(
                f"offboarded: demoted to viewer, revoked {revoked_sessions} "
                f"session(s) and {revoked_tokens} token(s)"
            ),
            commit=False,
        )
    return {
        "user_id": target_user_id,
        "role": users.VIEWER_ROLE,
        "revoked_session_count": revoked_sessions,
        "revoked_token_count": revoked_tokens,
    }
