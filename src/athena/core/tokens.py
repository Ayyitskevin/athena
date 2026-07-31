"""Data access for API tokens — the authenticated way to act as a user.

A token proves identity; the X-Athena-Actor header only claims it. We generate
a random token, hand the caller the raw value exactly once, and persist only its
SHA-256 hash. On each request the presented token is hashed and looked up here,
so the database never holds a credential an attacker could replay.

Tokens also carry coarse scopes. User roles remain the outer authorization cap;
token scopes can only narrow what a bearer token may do as that user.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import secrets
import sqlite3

# Raw tokens carry a short prefix so humans and secret-scanners recognize them.
# The hash is computed over the whole string, prefix included.
TOKEN_PREFIX = "ath_"

READ_SCOPE = "read"
ISSUE_WRITE_SCOPE = "issue:write"
DOCS_WRITE_SCOPE = "docs:write"
ADMIN_SCOPE = "admin"
SCOPES = (READ_SCOPE, ISSUE_WRITE_SCOPE, DOCS_WRITE_SCOPE, ADMIN_SCOPE)
# What a LEGACY stored row with no scopes column value means (rows minted before
# scopes existed): full access. Deliberately NOT a default for new mints — there
# is no default; normalize_scopes requires an explicit choice.
LEGACY_SCOPES = (ADMIN_SCOPE,)

# Columns safe to return to a caller — never the hash.
_PUBLIC_COLS = "id, user_id, name, scopes, created_at, last_used_at, revoked_at"


def _hash(raw: str) -> str:
    """SHA-256 hex of a raw token. Deterministic, so the same token always maps
    to the same lookup key; one-way, so the stored value can't be reversed."""
    return hashlib.sha256(raw.encode()).hexdigest()


def is_valid_hash(stored: object) -> bool:
    """Whether a stored value could match this release's SHA-256 token lookup."""
    if not isinstance(stored, str) or len(stored) != hashlib.sha256().digest_size * 2:
        return False
    try:
        digest = bytes.fromhex(stored)
    except ValueError:
        return False
    return stored == digest.hex()


def has_live_scope_credential(
    conn: sqlite3.Connection, *, user_id: int, scope: str
) -> bool:
    """Whether a live token row has a usable verifier and the requested scope."""
    rows = conn.execute(
        "SELECT token_hash, scopes FROM api_tokens "
        "WHERE user_id = ? AND revoked_at IS NULL",
        (user_id,),
    ).fetchall()
    for row in rows:
        raw_scopes = row["scopes"]
        if raw_scopes is not None and not isinstance(raw_scopes, str):
            continue
        try:
            stored_scopes = parse_scopes(raw_scopes)
        except ValueError:
            continue
        if scope in stored_scopes and is_valid_hash(row["token_hash"]):
            return True
    return False


def normalize_scopes(scopes: Iterable[str] | None) -> tuple[str, ...]:
    """Validate and canonicalize token scopes for storage and policy checks.

    Omitting scopes on a MINT is an error: an agent-credential product must not
    fail open, and the old behavior (None silently meant admin) inverted least
    privilege — forgetting a field granted everything. Callers must say what a
    token may do. (Legacy STORED rows with no scopes still read as full access —
    that compatibility lives in parse_scopes, which never round-trips through
    here for the missing case.)"""
    if scopes is None:
        raise ValueError(
            "token scopes are required; pass an explicit list "
            f"(one or more of: {', '.join(SCOPES)})"
        )

    seen: set[str] = set()
    for raw in scopes:
        value = raw.strip().lower()
        if value not in SCOPES:
            raise ValueError(f"scope must be one of: {', '.join(SCOPES)}")
        seen.add(value)

    if not seen:
        raise ValueError("at least one token scope is required")
    if ADMIN_SCOPE in seen:
        return (ADMIN_SCOPE,)
    return tuple(scope for scope in SCOPES if scope in seen)


def parse_scopes(raw: str | None) -> tuple[str, ...]:
    """Parse the DB representation. A missing/empty STORED value means legacy full
    access — rows minted before scopes existed keep their meaning. (New mints can
    never produce such a row: normalize_scopes refuses omitted scopes.)"""
    if raw is None or raw.strip() == "":
        return LEGACY_SCOPES
    return normalize_scopes(raw.split())


def _pack_scopes(scopes: Iterable[str] | None) -> str:
    return " ".join(normalize_scopes(scopes))


def _public_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["scopes"] = list(parse_scopes(out.get("scopes")))
    return out


def create_token(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    name: str,
    scopes: Iterable[str] | None = None,
    commit: bool = True,
) -> dict:
    """Mint a token for a user. Returns the row plus a one-time `token` field
    holding the raw secret — it is never retrievable again after this call.
    ``commit=False`` lets an audited command fold the mint and its activity event
    into one transaction, so a credential and its provenance land together."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash, scopes) VALUES (?, ?, ?, ?)",
        (user_id, name, _hash(raw), _pack_scopes(scopes)),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM api_tokens WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return {**_public_row(row), "token": raw}


def resolve_token(conn: sqlite3.Connection, raw: str) -> dict | None:
    """Authenticate a raw token: return the acting user, or None if the token is
    unknown or revoked. Stamps last_used_at on success for audit."""
    row = conn.execute(
        "SELECT id, user_id, scopes FROM api_tokens"
        " WHERE token_hash = ? AND revoked_at IS NULL",
        (_hash(raw),),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE api_tokens SET last_used_at = datetime('now') WHERE id = ?",
        (row["id"],),
    )
    conn.commit()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (row["user_id"],)
    ).fetchone()
    if user is None:
        return None
    actor = dict(user)
    actor["_token_id"] = row["id"]
    actor["_token_scopes"] = parse_scopes(row["scopes"])
    return actor


def get_revoked_token(conn: sqlite3.Connection, raw: str) -> dict | None:
    """Identify a REVOKED token presented as a credential: {id, user_id}, or None
    when the raw value matches no revoked token. Failure auditing uses this to
    attribute the attempt to the token's owner; it never authenticates."""
    row = conn.execute(
        "SELECT id, user_id FROM api_tokens"
        " WHERE token_hash = ? AND revoked_at IS NOT NULL",
        (_hash(raw),),
    ).fetchone()
    return dict(row) if row is not None else None


def list_tokens(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Every token belonging to a user, hash withheld. Revoked ones included so
    the owner can see (and stop re-creating) them."""
    rows = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM api_tokens WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return [_public_row(r) for r in rows]


def revoke_token(
    conn: sqlite3.Connection, *, user_id: int, token_id: int, commit: bool = True
) -> bool:
    """Disable a token the user owns. Returns False if no such live token exists
    (wrong owner, unknown id, or already revoked) so the API can 404 honestly.
    ``commit=False`` lets an audited command fold the revoke and its activity event
    into one transaction, so a credential's lockout and its record land together."""
    cur = conn.execute(
        "UPDATE api_tokens SET revoked_at = datetime('now')"
        " WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (token_id, user_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def revoke_all_tokens_for_user(
    conn: sqlite3.Connection, *, user_id: int, commit: bool = True
) -> int:
    """Revoke EVERY live token a user holds; return how many were revoked.

    Unlike revoke_token this is NOT owner-scoped — it is the operator's fleet-level
    kill switch for a compromised or runaway agent, so the CALLER must gate it on
    admin. Idempotent: a second call revokes nothing and returns 0. `commit=False`
    lets an offboard command bundle this with the session/role changes and the audit
    event in one transaction, so the credential and its lockout record land together."""
    cur = conn.execute(
        "UPDATE api_tokens SET revoked_at = datetime('now')"
        " WHERE user_id = ? AND revoked_at IS NULL",
        (user_id,),
    )
    if commit:
        conn.commit()
    return cur.rowcount
