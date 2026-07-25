"""One rollup of everything asking for the operator's attention.

Athena grew its exception surfaces one at a time, and each landed on its own page:
claims needing attention in Mission Control, failing automation rules beside them,
failing webhooks somewhere else, and — before this — refused logins nowhere at
all, pending approvals only on the agents page, and unanswered kill requests only
on the worker list. An operator who is supposed to *steer by exception* had to
already know all six places to look.

This is the one number-per-thing card, and it is deliberately only that: counts
plus the link to the surface that owns each one. It computes no state of its own,
so it cannot disagree with the pages it points at.

**Every count says what it counts.** `needs_attention` claims are the ones the
bounded active-work window examined, not a fleet-wide total (see
`fleet_work.build_active_work`). Security refusals and budget exhaustions are
counted over a stated recent window, because "someone probed the boundary once,
months ago" is not the same alarm as "someone is probing now".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3

from athena.aegis import automation, fleet_work
from athena.core import approvals, budgets, security_events, webhooks, workers

SCHEMA = "athena.fleet_attention.v1"

#: How far back the event-counted signals look. A day is long enough to catch an
#: overnight probe and short enough that a stale number never reads as a live one.
DEFAULT_WINDOW_HOURS = 24

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _since_text(*, window_hours: int, now: datetime | None) -> str:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current - timedelta(hours=window_hours)).strftime(_TS_FORMAT)


def build_attention(
    conn: sqlite3.Connection,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
) -> dict:
    """Count what needs a human, and say where each number lives.

    Admin-only by the caller's contract: every input here is already an
    admin-scoped read (active work, automation rules, webhook health, the approval
    queue, the worker registry, refusal events), and aggregating them does not
    widen any of them.
    """
    if isinstance(window_hours, bool) or not isinstance(window_hours, int):
        raise ValueError("window_hours must be an integer")
    if window_hours < 1:
        raise ValueError("window_hours must be positive")
    since = _since_text(window_hours=window_hours, now=now)

    # Ask the active-work projection for exactly the attention rows, so this card
    # and Mission Control's filtered view are the same query with the same bounds.
    attention_work = fleet_work.build_active_work(
        conn, attention_state=fleet_work.NEEDS_ATTENTION, now=now
    )
    failing_rules = automation.list_rules(conn, failing_only=True)
    failing_webhooks = [
        hook for hook in webhooks.list_webhooks(conn) if hook["failure_count"]
    ]
    pending_approvals = approvals.list_requests(conn, state="pending", limit=200)
    unanswered_kills = [
        worker
        for worker in workers.list_workers(conn, limit=workers.MAX_LIST_LIMIT)
        if worker["kill_state"] in (workers.KILL_REQUESTED, workers.KILL_DEFIED)
    ]
    refusals = security_events.failure_counts(conn, since=since)
    budget_exhaustions = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM activity WHERE verb = ? AND created_at >= ?",
            (budgets.VERB_BUDGET_EXHAUSTED, since),
        ).fetchone()["n"]
    )

    signals = [
        {
            "key": "claims_needing_attention",
            "label": "Claims needing attention",
            "count": len(attention_work["items"]),
            "href": "/admin/agents/runs?attention_state=needs_attention",
            # This one is window-bounded twice over: by the active-work limit and
            # by what the projection examined. Saying so here keeps the card from
            # implying a fleet-wide total it never computed.
            "scope": (
                f"of {attention_work['examined_count']} claims examined"
                + (" (more exist)" if attention_work["clipped"] else "")
            ),
        },
        {
            "key": "pending_approvals",
            "label": "Approvals waiting on you",
            "count": len(pending_approvals),
            "href": "/admin/agents",
            "scope": "open requests",
        },
        {
            "key": "unanswered_kills",
            "label": "Workers told to stop",
            "count": len(unanswered_kills),
            "href": "/admin/agents",
            "scope": "asked, not yet confirmed",
        },
        {
            "key": "failing_automation_rules",
            "label": "Failing automation rules",
            "count": len(failing_rules),
            "href": "/admin/automation",
            "scope": "rules with a recorded failure",
        },
        {
            "key": "failing_webhooks",
            "label": "Failing webhooks",
            "count": len(failing_webhooks),
            "href": "/admin/webhooks",
            "scope": "endpoints with a delivery failure",
        },
        {
            "key": "budget_exhaustions",
            "label": "Budget ceilings hit",
            "count": budget_exhaustions,
            "href": "/admin/agents",
            "scope": f"in the last {window_hours}h",
        },
        {
            "key": "security_refusals",
            "label": "Boundary refusals",
            "count": sum(refusals.values()),
            "href": "/admin/security",
            "scope": f"in the last {window_hours}h",
        },
    ]
    return {
        "schema": SCHEMA,
        "window_hours": window_hours,
        "since": since,
        "signals": signals,
        "total": sum(signal["count"] for signal in signals),
        # Kept apart from the rolled-up total so the card can break the refusals
        # down without a second query.
        "refusals_by_verb": refusals,
    }
