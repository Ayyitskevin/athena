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
  strictness for availability. Every recording path is flood-bounded before it
  reaches the trail: the login limiter (browser login), the per-token limiter
  (bearer paths), or the anon limiter — either raising (invalid bearer) or a
  non-raising gate (the trusted-header paused refusal, whose 403 is unchanged).

The one residual asymmetry is deliberate and documented: presenting a REVOKED
token records an event while presenting a never-a-token string records none, so
the revoked path is marginally slower. Distinguishing "a revoked token" from
"random bytes" requires already holding a real revoked token's exact value, so
the signal is near-worthless — and the recording is anon-limiter-bounded. The
higher-value login oracle (which must stay existence-opaque) is closed instead
by recording login failures on a response background task, off the measured path.
"""

from __future__ import annotations

import logging
import sqlite3

from athena.core import activity

_logger = logging.getLogger("athena")

VERB_LOGIN_FAILED = "login_failed"
VERB_LOGIN_THROTTLED = "login_throttled"
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


# The refusal verbs, as a closed set: a surface that lists "security signals" must
# not silently widen into every verb that happens to sound alarming. A login that was
# THROTTLED is a different fact from one that was wrong — the first says someone is
# working through a list at one address, the second that a single guess missed — so it
# gets its own verb rather than inflating the failed-login count.
SECURITY_VERBS: tuple[str, ...] = (
    VERB_LOGIN_FAILED,
    VERB_LOGIN_THROTTLED,
    VERB_REVOKED_TOKEN_USED,
    VERB_SCOPE_DENIED,
    VERB_PAUSED_REFUSED,
)

MAX_LIST_LIMIT = 200


def list_failures(
    conn: sqlite3.Connection,
    *,
    verb: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Recent boundary refusals, newest first.

    These events were always on the trail and always filterable — but only by an
    operator who already knew the four verb names and thought to look. Probing
    before compromise is exactly the signal that must not require knowing to grep,
    so this is the query the cockpit and MCP ask.

    Reads are UNGATED because every caller is an admin (the routes enforce it) and
    a refusal names the account whose boundary was hit, which is the fact an
    operator needs. `verb` is checked against the closed set rather than passed
    through, so this cannot become a general activity reader with a different name.

    Imported rows are excluded: a hostile import bundle could back-date security
    verbs into the window and plant fake refusals on /admin/security. Foreign
    history is something Athena was told, not a boundary it enforced. The row
    still SELECTs its imported_at so a future surface could label such rows
    instead of hiding them.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be positive")
    bounded = min(limit, MAX_LIST_LIMIT)
    verbs = SECURITY_VERBS
    if verb is not None:
        if verb not in SECURITY_VERBS:
            raise ValueError(f"verb must be one of: {', '.join(SECURITY_VERBS)}")
        verbs = (verb,)
    clauses = [
        f"a.verb IN ({','.join('?' * len(verbs))})",
        "a.imported_at IS NULL",
    ]
    params: list[object] = list(verbs)
    if since is not None:
        # Compared as text against the stored 'YYYY-MM-DD HH:MM:SS' server clock,
        # the same shape every other activity read uses.
        clauses.append("a.created_at >= ?")
        params.append(since)
    params.append(bounded)
    rows = conn.execute(
        "SELECT a.id, a.actor_id, a.verb, a.target_kind, a.target_id, a.detail, "
        "a.created_at, a.imported_at, u.name AS actor_name, u.email AS actor_email, "
        "u.is_agent AS actor_is_agent "
        "FROM activity a JOIN users u ON u.id = a.actor_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY a.id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def failure_counts(
    conn: sqlite3.Connection, *, since: str | None = None
) -> dict[str, int]:
    """How many of each refusal, for the attention rollup. Zero-filled, so a quiet
    fleet reads as an explicit zero rather than a missing key. Imported rows are
    excluded (same rule as list_failures): a back-dated import must not inflate
    the counts an operator steers by."""
    clauses = [
        f"verb IN ({','.join('?' * len(SECURITY_VERBS))})",
        "imported_at IS NULL",
    ]
    params: list[object] = list(SECURITY_VERBS)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    rows = conn.execute(
        f"SELECT verb, COUNT(*) AS n FROM activity WHERE {' AND '.join(clauses)} "
        "GROUP BY verb",
        params,
    ).fetchall()
    counts = {verb: 0 for verb in SECURITY_VERBS}
    for row in rows:
        counts[row["verb"]] = int(row["n"])
    return counts
