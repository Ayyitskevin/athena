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
from dataclasses import dataclass
import hashlib
import secrets
import sqlite3
from typing import Literal

from athena.core import db

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

TokenCredentialState = Literal["active", "revoked", "invalid"]


@dataclass(frozen=True, slots=True)
class TokenCredential:
    """Side-effect-free authentication facts for one presented bearer token."""

    state: TokenCredentialState
    token_id: int | None = None
    user_id: int | None = None
    actor: dict | None = None


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
    commit: bool,
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


def inspect_token(conn: sqlite3.Connection, raw: str) -> TokenCredential:
    """Classify a bearer token without stamping, committing, throttling, or auditing.

    This is the single owner of token validity: hash lookup, revocation, user
    existence, and stored-scope parsing all complete before a credential is active.
    Transport policy consumes these facts and owns its own effects.
    """
    row = conn.execute(
        "SELECT id, user_id, scopes, revoked_at FROM api_tokens WHERE token_hash = ?",
        (_hash(raw),),
    ).fetchone()
    if row is None:
        return TokenCredential("invalid")
    if row["revoked_at"] is not None:
        return TokenCredential("revoked", token_id=row["id"], user_id=row["user_id"])
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (row["user_id"],)
    ).fetchone()
    if user is None:
        return TokenCredential("invalid")
    raw_scopes = row["scopes"]
    if raw_scopes is not None and not isinstance(raw_scopes, str):
        return TokenCredential("invalid")
    try:
        parsed_scopes = parse_scopes(raw_scopes)
    except ValueError:
        return TokenCredential("invalid")
    actor = dict(user)
    actor["_token_id"] = row["id"]
    actor["_token_scopes"] = parsed_scopes
    return TokenCredential(
        "active", token_id=row["id"], user_id=row["user_id"], actor=actor
    )


def resolve_token(conn: sqlite3.Connection, raw: str) -> dict | None:
    """Authenticate and stamp a valid raw token; return its actor or None."""
    credential = inspect_token(conn, raw)
    if (
        credential.state != "active"
        or credential.token_id is None
        or credential.actor is None
    ):
        return None
    _stamp_last_use(conn, credential.token_id)
    return credential.actor


#: A token's last use is stamped at most this often (H7). The stamp says
#: whether a credential is still in use (the operator's stale-token warning
#: counts days), so a minute's resolution loses nothing, and every
#: bearer-authenticated request in between is spared the writer lock.
LAST_USED_STAMP_SECONDS = 60
#: How long the stamp itself may wait for another writer. It is advisory: a
#: read must not queue behind a long write to record that it happened.
_STAMP_WAIT_MS = 250


def _stamp_last_use(conn: sqlite3.Connection, token_id: int) -> None:
    """Record that this token was just used, unless it was stamped within the
    last minute or the database is busy.

    The check is a read and takes no lock. Only a due stamp writes, and it
    waits briefly: if another writer holds the database the stamp is skipped
    and the next request tries again, so authenticating never fails, and
    never stalls, for want of a bookkeeping write."""
    due = conn.execute(
        "SELECT last_used_at IS NULL OR last_used_at <= datetime('now', ?) AS due "
        "FROM api_tokens WHERE id = ?",
        (f"-{LAST_USED_STAMP_SECONDS} seconds", token_id),
    ).fetchone()
    if due is None or not due["due"]:
        return
    conn.execute(f"PRAGMA busy_timeout = {_STAMP_WAIT_MS}")
    try:
        conn.execute(
            "UPDATE api_tokens SET last_used_at = datetime('now') WHERE id = ?",
            (token_id,),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        if not db.is_lock_timeout(exc):
            raise
        conn.rollback()
    finally:
        conn.execute(f"PRAGMA busy_timeout = {db.BUSY_TIMEOUT_MS}")


def get_revoked_token(conn: sqlite3.Connection, raw: str) -> dict | None:
    """Identify a revoked credential for audit attribution without authenticating."""
    credential = inspect_token(conn, raw)
    if (
        credential.state != "revoked"
        or credential.token_id is None
        or credential.user_id is None
    ):
        return None
    return {"id": credential.token_id, "user_id": credential.user_id}


def list_tokens(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Every token belonging to a user, hash withheld. Revoked ones included so
    the owner can see (and stop re-creating) them."""
    rows = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM api_tokens WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return [_public_row(r) for r in rows]


def revoke_token(
    conn: sqlite3.Connection, *, user_id: int, token_id: int, commit: bool
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
    conn: sqlite3.Connection, *, user_id: int, commit: bool
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
