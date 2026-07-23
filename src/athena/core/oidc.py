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
    conn: sqlite3.Connection,
    *,
    issuer: str,
    subject: str,
    user_id: int,
    commit: bool = True,
) -> dict:
    """Bind an external IdP identity (issuer + subject) to an Athena user and return
    the link. Raises sqlite3.IntegrityError if this (issuer, subject) is already
    linked (the composite PK keeps one provider-identity mapped to one user) or if
    user_id isn't a real user (the foreign key). ``commit=False`` lets an audited
    command fold the link and its activity event into one transaction."""
    conn.execute(
        "INSERT INTO oidc_identities (issuer, subject, user_id) VALUES (?, ?, ?)",
        (issuer, subject, user_id),
    )
    if commit:
        conn.commit()
    return get_identity(conn, issuer=issuer, subject=subject)


def get_identity(conn: sqlite3.Connection, *, issuer: str, subject: str) -> dict | None:
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
    conn: sqlite3.Connection, *, issuer: str, subject: str, commit: bool = True
) -> bool:
    """Remove a link. Returns True if one was removed, False if it wasn't linked (so
    the caller can 404). The user row itself is untouched. ``commit=False`` lets an
    audited command fold the unlink and its activity event into one transaction."""
    cur = conn.execute(
        "DELETE FROM oidc_identities WHERE issuer = ? AND subject = ?",
        (issuer, subject),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


# --- in-flight login state (the /login/sso ↔ /auth/callback handoff) --------


def start_login(
    conn: sqlite3.Connection, *, state: str, nonce: str, code_verifier: str
) -> None:
    """Stash the secrets for one in-flight SSO login, keyed by its (unguessable)
    state. Recorded at /login/sso; consumed once at /auth/callback."""
    conn.execute(
        "INSERT INTO oidc_login_states (state, nonce, code_verifier) VALUES (?, ?, ?)",
        (state, nonce, code_verifier),
    )
    conn.commit()


def take_login_state(
    conn: sqlite3.Connection, state: str, *, max_age_seconds: int = 600
) -> dict | None:
    """Consume the login state for `state`: return its {nonce, code_verifier} and
    DELETE it (single-use — a code/state pair can't be replayed), or None if there is
    no such state or it has expired. Also sweeps any other expired rows, so abandoned
    logins don't accumulate. Time is compared in SQLite so it matches created_at's
    clock exactly."""
    row = conn.execute(
        "SELECT nonce, code_verifier FROM oidc_login_states "
        "WHERE state = ? AND created_at >= datetime('now', ?)",
        (state, f"-{int(max_age_seconds)} seconds"),
    ).fetchone()
    # Delete this state (whether fresh or expired) plus anything else stale, in one
    # commit, so the row is gone before we trust it and abandoned rows don't linger.
    conn.execute("DELETE FROM oidc_login_states WHERE state = ?", (state,))
    conn.execute(
        "DELETE FROM oidc_login_states WHERE created_at < datetime('now', ?)",
        (f"-{int(max_age_seconds)} seconds",),
    )
    conn.commit()
    return dict(row) if row else None
