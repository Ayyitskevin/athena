"""Data access for users.

All user SQL lives here, mirroring aegis/issues.py: HTTP handlers call these
functions instead of writing queries, so storage changes touch only this file.

Users are the actors in Athena — people now, agents later. Issues (and, later,
docs) point at a user via a foreign key, so a user must exist before it can act.
"""
from __future__ import annotations

import sqlite3

from athena.core import passwords


def create_user(
    conn: sqlite3.Connection, *, email: str, name: str, password: str | None = None
) -> dict:
    """Insert a user and return it. An optional password enables browser login;
    without one the user exists as an actor but can only act via API tokens.
    Raises sqlite3.IntegrityError if the email is already taken."""
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
        (email, name, passwords.hash_password(password) if password else None),
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


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]
