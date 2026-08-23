"""Data access for users.

All user SQL lives here, mirroring aegis/issues.py: HTTP handlers call these
functions instead of writing queries, so storage changes touch only this file.

Users are the actors in Athena — people now, agents later. Issues (and, later,
docs) point at a user via a foreign key, so a user must exist before it can act.
"""

from __future__ import annotations

import re
import sqlite3

from athena.core import passwords

ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
VIEWER_ROLE = "viewer"
ROLES = (ADMIN_ROLE, MEMBER_ROLE, VIEWER_ROLE)
DEFAULT_ROLE = MEMBER_ROLE
BOOTSTRAP_ROLE = ADMIN_ROLE
MAX_EMAIL_CHARS = 320
MAX_NAME_CHARS = 512
_BROWSER_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def is_browser_login_email(value: object) -> bool:
    """Whether an identifier survives the shipped HTML email input unchanged."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_EMAIL_CHARS
        and value.isascii()
        and _BROWSER_EMAIL_RE.fullmatch(value) is not None
    )


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
    is_agent: bool = False,
    commit: bool = True,
) -> dict:
    """Insert a user and return it. An optional password enables browser login;
    without one the user exists as an actor but can only act via API tokens.
    is_agent marks the user as an agent rather than a person — a display/audit
    distinction only (roles, not this flag, govern what a user may do).
    Raises sqlite3.IntegrityError if the email is already taken. ``commit=False`` lets
    an audited command fold the insert and its activity event into one transaction."""
    role = normalize_role(role)
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash, role, is_agent) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            email,
            name,
            passwords.hash_password(password) if password else None,
            role,
            1 if is_agent else 0,
        ),
    )
    if commit:
        conn.commit()
    user_id = cur.lastrowid
    assert user_id is not None
    user = get_user(conn, user_id)
    assert user is not None
    return user


def _row_to_user(row: sqlite3.Row) -> dict:
    """Turn a users row into a dict, coercing SQLite's 0/1 is_agent into a real
    bool so every caller (templates, JSON, tests) sees a proper boolean."""
    user = dict(row)
    if "is_agent" in user:
        user["is_agent"] = bool(user["is_agent"])
    return user


def _with_password_state(user: dict) -> dict:
    user["has_password"] = bool(user.get("password_hash"))
    user.pop("password_hash", None)
    return user


def set_password(
    conn: sqlite3.Connection, user_id: int, password: str, *, commit: bool = True
) -> dict | None:
    """Set or replace a user's login password and return the updated row.
    ``commit=False`` lets ``user_commands`` fold the hash write, the session
    revocation it forces, and the audit event into one transaction."""
    passwords.validate_new_password(password)
    cur = conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (passwords.hash_password(password), user_id),
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_user(conn, user_id)


def verify_credentials(
    conn: sqlite3.Connection, *, email: str, password: str
) -> dict | None:
    """Return the user iff the email exists and the password matches. One opaque
    None for both 'no such email' and 'wrong password' — don't reveal which, in
    the response OR in timing: when there is no stored hash to check (unknown
    email, or an API-only account with no password) we still burn a full PBKDF2
    verification so both rejections cost the same."""
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # A removed account is indistinguishable from an unknown email — same
    # opaque None, same full-cost verification, so removal leaks nothing.
    if row is None or row["removed_at"] is not None or not row["password_hash"]:
        passwords.dummy_verify(password)
        return None
    if not passwords.verify_password(password, row["password_hash"]):
        return None
    # Successful login with a hash stored at an older cost: transparently upgrade
    # it while we hold the plaintext. This is the one password write that stays
    # outside the command layer: it re-encodes the SAME credential at a newer cost
    # rather than replacing it, so there is no lifecycle change to attribute (the
    # audited replacements live in user_commands), per docs/COMMAND_MIGRATION.md.
    if passwords.needs_rehash(row["password_hash"]):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (passwords.rehash_verified_legacy_password(password), row["id"]),
        )
        conn.commit()
    return _row_to_user(row)


def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_users_by_ids(conn: sqlite3.Connection, user_ids: list[int]) -> dict[int, dict]:
    """One lookup for a set of user ids. Missing ids are omitted."""
    ids = sorted({int(uid) for uid in user_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM users WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {int(row["id"]): _row_to_user(row) for row in rows}


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    """Email lookup is a LIVENESS operation — login, OIDC, assignment by
    address — so a removed account is not findable here. Attribution and
    display resolve by id (get_user / get_users_by_ids), which never filter."""
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? AND removed_at IS NULL", (email,)
    ).fetchone()
    return _row_to_user(row) if row else None


def set_role(
    conn: sqlite3.Connection, user_id: int, role: str, *, commit: bool = True
) -> dict | None:
    """Change a user's role and return the updated row, or None if missing.
    `commit=False` lets a compound command (e.g. offboarding) fold the role change
    into one transaction with the session/token revocations and the audit event."""
    role = normalize_role(role)
    cur = conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_user(conn, user_id)


def set_agent(
    conn: sqlite3.Connection, user_id: int, is_agent: bool, *, commit: bool = True
) -> dict | None:
    """Mark (or unmark) a user as an agent and return the updated row, or None if
    the user doesn't exist. A display/audit distinction only — it changes nothing
    about what the user is allowed to do. ``commit=False`` lets an audited command
    fold the flip and its activity event into one transaction."""
    cur = conn.execute(
        "UPDATE users SET is_agent = ? WHERE id = ?",
        (1 if is_agent else 0, user_id),
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_user(conn, user_id)


def count_admins(conn: sqlite3.Connection) -> int:
    # A removed admin cannot administrate, so it never satisfies a
    # last-admin guard.
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role = ? AND removed_at IS NULL",
        (ADMIN_ROLE,),
    ).fetchone()["n"]


def count_active_admins(conn: sqlite3.Connection) -> int:
    """Admins who can actually act: the admin role AND not paused or removed.
    The pause guard counts these — pausing the last one would brick the
    workspace (a paused admin cannot resume anyone, itself included)."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users "
        "WHERE role = ? AND paused_at IS NULL AND removed_at IS NULL",
        (ADMIN_ROLE,),
    ).fetchone()["n"]


def set_paused(
    conn: sqlite3.Connection, user_id: int, paused: bool, *, commit: bool = True
) -> dict | None:
    """Pause (or resume) a user and return the updated row, or None if the user
    doesn't exist. Pausing freezes the account at identity resolution without
    destroying anything; resume restores it exactly. ``commit=False`` lets the
    audited command fold the flip and its activity event into one transaction."""
    cur = conn.execute(
        "UPDATE users SET paused_at = ? WHERE id = ?",
        (_now(conn) if paused else None, user_id),
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_user(conn, user_id)


def set_removed(
    conn: sqlite3.Connection, user_id: int, removed: bool, *, commit: bool = True
) -> dict | None:
    """Stamp (or clear) the removal tombstone and return the updated row, or
    None if the user doesn't exist. Removal hides the account from every list
    and lookup-by-email while every attributed row keeps its real user; restore
    clears the stamp and nothing else. ``commit=False`` lets the audited
    command fold the flip and its activity event into one transaction."""
    cur = conn.execute(
        "UPDATE users SET removed_at = ? WHERE id = ?",
        (_now(conn) if removed else None, user_id),
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_user(conn, user_id)


def _now(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT datetime('now')").fetchone()[0]


def list_users(
    conn: sqlite3.Connection, *, include_removed: bool = False
) -> list[dict]:
    """Every list and picker in the app builds from here, so removed users
    vanish everywhere by default; pass ``include_removed=True`` only for a
    surface that deliberately shows tombstones."""
    clause = "" if include_removed else " WHERE removed_at IS NULL"
    rows = conn.execute(f"SELECT * FROM users{clause} ORDER BY id").fetchall()
    return [_with_password_state(_row_to_user(row)) for row in rows]


def count_users(conn: sqlite3.Connection) -> int:
    """How many users exist. Used by the bootstrap rule: the process-configured
    one-time credential may create exactly the first user; after that, creating
    users requires an authenticated actor."""
    return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
