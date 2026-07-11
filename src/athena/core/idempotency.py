"""Durable ownership and replay records for mutating API requests.

The ASGI middleware owns authentication, request buffering, and response replay.
This module owns the SQLite state machine. Transactions here are intentionally
short: an executing row is committed before the route runs, because the route
uses its own connection and must not wait behind an idempotency write lock.

That separation has one unavoidable consequence. If a worker disappears after a
domain commit but before it stores the response, Athena cannot know whether the
mutation happened. Such a key is therefore indeterminate and is never taken over
automatically. Failing closed is less convenient than guessing, but it cannot
silently apply the same mutation twice.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import sqlite3
import time

from athena.core import db


LEGACY_FINGERPRINT = "legacy-fingerprint-unavailable"


@dataclass(frozen=True)
class ClaimResult:
    """The durable decision for one claim/read attempt."""

    kind: str
    record: dict | None = None


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else int(value)


def _record(conn: sqlite3.Connection, *, key: str, identity: str) -> dict | None:
    row = conn.execute(
        "SELECT request_fingerprint, authorization_revision, method, path, "
        "state, owner_token, lease_expires_at, status_code, content_type, "
        "response_headers, "
        "response_body, failure_code, created_at, updated_at, expires_at "
        "FROM idempotency_keys WHERE idempotency_key = ? AND identity = ?",
        (key, identity),
    ).fetchone()
    return dict(row) if row is not None else None


def _legacy_actor_alias_record(
    conn: sqlite3.Connection, *, key: str, identity: str
) -> dict | None:
    """Canonicalize an old actor identity using Python's original int semantics.

    V1 accepted spellings such as actor:01, actor:+1, surrounding whitespace, and
    underscores. SQLite CAST does not match Python int for all of them, so migration
    SQL alone cannot safely collapse aliases. This runs under the claim transaction.
    """
    if not identity.startswith("actor:"):
        return None
    try:
        wanted_id = int(identity.removeprefix("actor:"))
    except ValueError:
        return None

    aliases = []
    for row in conn.execute(
        "SELECT identity FROM idempotency_keys "
        "WHERE idempotency_key = ? AND substr(identity, 1, 6) = 'actor:'",
        (key,),
    ):
        try:
            if int(row["identity"].removeprefix("actor:")) == wanted_id:
                aliases.append(row["identity"])
        except ValueError:
            continue
    if not aliases:
        return None
    if identity in aliases:
        for alias in aliases:
            if alias != identity:
                conn.execute(
                    "DELETE FROM idempotency_keys "
                    "WHERE idempotency_key = ? AND identity = ?",
                    (key, alias),
                )
        return _record(conn, key=key, identity=identity)

    chosen = sorted(aliases)[0]
    conn.execute(
        "UPDATE idempotency_keys SET identity = ? "
        "WHERE idempotency_key = ? AND identity = ?",
        (identity, key, chosen),
    )
    for alias in aliases:
        if alias != chosen:
            conn.execute(
                "DELETE FROM idempotency_keys "
                "WHERE idempotency_key = ? AND identity = ?",
                (key, alias),
            )
    return _record(conn, key=key, identity=identity)


def _authorization_changed(
    conn: sqlite3.Connection, *, key: str, identity: str
) -> None:
    """Purge a stale response and retain a non-expiring, fail-closed marker."""
    conn.execute(
        "UPDATE idempotency_keys SET "
        "state = 'indeterminate', owner_token = NULL, lease_expires_at = NULL, "
        "status_code = NULL, content_type = NULL, response_headers = NULL, "
        "response_body = NULL, failure_code = 'authorization_changed', "
        "updated_at = datetime('now'), expires_at = NULL "
        "WHERE idempotency_key = ? AND identity = ?",
        (key, identity),
    )


def claim_or_read(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    method: str,
    path: str,
    request_fingerprint: str,
    lease_seconds: int,
    now: int | None = None,
) -> ClaimResult:
    """Atomically become the owner, observe a result, or get a safe refusal.

    BEGIN IMMEDIATE serializes the read/insert decision across processes. No
    transaction survives this function, so the winning route can write using its
    normal connection. A live executing owner is observed, never replaced.
    """
    current = _now(now)
    with db.transaction(conn, immediate=True):
        authorization_revision = conn.execute(
            "SELECT revision FROM idempotency_authorization_state "
            "WHERE singleton = 1"
        ).fetchone()["revision"]
        record = _legacy_actor_alias_record(
            conn, key=key, identity=identity
        )
        if record is None:
            record = _record(conn, key=key, identity=identity)

        if record is not None:
            # V1 receipts cannot prove that the retry body is the original body.
            # Their key remains an explicit ambiguity marker after aliases collapse.
            if record["request_fingerprint"] == LEGACY_FINGERPRINT:
                if record["method"] != method or record["path"] != path:
                    return ClaimResult("mismatch", record)
                return ClaimResult("indeterminate", record)
            if record["state"] == "indeterminate":
                kind = (
                    "authorization_changed"
                    if record["failure_code"] == "authorization_changed"
                    else "indeterminate"
                )
                return ClaimResult(kind, record)
            if record["authorization_revision"] != authorization_revision:
                if record["state"] == "executing":
                    return ClaimResult("authorization_changed", record)
                # Authorization changed after claim. Purge the stored body and keep
                # the key forever fail-closed; neither replay nor re-execution is safe.
                _authorization_changed(conn, key=key, identity=identity)
                return ClaimResult(
                    "authorization_changed",
                    _record(conn, key=key, identity=identity),
                )
            if record["state"] == "completed" and int(record["expires_at"]) <= current:
                # The target key must expire even when more than the cleanup batch's
                # worth of older receipts exist.
                conn.execute(
                    "DELETE FROM idempotency_keys "
                    "WHERE idempotency_key = ? AND identity = ?",
                    (key, identity),
                )
                record = None

        # Lazy, bounded retention for other keys. Executing and indeterminate rows
        # never expire automatically because their mutation outcome may be unknown.
        conn.execute(
            "DELETE FROM idempotency_keys WHERE rowid IN ("
            "SELECT rowid FROM idempotency_keys "
            "WHERE state = 'completed' AND expires_at <= ? LIMIT 100)",
            (current,),
        )

        if record is None:
            owner_token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO idempotency_keys ("
                "idempotency_key, identity, request_fingerprint, "
                "authorization_revision, method, path, state, owner_token, "
                "lease_expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'executing', ?, ?)",
                (
                    key,
                    identity,
                    request_fingerprint,
                    authorization_revision,
                    method,
                    path,
                    owner_token,
                    current + max(1, int(lease_seconds)),
                ),
            )
            return ClaimResult("owner", {"owner_token": owner_token})

        if record["request_fingerprint"] != request_fingerprint:
            return ClaimResult("mismatch", record)
        if (
            record["state"] == "executing"
            and int(record["lease_expires_at"]) <= current
        ):
            # Do not rewrite or steal the row. A slow original owner may still
            # finish and publish its fenced result; a dead one remains fail-closed.
            return ClaimResult("indeterminate", record)
        if record["state"] == "completed":
            return ClaimResult("replay", record)
        return ClaimResult("in_progress", record)


def complete(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    owner_token: str,
    status_code: int,
    content_type: str | None,
    response_headers: str,
    response_body: bytes,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    """Publish a bounded 2xx response iff this caller still owns the claim.

    Returns completed, authorization_changed, or lost. An authorization change
    during route execution purges the replay body but still lets middleware send
    this fresh response; subsequent retries remain fail-closed.
    """
    current = _now(now)
    with db.transaction(conn, immediate=True):
        row = conn.execute(
            "SELECT authorization_revision FROM idempotency_keys "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (key, identity, owner_token),
        ).fetchone()
        if row is None:
            return "lost"
        authorization_revision = conn.execute(
            "SELECT revision FROM idempotency_authorization_state "
            "WHERE singleton = 1"
        ).fetchone()["revision"]
        if row["authorization_revision"] != authorization_revision:
            _authorization_changed(conn, key=key, identity=identity)
            return "authorization_changed"

        cur = conn.execute(
            "UPDATE idempotency_keys SET "
            "state = 'completed', owner_token = NULL, lease_expires_at = NULL, "
            "status_code = ?, content_type = ?, response_headers = ?, "
            "response_body = ?, failure_code = NULL, updated_at = datetime('now'), "
            "expires_at = ? "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (
                status_code,
                content_type,
                response_headers,
                response_body,
                current + max(1, int(ttl_seconds)),
                key,
                identity,
                owner_token,
            ),
        )
        return "completed" if cur.rowcount == 1 else "lost"


def release(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    owner_token: str,
) -> bool:
    """Release a claim after an ordinary non-5xx response, fenced by owner."""
    with db.transaction(conn, immediate=True):
        cur = conn.execute(
            "DELETE FROM idempotency_keys "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (key, identity, owner_token),
        )
        return cur.rowcount == 1


def mark_indeterminate(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    owner_token: str,
    failure_code: str,
) -> bool:
    """Fence a key after an outcome that may have followed a committed write."""
    with db.transaction(conn, immediate=True):
        cur = conn.execute(
            "UPDATE idempotency_keys SET "
            "state = 'indeterminate', owner_token = NULL, lease_expires_at = NULL, "
            "failure_code = ?, updated_at = datetime('now') "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (failure_code, key, identity, owner_token),
        )
        return cur.rowcount == 1


def get_record(
    conn: sqlite3.Connection, *, key: str, identity: str
) -> dict | None:
    """Inspection helper for diagnostics and state-machine tests."""
    return _record(conn, key=key, identity=identity)
