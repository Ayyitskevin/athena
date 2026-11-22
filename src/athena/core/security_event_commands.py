"""Record a refusal on the trail: the one writer for security failure events.

``core/security_events.py`` owns the verbs and the reads. Recording is
BEST-EFFORT (that module's docstring says why): a failure event must never
turn a clean 401/403 into a 500, so this swallows and logs its own errors. The
authentication dependency, the browser login flow and the app's scope-denial
handler all record through it.
"""

from __future__ import annotations

import logging
import sqlite3

from athena.core import activity

_logger = logging.getLogger("athena")


def record_failure(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    verb: str,
    target_kind: str,
    target_id: int,
    detail: str = "",
) -> None:
    """Append one failure event, never letting the attempt block the refusal."""
    try:
        activity.record(
            conn,
            actor_id=actor_id,
            verb=verb,
            target_kind=target_kind,
            target_id=target_id,
            detail=detail,
            commit=True,
        )
    except Exception:  # noqa: BLE001 — the refusal must go out regardless
        _logger.exception("could not record auth failure event (%s)", verb)
