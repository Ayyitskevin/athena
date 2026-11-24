"""What an operator and a starting process may do to an idempotency key (H14).

A keyed write that a hard kill interrupts leaves its key ``executing`` with
no owner. A retry was told ``idempotency_in_progress`` until the claim's lease
ran out, about a minute, then ``idempotency_indeterminate`` for ever, and
nothing listed such keys or let anyone settle one.

Two commands:

- :func:`orphan_executing` runs when a process starts serving a database no
  other process is serving. Every key still ``executing`` then belongs to a
  process that no longer exists, so each becomes indeterminate at once and a
  retry is told the truth without the wait.
- :func:`resolve` records what an admin found when they read the target of a
  key whose outcome is unknown. ``not_applied``: the key is forgotten, and the
  same request under it runs. ``applied``: the key keeps refusing, now with
  ``idempotency_applied``, so the caller learns its write landed and does not
  send it under a new key. Either way the trail says who decided and what.

Neither replays anything: the response the first request would have produced
was never stored and cannot be rebuilt.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, idempotency

VERB_RESOLVED_KEY = "resolved_idempotency_key"

APPLIED = "applied"
NOT_APPLIED = "not_applied"
OUTCOMES = (APPLIED, NOT_APPLIED)


class KeyNotIndeterminate(LookupError):
    """No key with that id and text has an unknown outcome now."""


def orphan_executing(conn: sqlite3.Connection) -> int:
    """Mark every executing key indeterminate; return how many. Only for a
    process that has established no other process is serving the database."""
    with db.transaction(conn, immediate=True):
        return conn.execute(
            "UPDATE idempotency_keys SET state = 'indeterminate', "
            "owner_token = NULL, lease_expires_at = NULL, failure_code = ?, "
            "updated_at = datetime('now') WHERE state = 'executing'",
            (idempotency.OWNER_LOST,),
        ).rowcount


def resolve(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    row_id: int,
    key: str,
    outcome: str,
) -> dict:
    """Settle one key whose outcome is unknown, and record it on the trail.

    ``row_id`` and ``key`` must both name the row: an id alone could be a row
    that replaced the one the admin was shown. Raises
    :class:`KeyNotIndeterminate` when no such key is waiting, which is also
    the answer to a second resolution of the same key.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of: {', '.join(OUTCOMES)}")
    with db.transaction(conn, immediate=True):
        row = idempotency.get_indeterminate(conn, row_id=row_id)
        if row is None or row["idempotency_key"] != key:
            raise KeyNotIndeterminate(row_id)
        if outcome == NOT_APPLIED:
            conn.execute("DELETE FROM idempotency_keys WHERE rowid = ?", (row_id,))
        else:
            conn.execute(
                "UPDATE idempotency_keys SET state = 'indeterminate', "
                "owner_token = NULL, lease_expires_at = NULL, failure_code = ?, "
                "updated_at = datetime('now') WHERE rowid = ?",
                (idempotency.RESOLVED_APPLIED, row_id),
            )
        sender = idempotency.sender(conn, row["identity"])
        event = activity.record(
            conn,
            actor_id=int(actor["id"]),
            verb=VERB_RESOLVED_KEY,
            # The account whose request it was; the admin's own when that
            # account or its token no longer exists.
            target_kind="user",
            target_id=int(sender["user_id"] if sender else actor["id"]),
            detail=f"{outcome}: {row['method']} {row['path']} (key {key})",
            commit=False,
        )
    return {
        "id": row_id,
        "key": key,
        "method": row["method"],
        "path": row["path"],
        "outcome": outcome,
        "event_id": int(event["id"]),
    }
