"""Data access for browser sessions.

A session is the server-side half of a login: the browser holds a random cookie
value, the database holds only its SHA-256 hash plus an expiry. Resolving a
cookie hashes it and looks up a live (non-expired) row, mirroring core/tokens.py.
Logout deletes the row, so a stolen-then-revoked cookie stops working at once.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from athena import config
from athena.core._util import utc_now

# SQLite stores our timestamps as "YYYY-MM-DD HH:MM:SS" UTC (datetime('now')).
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    """Open a session for a user and return the raw cookie value (shown once,
    only to the browser). Expires config.SESSION_TTL_DAYS from now.

    Also mints the session's CSRF token (fetch it with csrf_token_for). Unlike the
    cookie value, the CSRF token is stored as-is, not hashed: it is an anti-forgery
    token we must hand back to the page to embed in forms, not a credential whose
    leak grants access on its own."""
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = (utc_now() + timedelta(days=config.SESSION_TTL_DAYS)).strftime(_TS_FMT)
    conn.execute(
        "INSERT INTO sessions (user_id, session_hash, expires_at, csrf_token)"
        " VALUES (?, ?, ?, ?)",
        (user_id, _hash(raw), expires, csrf),
    )
    conn.commit()
    return raw


def resolve_session(conn: sqlite3.Connection, raw: str | None) -> dict | None:
    """Return the logged-in user for a cookie value, or None if the cookie is
    missing, unknown, or expired."""
    if not raw:
        return None
    row = conn.execute(
        "SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.session_hash = ?",
        (_hash(raw),),
    ).fetchone()
    if row is None:
        return None
    if (
        datetime.strptime(row["expires_at"], _TS_FMT).replace(tzinfo=timezone.utc)
        <= utc_now()
    ):
        return None
    user = dict(row)
    user.pop("expires_at", None)
    # request.state.user reaches templates; the hash never needs to ride along.
    user.pop("password_hash", None)
    return user


def csrf_token_for(conn: sqlite3.Connection, raw: str | None) -> str | None:
    """The CSRF token bound to a cookie's live session, or None if the cookie is
    missing, unknown, or expired. Same liveness rule as resolve_session, so a
    dead session can never yield a usable token."""
    if not raw:
        return None
    row = conn.execute(
        "SELECT expires_at, csrf_token FROM sessions WHERE session_hash = ?",
        (_hash(raw),),
    ).fetchone()
    if row is None:
        return None
    if (
        datetime.strptime(row["expires_at"], _TS_FMT).replace(tzinfo=timezone.utc)
        <= utc_now()
    ):
        return None
    return row["csrf_token"] or None


def destroy_session(conn: sqlite3.Connection, raw: str | None) -> None:
    """Delete the session a cookie names (logout). No-op if it doesn't exist."""
    if not raw:
        return
    conn.execute("DELETE FROM sessions WHERE session_hash = ?", (_hash(raw),))
    conn.commit()


def revoke_other_sessions(
    conn: sqlite3.Connection,
    user_id: int,
    keep_raw: str | None,
    *,
    commit: bool = True,
) -> int:
    """Delete every OTHER session of a user, keeping only the one `keep_raw` names.
    Called after a password change so an older or stolen cookie can't outlive the
    rotation, while the user who just changed it stays logged in. Returns how many
    were revoked. If keep_raw is missing (no current cookie), this revokes ALL of the
    user's sessions — the safe direction, forcing a fresh login everywhere.
    ``commit=False`` lets the audited password command fold the revocation, the hash
    write, and the activity event into one transaction."""
    keep_hash = _hash(keep_raw) if keep_raw else ""
    cur = conn.execute(
        "DELETE FROM sessions WHERE user_id = ? AND session_hash != ?",
        (user_id, keep_hash),
    )
    if commit:
        conn.commit()
    return cur.rowcount


def revoke_all_sessions(
    conn: sqlite3.Connection, user_id: int, *, commit: bool = True
) -> int:
    """Delete every session of a user, forcing a fresh login everywhere. The hard
    lockout half of offboarding: unlike revoke_other_sessions it keeps NO cookie
    alive, so the target's browser access ends now rather than after SESSION_TTL_DAYS.
    Returns how many were revoked. `commit=False` composes inside an offboard
    transaction so the session deletes land atomically with the token/role changes."""
    cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    if commit:
        conn.commit()
    return cur.rowcount
