"""Data access for SSO identity links (oidc_identities).

The bridge between an external OpenID Connect identity — the (issuer, subject) pair
an IdP asserts — and a local Athena user. The login FLOW (discovery, token
verification, provisioning, session) lives elsewhere; this module only persists and
resolves the link, mirroring the other core data-access modules (users.py, tokens.py):
HTTP/flow code calls these functions instead of writing SQL.

`sub` is the IdP's stable, opaque user id (unlike email, it never changes), so the
link keys on (issuer, subject) — never on email.
"""
from __future__ import annotations

import sqlite3

from athena.core import users


def link_identity(
    conn: sqlite3.Connection, *, issuer: str, subject: str, user_id: int
) -> dict:
    """Bind an external IdP identity (issuer + subject) to an Athena user and return
    the link. Raises sqlite3.IntegrityError if this (issuer, subject) is already
    linked (the composite PK keeps one provider-identity mapped to one user) or if
    user_id isn't a real user (the foreign key)."""
    conn.execute(
        "INSERT INTO oidc_identities (issuer, subject, user_id) VALUES (?, ?, ?)",
        (issuer, subject, user_id),
    )
    conn.commit()
    return get_identity(conn, issuer=issuer, subject=subject)


def get_identity(
    conn: sqlite3.Connection, *, issuer: str, subject: str
) -> dict | None:
    row = conn.execute(
        "SELECT issuer, subject, user_id, created_at FROM oidc_identities "
        "WHERE issuer = ? AND subject = ?",
        (issuer, subject),
    ).fetchone()
    return dict(row) if row else None


def find_user_by_identity(
    conn: sqlite3.Connection, *, issuer: str, subject: str
) -> dict | None:
    """The Athena user this IdP identity logs into, or None when it has never been
    linked (a first SSO login). Returns the full user via users.get_user so the
    caller can start a session, exactly as a password login would."""
    identity = get_identity(conn, issuer=issuer, subject=subject)
    return users.get_user(conn, identity["user_id"]) if identity else None


def list_identities(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """The IdP identities linked to one user (for an account view), newest first."""
    rows = conn.execute(
        "SELECT issuer, subject, created_at FROM oidc_identities "
        "WHERE user_id = ? ORDER BY created_at DESC, issuer",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def unlink_identity(
    conn: sqlite3.Connection, *, issuer: str, subject: str
) -> bool:
    """Remove a link. Returns True if one was removed, False if it wasn't linked (so
    the caller can 404). The user row itself is untouched."""
    cur = conn.execute(
        "DELETE FROM oidc_identities WHERE issuer = ? AND subject = ?",
        (issuer, subject),
    )
    conn.commit()
    return cur.rowcount > 0
