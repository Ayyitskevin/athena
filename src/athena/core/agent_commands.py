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
import re
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
VERB_REMOVED = "removed_user"
VERB_RESTORED = "restored_user"

# Synthetic login id when the operator only names the agent. Agents do not
# sign in with a mailbox; this is a unique handle, not a human inbox.
AGENT_EMAIL_DOMAIN = "agents.local"


def agent_email_from_name(name: str) -> str:
    """Turn a display name into the unique email the users table requires."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise AgentCommandError("invalid", "agent name is required")
    return f"{slug}@{AGENT_EMAIL_DOMAIN}"


class AgentCommandError(Exception):
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND`` ("not_found"
    for an unknown target, "conflict" for the last-admin guard) without the
    command having to know about HTTP."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _require_admin_actor(actor: dict | None) -> dict:
    """Resolve the command's administrative authorization boundary.

    Browser sessions and the explicitly trusted actor-header path have no token
    scope cap. Bearer-token actors must carry the admin scope in addition to the
    admin role. Keeping both checks here prevents a new adapter or an in-process
    caller from turning the kill switch into an authorization bypass.
    """
    if actor is None:
        raise AgentCommandError("unauthorized", "authentication required")
    if not identity.is_admin(actor):
        raise AgentCommandError("forbidden", "admin role required")
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise AgentCommandError(
            "forbidden", f"token scope required: {tokens.ADMIN_SCOPE}"
        )
    return actor


def onboard_agent(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    name: str,
    scopes: Iterable[str] | None,
    email: str | None = None,
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

    ``email`` is optional. Agents do not sign in with a mailbox; when omitted,
    Athena stores ``{slug}@agents.local`` as the unique handle. An explicit
    email is still accepted so existing API/MCP callers keep working.

    Scopes are required — an agent's first credential must say what it may do
    (the same no-fail-open rule as every other mint surface). Raises
    AgentCommandError("unauthorized"/"forbidden") for a missing or non-admin
    actor, "conflict" for a duplicate email, "invalid" for a blank name or
    missing/invalid scopes."""
    actor = _require_admin_actor(actor)
    name = name.strip()
    if not name:
        raise AgentCommandError("invalid", "agent name is required")
    email = (email or "").strip() or agent_email_from_name(name)
    try:
        normalized = tokens.normalize_scopes(scopes)
    except ValueError as exc:
        raise AgentCommandError("invalid", str(exc)) from exc
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
            raise AgentCommandError("invalid", exc.detail) from exc
        except sqlite3.IntegrityError as exc:
            raise AgentCommandError("conflict", "email already in use") from exc
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
    0). Raises AgentCommandError("not_found") if the target user does not exist. Reads and the
    write share one immediate transaction so a concurrent mint cannot slip a token in
    between the count and the revoke. Raises AgentCommandError("unauthorized"/
    "forbidden") when the
    actor is missing or lacks the admin role/scope."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("not_found", "no such user")
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

    Refuses to pause the last ACTIVE admin ("conflict"): a paused admin cannot resume
    anyone — itself included — so that pause would brick the workspace. Raises
    AgentCommandError("not_found") for an unknown target, "unauthorized"/
    "forbidden" when the actor is missing or lacks the admin role/scope."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("not_found", "no such user")
        if (
            paused
            and target["role"] == users.ADMIN_ROLE
            and not target.get("paused_at")
            and users.count_active_admins(conn) <= 1
        ):
            raise AgentCommandError("conflict", "cannot pause the last active admin")
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
    lock itself out. Raises AgentCommandError("not_found") for an unknown target,
    "conflict" for the
    last-admin case. Idempotent enough to retry safely: re-running on an already-viewer
    account with no sessions/tokens simply revokes zero of each. Raises
    AgentCommandError("unauthorized"/"forbidden") when the actor is missing or
    lacks the admin role/scope."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("not_found", "no such user")
        if target["role"] == users.ADMIN_ROLE and users.count_admins(conn) <= 1:
            raise AgentCommandError("conflict", "cannot offboard the last admin")
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


def remove_user(
    conn: sqlite3.Connection, *, actor: dict | None, target_user_id: int
) -> dict:
    """The lever after offboarding: offboard (demote to viewer, revoke every
    session and token) AND stamp the removal tombstone in one atomic move, then
    record one audited event. A removed user vanishes from every list, picker,
    and email lookup and can never authenticate; every attributed row — issues,
    activity, forge sources — keeps pointing at the real user, because the
    audit trail is load-bearing and 49 foreign keys reference users. Nothing is
    deleted; ``restore_user`` brings the account back as an offboarded viewer.

    Refuses to remove the last admin (409, mirroring the offboard guard —
    removed admins no longer count). Raises AgentCommandError("not_found") for an
    unknown target, "unauthorized"/"forbidden" when the actor is missing or lacks the admin
    role/scope. Idempotent: re-running on an already-removed account returns
    its current state and records nothing."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("not_found", "no such user")
        if target.get("removed_at"):
            return {
                "user_id": target_user_id,
                "removed_at": target["removed_at"],
                "revoked_session_count": 0,
                "revoked_token_count": 0,
            }
        if target["role"] == users.ADMIN_ROLE and users.count_admins(conn) <= 1:
            raise AgentCommandError("conflict", "cannot remove the last admin")
        users.set_role(conn, target_user_id, users.VIEWER_ROLE, commit=False)
        revoked_sessions = sessions.revoke_all_sessions(
            conn, target_user_id, commit=False
        )
        revoked_tokens = tokens.revoke_all_tokens_for_user(
            conn, user_id=target_user_id, commit=False
        )
        updated = users.set_removed(conn, target_user_id, True, commit=False)
        assert updated is not None
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_REMOVED,
            target_kind="user",
            target_id=target_user_id,
            detail=(
                f"removed: offboarded (revoked {revoked_sessions} session(s), "
                f"{revoked_tokens} token(s)) and hidden everywhere; history "
                "kept attributed"
            ),
            commit=False,
        )
    return {
        "user_id": target_user_id,
        "removed_at": updated["removed_at"],
        "revoked_session_count": revoked_sessions,
        "revoked_token_count": revoked_tokens,
    }


def restore_user(
    conn: sqlite3.Connection, *, actor: dict | None, target_user_id: int
) -> dict:
    """Clear the removal tombstone and nothing else: the account returns as an
    offboarded viewer with no sessions, no tokens, and no role back — every
    further step (role, tokens) is its own audited action. Raises
    AgentCommandError("not_found") for an unknown target, "unauthorized"/
    "forbidden" when the actor is missing or lacks the admin role/scope. Idempotent: restoring a present
    account returns it unchanged and records nothing."""
    actor = _require_admin_actor(actor)
    with db.transaction(conn, immediate=True):
        target = users.get_user(conn, target_user_id)
        if target is None:
            raise AgentCommandError("not_found", "no such user")
        if not target.get("removed_at"):
            return target
        updated = users.set_removed(conn, target_user_id, False, commit=False)
        assert updated is not None
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_RESTORED,
            target_kind="user",
            target_id=target_user_id,
            detail="restored: back as an offboarded viewer with no credentials",
            commit=False,
        )
    return updated
