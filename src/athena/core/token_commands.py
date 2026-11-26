"""Application commands for audited API-token lifecycle changes.

Minting a token hands a caller a live credential; revoking one takes it away. Both
were bare writes with NO activity event — a user (or an admin-scoped agent acting as
one) could mint itself a fresh admin credential, or quietly revoke one, with zero
trace in the append-only log. These commands own that write: the credential change
and its audit event run in one db.transaction, so a token can never appear or vanish
without its record.

Tokens are always self-service — you manage your OWN tokens — so the acting user IS
the owner. Both commands take the resolved actor, not a bare id, and re-read the
account inside their own transaction: one that vanished or was removed between the
caller's lookup and the write mints and revokes nothing, and a paused one mints
nothing (revoking stays open to it: taking a credential away is always safe). The
host command, the demo and the smoke script pass the account row they already
resolved, so each acts as the account it records. The raw secret returned by a mint
is NEVER written to the log; the audit detail records only the token's name and
scopes.
"""

from __future__ import annotations

from collections.abc import Iterable
import sqlite3

from athena.core import activity, db, tokens, users

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


def _live_owner(conn: sqlite3.Connection, actor: dict, *, minting: bool) -> int:
    """The acting account's id, re-read under the command's lock, or the
    command's refusal: a vanished or removed account owns nothing, and a paused
    one may not mint a fresh credential."""
    live = users.get_user(conn, int(actor["id"]))
    if live is None or live.get("removed_at"):
        raise TokenCommandError("forbidden", "account is no longer available")
    if minting and live.get("paused_at"):
        raise TokenCommandError("forbidden", "account is paused")
    return int(live["id"])


def mint_token(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    name: str,
    scopes: Iterable[str] | None = None,
    via: str | None = None,
) -> dict:
    """Mint a token for the acting user and record a 'minted_token' event atomically.

    ``via`` says how it was minted when the account did not ask through its own
    session: the host command names itself, so the trail does not read as
    though the account minted a credential it was in fact handed.

    The one-time raw secret rides back in the returned dict but is NEVER written to
    the audit log — the detail records the token's name and its granted scopes only,
    so the trail shows a credential was created and how powerful it is, not what it is.
    Raises TokenCommandError ``invalid`` for an invalid scope and ``forbidden``
    for an account that is gone, removed or paused."""
    with db.transaction(conn, immediate=True):
        actor_id = _live_owner(conn, actor, minting=True)
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
            detail=f"{created['name']} [{' '.join(created['scopes'])}]"
            + ("" if via is None else f"; via {via}"),
            commit=False,
        )
        return created


def revoke_token(conn: sqlite3.Connection, *, actor: dict, token_id: int) -> bool:
    """Revoke a token the acting user owns and record a 'revoked_token' event
    atomically. Returns True if a live token was revoked, False otherwise (unknown id,
    another owner's token, or already revoked) so the caller can 404 honestly. No event
    is recorded when nothing was revoked — a missed revoke is not a lifecycle moment.
    Raises TokenCommandError ``forbidden`` for an account that is gone or removed."""
    with db.transaction(conn, immediate=True):
        actor_id = _live_owner(conn, actor, minting=False)
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
