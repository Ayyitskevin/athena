"""Data access for users.

All user SQL lives here, mirroring aegis/issues.py: HTTP handlers call these
functions instead of writing queries, so storage changes touch only this file.

Users are the actors in Athena — people now, agents later. Issues (and, later,
docs) point at a user via a foreign key, so a user must exist before it can act.
"""

from __future__ import annotations

import sqlite3

from athena.core import passwords

ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
VIEWER_ROLE = "viewer"
ROLES = (ADMIN_ROLE, MEMBER_ROLE, VIEWER_ROLE)
DEFAULT_ROLE = MEMBER_ROLE
BOOTSTRAP_ROLE = ADMIN_ROLE


def normalize_role(role: str | None) -> str:
    """Return a canonical role or raise ValueError for an unknown one."""
    value = (role or DEFAULT_ROLE).strip().lower()
    if value not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    return value


def create_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    name: str,
    password: str | None = None,
    role: str | None = None,
) -> dict:
    """Insert a user and return it. An optional password enables browser login;
    without one the user exists as an actor but can only act via API tokens.
    Raises sqlite3.IntegrityError if the email is already taken."""
    role = normalize_role(role)
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, ?)",
        (
            email,
            name,
            passwords.hash_password(password) if password else None,
            role,
        ),
    )
    conn.commit()
    return get_user(conn, cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    """Set or replace a user's login password."""
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (passwords.hash_password(password), user_id),
    )
    conn.commit()


def verify_credentials(
    conn: sqlite3.Connection, *, email: str, password: str
) -> dict | None:
    """Return the user iff the email exists and the password matches. One opaque
    None for both 'no such email' and 'wrong password' — don't reveal which."""
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or not passwords.verify_password(password, row["password_hash"]):
        return None
    return dict(row)


def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def set_role(conn: sqlite3.Connection, user_id: int, role: str) -> dict | None:
    """Change a user's role and return the updated row, or None if missing."""
    role = normalize_role(role)
    cur = conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_user(conn, user_id)


def count_admins(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role = ?", (ADMIN_ROLE,)
    ).fetchone()["n"]


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def count_users(conn: sqlite3.Connection) -> int:
    """How many users exist. Used by the bootstrap rule: the first user can be
    created without authentication (nobody could be authenticated yet); after
    that, creating users requires an authenticated actor."""
    return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
