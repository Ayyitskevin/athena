"""Application commands for audited API-token lifecycle changes.

Minting a token hands a caller a live credential; revoking one takes it away. Both
were bare writes with NO activity event — a user (or an admin-scoped agent acting as
one) could mint itself a fresh admin credential, or quietly revoke one, with zero
trace in the append-only log. These commands own that write: the credential change
and its audit event run in one db.transaction, so a token can never appear or vanish
without its record.

Same shape as core/agent_commands.py and core/user_commands.py. Tokens are always
self-service — you manage your OWN tokens — so the acting user IS the owner; the
command trusts the resolved ``actor_id`` for both attribution and ownership. The raw
secret returned by a mint is NEVER written to the log; the audit detail records only
the token's name and scopes.
"""

from __future__ import annotations

from collections.abc import Iterable
import sqlite3

from athena.core import activity, db, tokens

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_MINTED_TOKEN = "minted_token"
VERB_REVOKED_TOKEN = "revoked_token"


class TokenCommandError(Exception):
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND``, so the
    command layer states what went wrong and the transport decides how to say
    it — the same shape every command module written since uses."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def mint_token(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    name: str,
    scopes: Iterable[str] | None = None,
) -> dict:
    """Mint a token for the acting user and record a 'minted_token' event atomically.

    The one-time raw secret rides back in the returned dict but is NEVER written to
    the audit log — the detail records the token's name and its granted scopes only,
    so the trail shows a credential was created and how powerful it is, not what it is.
    Raises TokenCommandError(422) for an invalid scope."""
    with db.transaction(conn, immediate=True):
        try:
            created = tokens.create_token(
                conn, user_id=actor_id, name=name, scopes=scopes, commit=False
            )
        except ValueError as exc:
            raise TokenCommandError("invalid", str(exc)) from exc
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_MINTED_TOKEN,
            target_kind="token",
            target_id=created["id"],
            detail=f"{created['name']} [{' '.join(created['scopes'])}]",
            commit=False,
        )
        return created


def revoke_token(conn: sqlite3.Connection, *, actor_id: int, token_id: int) -> bool:
    """Revoke a token the acting user owns and record a 'revoked_token' event
    atomically. Returns True if a live token was revoked, False otherwise (unknown id,
    another owner's token, or already revoked) so the caller can 404 honestly. No event
    is recorded when nothing was revoked — a missed revoke is not a lifecycle moment."""
    with db.transaction(conn, immediate=True):
        revoked = tokens.revoke_token(
            conn, user_id=actor_id, token_id=token_id, commit=False
        )
        if revoked:
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_REVOKED_TOKEN,
                target_kind="token",
                target_id=token_id,
                commit=False,
            )
        return revoked
