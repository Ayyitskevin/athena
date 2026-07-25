"""Durable per-agent action budgets — the bounded half of the operator's loop.

`VISION.md` promises that every agent action is "attributable, reversible, and
**bounded**". Attribution is the activity trail; bounding, until now, was the
per-token rate limiter in `rate_limits.py` — which lives in process memory and
forgets everything on restart. That bounds a burst; it is not a budget. This
module is the durable ceiling: an operator sets how many metered writes an agent
may make per fixed window, and the metered commands decrement it *inside their own
write transaction*, so a mutation and its charge commit or roll back together.

Three design decisions worth stating plainly, because each rules something out:

**Actions, not tokens or dollars.** Athena is the control plane; the agent's model
spend happens in the agent's own process, which Athena never observes. A cost
column here would be a number Athena could not honestly populate, so there isn't
one. If an external meter ever reports spend, it can be added additively.

**Opt-in.** No row means unlimited, mirroring the blocked-close policy: a fresh
deploy behaves exactly as before until a human admin sets a ceiling. That also
means in-process actors (the automation engine's system user) stay unbudgeted
unless an operator deliberately budgets them.

**Fixed windows.** `window_started_at` anchors the period; the first charge at or
after its end resets the counter and re-anchors. A fixed window permits a burst
across the boundary (up to 2x the limit within one window-length). That is the
accepted trade-off for a counter an operator can reason about — and one that needs
no background job, honoring "one operator, zero ops".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import sqlite3

from athena.core import activity, db
from athena.core._util import utc_now

WINDOWS: dict[str, timedelta] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_BUDGET_SET = "agent_budget_set"
VERB_BUDGET_CLEARED = "agent_budget_cleared"
VERB_BUDGET_EXHAUSTED = "agent_budget_exhausted"

# The stable machine-readable code every transport echoes for a refusal, so an
# agent can branch on it instead of parsing prose.
BUDGET_EXHAUSTED_CODE = "agent_budget_exhausted"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Budget:
    """One agent's durable ceiling and its current consumption."""

    user_id: int
    window: str
    action_limit: int
    action_used: int
    window_started_at: str
    set_by: int | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.action_limit - self.action_used)

    def public(self) -> dict:
        """The shape every surface returns — no internal columns."""
        return {
            "user_id": self.user_id,
            "window": self.window,
            "action_limit": self.action_limit,
            "action_used": self.action_used,
            "remaining": self.remaining,
            "window_started_at": self.window_started_at,
        }


class BudgetExhausted(Exception):
    """A metered write was refused because the actor's budget is spent.

    Transport-neutral: adapters map it to 429 with ``BUDGET_EXHAUSTED_CODE`` and a
    ``Retry-After`` derived from ``retry_after_seconds``. Raised INSIDE the
    command's transaction, so the refused write rolls back whole — the charge and
    the mutation are never half-applied.
    """

    def __init__(self, budget: Budget, *, retry_after_seconds: int) -> None:
        super().__init__(
            f"agent budget exhausted: {budget.action_used}/{budget.action_limit} "
            f"metered actions used this {budget.window}"
        )
        self.budget = budget
        self.retry_after_seconds = retry_after_seconds


def _stamp(moment) -> str:
    return moment.strftime(_TS_FMT)


def _parse(value: str):
    from datetime import datetime, timezone

    return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)


def _row_to_budget(row: sqlite3.Row) -> Budget:
    return Budget(
        user_id=row["user_id"],
        window=row["window"],
        action_limit=row["action_limit"],
        action_used=row["action_used"],
        window_started_at=row["window_started_at"],
        set_by=row["set_by"],
    )


def get_budget(conn: sqlite3.Connection, user_id: int) -> Budget | None:
    """This user's budget, or None when unbudgeted (the default = unlimited)."""
    row = conn.execute(
        "SELECT * FROM agent_budgets WHERE user_id = ?", (user_id,)
    ).fetchone()
    return _row_to_budget(row) if row else None


def _rolled(budget: Budget, now) -> Budget:
    """The budget as of ``now``: unchanged, or reset into a fresh window.

    Pure — it does not write. Callers that need the reset persisted pass through
    :func:`charge`, which writes the rolled state with the charge in one statement.
    """
    started = _parse(budget.window_started_at)
    if now - started < WINDOWS[budget.window]:
        return budget
    return Budget(
        user_id=budget.user_id,
        window=budget.window,
        action_limit=budget.action_limit,
        action_used=0,
        window_started_at=_stamp(now),
        set_by=budget.set_by,
    )


def observed(conn: sqlite3.Connection, user_id: int) -> Budget | None:
    """The budget a reader should see: the stored row rolled forward to now, so a
    stale window never displays as spent. Read-only — an expired window is
    persisted by the next charge, not by looking at it."""
    budget = get_budget(conn, user_id)
    return None if budget is None else _rolled(budget, utc_now())


def charge(conn: sqlite3.Connection, actor: dict | None, *, cost: int = 1) -> None:
    """Consume ``cost`` metered actions from ``actor``'s budget, or refuse.

    Call this INSIDE the metered command's ``db.transaction(immediate=True)``: the
    read-check-write runs under SQLite's writer reservation, so two concurrent
    writes cannot both spend the last unit, and a later failure in the same command
    rolls the charge back with the mutation.

    No-ops for an unbudgeted actor (the default), so metering is strictly opt-in.
    Raises :class:`BudgetExhausted` when the window's limit is already reached; the
    caller records the refusal after the rollback (see :func:`record_exhaustion`).
    """
    if actor is None:
        return
    budget = get_budget(conn, actor["id"])
    if budget is None:
        return
    now = utc_now()
    budget = _rolled(budget, now)
    if budget.action_used + cost > budget.action_limit:
        window_ends = _parse(budget.window_started_at) + WINDOWS[budget.window]
        retry_after = max(1, int((window_ends - now).total_seconds()))
        raise BudgetExhausted(budget, retry_after_seconds=retry_after)
    conn.execute(
        "UPDATE agent_budgets SET action_used = ?, window_started_at = ?, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (budget.action_used + cost, budget.window_started_at, actor["id"]),
    )


def record_exhaustion(conn: sqlite3.Connection, *, actor_id: int, detail: str) -> None:
    """Put a refused write on the trail, AFTER its transaction rolled back.

    The refusal is exactly the "steer by exception" signal the operator wants — an
    agent hitting its ceiling is a decision to make, not noise. It cannot be
    recorded inside the command, because that transaction is unwinding; this runs
    on the now-free connection in its own transaction. Best-effort by construction:
    the write was already refused, so a failure to log must not mask that.
    """
    activity.record(
        conn,
        actor_id=actor_id,
        verb=VERB_BUDGET_EXHAUSTED,
        target_kind="user",
        target_id=actor_id,
        detail=detail,
    )


def set_budget(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    target_user_id: int,
    window: str,
    action_limit: int,
) -> Budget:
    """Create or replace a user's budget atomically with its audit event.

    Changing the ceiling preserves consumption and the current window: raising a
    limit mid-window releases the agent immediately without gifting it a fresh
    window, and lowering it can leave the agent already over (``remaining`` clamps
    to 0). Re-setting the identical ceiling is a no-op that records nothing.

    Authorization is the caller's job (the command/route enforces admin), matching
    how the neighbouring core commands split it. Raises ValueError for an unknown
    window or a negative limit — the boundary turns that into a 422.
    """
    if window not in WINDOWS:
        raise ValueError(f"window must be one of: {', '.join(sorted(WINDOWS))}")
    if not isinstance(action_limit, int) or isinstance(action_limit, bool):
        raise ValueError("action_limit must be an integer")
    if action_limit < 0:
        raise ValueError("action_limit must not be negative")
    with db.transaction(conn, immediate=True):
        before = get_budget(conn, target_user_id)
        if (
            before is not None
            and before.window == window
            and before.action_limit == action_limit
        ):
            return before
        if before is None:
            conn.execute(
                "INSERT INTO agent_budgets "
                "(user_id, window, action_limit, action_used, window_started_at, set_by) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (target_user_id, window, action_limit, _stamp(utc_now()), actor_id),
            )
        else:
            # Preserve action_used and the window anchor: a limit change is not a
            # fresh allowance.
            conn.execute(
                "UPDATE agent_budgets SET window = ?, action_limit = ?, set_by = ?, "
                "updated_at = datetime('now') WHERE user_id = ?",
                (window, action_limit, actor_id, target_user_id),
            )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_BUDGET_SET,
            target_kind="user",
            target_id=target_user_id,
            detail=f"{action_limit} actions per {window}",
            commit=False,
        )
        updated = get_budget(conn, target_user_id)
        assert updated is not None
        return updated


def clear_budget(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int
) -> bool:
    """Remove a user's budget (back to unlimited) atomically with its audit event.
    Returns False when there was nothing to clear — no event for a no-op."""
    with db.transaction(conn, immediate=True):
        if get_budget(conn, target_user_id) is None:
            return False
        conn.execute("DELETE FROM agent_budgets WHERE user_id = ?", (target_user_id,))
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_BUDGET_CLEARED,
            target_kind="user",
            target_id=target_user_id,
            commit=False,
        )
        return True
