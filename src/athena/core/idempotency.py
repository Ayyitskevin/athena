"""Data access for idempotency keys — replaying the first response to a retried write.

The middleware (athena.main.IdempotencyMiddleware) owns the request/response
plumbing; this module owns the SQL, mirroring the other core data modules. A record
is the stored outcome of one (key, identity) write: the status, content type, and
raw body to replay verbatim on a repeat.
"""
from __future__ import annotations

import sqlite3


def get(conn: sqlite3.Connection, *, key: str, identity: str) -> dict | None:
    """The stored response for this (key, identity), or None if this is the first
    time we've seen it. identity scopes the key to the caller so two callers reusing
    the same key string never read each other's responses."""
    row = conn.execute(
        "SELECT method, path, status_code, content_type, response_body "
        "FROM idempotency_keys WHERE idempotency_key = ? AND identity = ?",
        (key, identity),
    ).fetchone()
    return dict(row) if row else None


def store(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    method: str,
    path: str,
    status_code: int,
    content_type: str | None,
    response_body: bytes,
) -> None:
    """Record the first response for this (key, identity). INSERT OR IGNORE so a
    concurrent first-write that lost the race is a harmless no-op (the row already
    there wins) rather than an IntegrityError."""
    conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys "
        "(idempotency_key, identity, method, path, status_code, content_type, "
        "response_body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key, identity, method, path, status_code, content_type, response_body),
    )
    conn.commit()
