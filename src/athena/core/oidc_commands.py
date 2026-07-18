"""Application commands for audited SSO identity-link lifecycle changes.

Binding an external OpenID Connect identity (an issuer + subject the IdP asserts) to
an Athena account grants a new way to log in as that user; unlinking one removes it.
Both were bare writes with NO activity event — an SSO identity could be attached to
(or detached from) an account with zero trace in the append-only log, even though a
new login credential for an account is exactly what the audit trail exists to record.
These commands own that write: the link/unlink and its audit event run in one
db.transaction, so an account's set of sign-in methods never changes silently.

Both operations are self-service — a user links their own identity on first SSO login
and unlinks it from their own settings — so the acting user IS the subject; the command
takes the resolved ``actor_id`` for attribution and records the event against that same
user (target_kind "user"). The IdP subject is an opaque account identifier, not a
secret, and is already shown on the settings page, so the detail records issuer+subject.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, oidc

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_LINKED = "linked_identity"
VERB_UNLINKED = "unlinked_identity"


def _detail(issuer: str, subject: str) -> str:
    return f"{issuer} (sub={subject})"


def link_identity(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issuer: str,
    subject: str,
    user_id: int,
) -> dict:
    """Bind an IdP identity to user_id and record a 'linked_identity' event atomically.

    Attributed to actor_id (the user signing in / linking — the same account, on the
    self-service SSO paths). Raises sqlite3.IntegrityError if the pair is already linked
    or user_id is unreal (the data layer's guards), unchanged from the bare call."""
    with db.transaction(conn, immediate=True):
        link = oidc.link_identity(
            conn, issuer=issuer, subject=subject, user_id=user_id, commit=False
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_LINKED,
            target_kind="user",
            target_id=user_id,
            detail=_detail(issuer, subject),
            commit=False,
        )
        return link


def unlink_identity(
    conn: sqlite3.Connection, *, actor_id: int, issuer: str, subject: str
) -> bool:
    """Remove an IdP identity from the acting user and record an 'unlinked_identity'
    event atomically. Returns True if a link was removed, False otherwise (so the caller
    can 404). No event is recorded when nothing was removed. Attributed to and recorded
    against actor_id — the self-service unlink only reaches the user's own identities."""
    with db.transaction(conn, immediate=True):
        removed = oidc.unlink_identity(
            conn, issuer=issuer, subject=subject, commit=False
        )
        if removed:
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_UNLINKED,
                target_kind="user",
                target_id=actor_id,
                detail=_detail(issuer, subject),
                commit=False,
            )
        return removed
