"""Application commands for operator control over an agent's credentials.

The scoped-token thesis implies a kill switch: when an agent is compromised or
running away, the operator must be able to STOP it, not merely watch it. Today the
admin cockpit can display an agent's live tokens but offers no lever to disable
them, and the only revoke path is owner-scoped (you can revoke your own tokens,
not another user's). These commands close that gap.

They are the *authoritative* levers over an agent's credential lifecycle:

- ``onboard_agent`` — the front door: create the agent user and mint its first
  scoped token, one atomic audited move.
- ``set_user_paused`` — the pause: freeze every authenticated action without
  destroying anything, and restore exactly on resume.
- ``revoke_agent_tokens`` — the kill switch: revoke every live token a user holds.
- ``offboard_user`` — the full lockout: demote to viewer, revoke every session,
  revoke every token, in one move.

Each runs as one atomic, audited transaction (the cardinal "one command owns each
write" rule): the credential change and the append-only activity event land or
roll back together, so a lockout can never persist without its record. Web, REST,
and MCP are thin callers that translate the single ``AgentCommandError`` into their
own error shape — the authorization guard and the last-admin safety live here, in
one place, not copied across three transports.

Adapters may reject non-admin requests early, but these commands repeat the role and
token-scope checks. A direct or future transport call therefore cannot bypass the
same authorization policy that protects the REST and browser routes.
"""

from __future__ import annotations

from collections.abc import Iterable
import sqlite3

from athena.core import (
    activity,
    db,
    identity,
    sessions,
    token_commands,
    tokens,
    user_commands,
    users,
)

# Free-form activity verbs. activity.verb is plain TEXT with no enum (see
# migrations/0017_activity.sql), so a new verb is just a constant — no migration.
VERB_REVOKE_TOKENS = "revoked_agent_tokens"
VERB_OFFBOARD = "offboarded_user"
VERB_ONBOARD = "onboarded_agent"
VERB_PAUSED = "paused_user"
VERB_RESUMED = "resumed_user"


class AgentCommandError(Exception):
    """A transport-neutral rejection. ``status_code`` lets each adapter map it (404
    for an unknown target, 409 for the last-admin guard) without the command having
    to know about HTTP."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_admin_actor(actor: dict | None) -> dict:
    """Resolve the command's administrative authorization boundary.

    Browser sessions and the explicitly trusted actor-header path have no token
    scope cap. Bearer-token actors must carry the admin scope in addition to the
    admin role. Keeping both checks here prevents a new adapter or an in-process
    caller from turning the kill switch into an authorization bypass.
    """
    if actor is None:
        raise AgentCommandError("authentication required", status_code=401)
    if not identity.is_admin(actor):
        raise AgentCommandError("admin role required", status_code=403)
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise AgentCommandError(
            f"token scope required: {tokens.ADMIN_SCOPE}", status_code=403
        )
    return actor


def onboard_agent(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    email: str,
    name: str,
    scopes: Iterable[str] | None,
    token_name: str | None = None,
) -> dict:
    """Provision a working agent in ONE atomic move: create the agent user
    (member role, no password — it acts through tokens only) and mint its first
    scoped token, attributing the whole moment to the acting admin.

    Without this, onboarding is three manual steps (create user, mark agent,
    mint token) and the mint step must impersonate the new agent — which records
    a 'minted_token' event claiming the agent minted its own credential before it
    ever ran. Here the trail is truthful: 'created_user' and 'minted_token' keep
    their enumeration invariants (every user, every token, has its event) but
    both name the ADMIN as actor, and one 'onboarded_agent' event summarizes the
    moment with the credential's power spelled out. The raw secret rides back in
    the returned dict only; it is never written to the log.

    Scopes are required — an agent's first credential must say what it may do
    (the same no-fail-open rule as every other mint surface). Raises
    AgentCommandError(401/403) for a missing or non-admin actor, (409) for a
    duplicate email, (422) for blank email/name or missing/invalid scopes."""
    actor = _require_admin_actor(actor)
    email = email.strip()
    name = name.strip()
    if not email or not name:
        raise AgentCommandError("agent email and name are required", status_code=422)
    try:
        normalized = tokens.normalize_scopes(scopes)
    except ValueError as exc:
        raise AgentCommandError(str(exc), status_code=422) from exc
    with db.transaction(conn, immediate=True):
        try:
            agent = user_commands.create_user(
                conn,
                actor_id=actor["id"],
                email=email,
                name=name,
                role=users.DEFAULT_ROLE,
                is_agent=True,
            )
        except user_commands.UserCommandError as exc:
            raise AgentCommandError(exc.detail, status_code=422) from exc
        except sqlite3.IntegrityError as exc:
            raise AgentCommandError("email already in use", status_code=409) from exc
        # Minted inline rather than via token_commands.mint_token: that command
        # attributes the event to the token's owner (self-service), which here
        # would falsely record the agent minting its own credential.
        token = tokens.create_token(
            conn,
            user_id=agent["id"],
            name=token_name or name,
            scopes=normalized,
            commit=False,
        )
        scope_list = " ".join(token["scopes"])
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=token_commands.VERB_MINTED_TOKEN,
            target_kind="token",
            target_id=token["id"],
            detail=f"{token['name']} [{scope_list}] (onboarding {agent['email']})",
            commit=False,
        )
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_ONBOARD,
            target_kind="user",
            target_id=agent["id"],
            detail=f"{agent['email']} with token '{token['name']}' [{scope_list}]",
            commit=False,
        )
    return {"user": agent, "token": token}


def revoke_agent_tokens(
    conn: sqlite3.Connection, *, actor: dict | None, target_user_id: int
) -> dict:
    """Revoke EVERY live API token held by ``target_user_id`` — the credential kill
    switch — and record one audited event attributed to the acting admin.

    Idempotent: a second call revokes nothing and records that (revoked_token_count
    0). Raises AgentCommandError(404) if the target user does not exist. Reads and the
    write share one immediate transaction so a concurrent mint cannot slip a token in
    between the count and the revoke. Raises AgentCommandError(401/403) when the
    actor is missing or lacks the admin role/scope."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("no such user", status_code=404)
        revoked = tokens.revoke_all_tokens_for_user(
            conn, user_id=target_user_id, commit=False
        )
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_REVOKE_TOKENS,
            target_kind="user",
            target_id=target_user_id,
            detail=f"revoked {revoked} live token(s)",
            commit=False,
        )
    return {"user_id": target_user_id, "revoked_token_count": revoked}


def set_user_paused(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    target_user_id: int,
    paused: bool,
) -> dict:
    """Pause or resume a user — the operator lever BETWEEN watch and revoke.

    Pausing freezes the account at identity resolution (every authenticated
    action refused, browser sessions treated as signed out) without destroying
    anything: no tokens revoked, no sessions burned, no role change. Resume
    restores it exactly. The flip and its 'paused_user'/'resumed_user' event
    land in one transaction; a no-op flip (already in the requested state)
    records nothing — repeated pushes of the same lever are not lifecycle
    moments.

    Refuses to pause the last ACTIVE admin (409): a paused admin cannot resume
    anyone — itself included — so that pause would brick the workspace. Raises
    AgentCommandError(404) for an unknown target, (401/403) when the actor is
    missing or lacks the admin role/scope."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("no such user", status_code=404)
        if (
            paused
            and target["role"] == users.ADMIN_ROLE
            and not target.get("paused_at")
            and users.count_active_admins(conn) <= 1
        ):
            raise AgentCommandError(
                "cannot pause the last active admin", status_code=409
            )
        already = bool(target.get("paused_at"))
        if already == paused:
            return target
        updated = users.set_paused(conn, target_user_id, paused, commit=False)
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_PAUSED if paused else VERB_RESUMED,
            target_kind="user",
            target_id=target_user_id,
            commit=False,
        )
        assert updated is not None
    return updated


def offboard_user(
    conn: sqlite3.Connection, *, actor: dict | None, target_user_id: int
) -> dict:
    """Lock a user out in one atomic move: demote to viewer, revoke every session,
    revoke every token — the full offboarding lever — and record one audited event.

    Refuses to strip the last admin (mirrors the role-change guard) so a deploy can't
    lock itself out. Raises AgentCommandError(404) for an unknown target, (409) for the
    last-admin case. Idempotent enough to retry safely: re-running on an already-viewer
    account with no sessions/tokens simply revokes zero of each. Raises
    AgentCommandError(401/403) when the actor is missing or lacks the admin
    role/scope."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("no such user", status_code=404)
        if target["role"] == users.ADMIN_ROLE and users.count_admins(conn) <= 1:
            raise AgentCommandError("cannot offboard the last admin", status_code=409)
        users.set_role(conn, target_user_id, users.VIEWER_ROLE, commit=False)
        revoked_sessions = sessions.revoke_all_sessions(
            conn, target_user_id, commit=False
        )
        revoked_tokens = tokens.revoke_all_tokens_for_user(
            conn, user_id=target_user_id, commit=False
        )
        activity.record(
            conn,
            actor_id=actor["id"],
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
