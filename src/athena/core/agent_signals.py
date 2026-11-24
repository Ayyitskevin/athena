"""What an agent account was last refused, and when it last found nothing ready.

A refusal is an answer, not an event: nothing was done, so the trail holds
nothing, and an operator looking at an idle agent could not tell an agent
refused on every call from one with no work from one that is not running
(F15). Migration 0091 keeps the newest of each per agent account, replaced in
place. This module reads them; ``agent_signal_commands`` writes them.

Both are diagnosis, not record. They are outside the activity trail and its
hash chain, they keep no history, and an agent's own calls move them, so
nothing may be decided from them.
"""

from __future__ import annotations

import sqlite3

from athena.core import packet_time

#: The longest request line kept with a refusal: a method and a path. A longer
#: path is cut, never refused; the code and the time are the facts.
MAX_REQUEST_CHARS = 300


def last_signals(conn: sqlite3.Connection, user_ids: list[int]) -> dict[int, dict]:
    """The stored signals of these accounts, by user id. An account with no row
    reads as never refused and never told there was nothing ready."""
    signals: dict[int, dict] = {
        int(user_id): {"last_refusal": None, "last_nothing_ready_at": None}
        for user_id in user_ids
    }
    if not signals:
        return signals
    placeholders = ",".join("?" for _ in signals)
    for row in conn.execute(
        "SELECT user_id, last_refusal_code, last_refusal_status, "
        "last_refusal_request, last_refusal_at, last_nothing_ready_at "
        f"FROM agent_last_signals WHERE user_id IN ({placeholders})",
        list(signals),
    ):
        signals[int(row["user_id"])] = {
            "last_refusal": None
            if row["last_refusal_code"] is None
            else {
                "code": row["last_refusal_code"],
                "status_code": row["last_refusal_status"],
                "request": row["last_refusal_request"],
                "at": packet_time.rfc3339(row["last_refusal_at"]),
            },
            "last_nothing_ready_at": packet_time.rfc3339(row["last_nothing_ready_at"]),
        }
    return signals
