"""Audit the FAILURES — an actor probing its boundary now leaves a trace.

Success has always been first-class history here; refusals were invisible. A
failed login against a real account, a revoked token still being presented, a
scope-capped agent probing past its grant, a paused account that keeps trying —
each is exactly the signal an operator needs BEFORE something goes wrong, and
each now lands on the activity trail as an ordinary event (feed-filterable,
CSV-exportable, run-attributable like everything else).

Two deliberate rules:

- Attribution is to the ACCOUNT INVOLVED, best available truth: the user whose
  password was guessed at, the owner of the revoked token presented, the actor
  whose scope ran out. The event records that the boundary of that identity was
  hit — the detail says how.
- Recording is BEST-EFFORT: a failure event must never turn a clean 401/403
  into a 500, so recorders swallow (and log) their own errors. Success events
  keep their strict atomic contract in activity.record; only refusals trade
  strictness for availability. Every recording path sits behind a rate limiter
  (login/token/anon), so a hostile flood is bounded before it reaches the trail.
"""

from __future__ import annotations

import logging
import sqlite3

from athena.core import activity

_logger = logging.getLogger("athena")

VERB_LOGIN_FAILED = "login_failed"
VERB_REVOKED_TOKEN_USED = "revoked_token_used"
VERB_SCOPE_DENIED = "scope_denied"
VERB_PAUSED_REFUSED = "paused_account_refused"


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
        )
    except Exception:  # noqa: BLE001 — the refusal must go out regardless
        _logger.exception("could not record auth failure event (%s)", verb)
