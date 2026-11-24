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

**Two counters, one window (D6).** The action counter meters the writes that
change the work. The coordination counter meters the writes about the work: a
comment, a claim, a delegation. They are counted apart so that a seat out of
action budget can still say so and hand the work back, and a seat that talks too
much keeps the writes that do its work. The coordination ceiling is optional on
a budget row: unset, coordination is not limited. Lease renewals, yields,
completions and releases are never metered: the way out of a claim must stay
open.

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

from athena.core._util import utc_now

WINDOWS: dict[str, timedelta] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_BUDGET_SET = "agent_budget_set"
VERB_BUDGET_CLEARED = "agent_budget_cleared"
VERB_BUDGET_EXHAUSTED = "agent_budget_exhausted"
VERB_COORDINATION_EXHAUSTED = "agent_coordination_budget_exhausted"
#: Both exhaustions, for the readers that count "a ceiling was hit".
EXHAUSTION_VERBS = (VERB_BUDGET_EXHAUSTED, VERB_COORDINATION_EXHAUSTED)

# The stable machine-readable code every transport echoes for a refusal, so an
# agent can branch on it instead of parsing prose.
BUDGET_EXHAUSTED_CODE = "agent_budget_exhausted"
COORDINATION_EXHAUSTED_CODE = "agent_coordination_budget_exhausted"

#: The two counters. ``ACTION`` is the default everywhere, so a caller written
#: before D6 meters what it always did.
ACTION = "action"
COORDINATION = "coordination"

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
    #: None: coordination writes are not limited on this budget.
    coordination_limit: int | None = None
    coordination_used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.action_limit - self.action_used)

    @property
    def coordination_remaining(self) -> int | None:
        if self.coordination_limit is None:
            return None
        return max(0, self.coordination_limit - self.coordination_used)

    def public(self) -> dict:
        """The shape every surface returns — no internal columns."""
        return {
            "user_id": self.user_id,
            "window": self.window,
            "action_limit": self.action_limit,
            "action_used": self.action_used,
            "remaining": self.remaining,
            "window_started_at": self.window_started_at,
            "coordination_limit": self.coordination_limit,
            "coordination_used": self.coordination_used,
            "coordination_remaining": self.coordination_remaining,
        }


class BudgetExhausted(Exception):
    """A metered write was refused because the actor's budget is spent.

    Transport-neutral: adapters map it to 429 with ``BUDGET_EXHAUSTED_CODE`` and a
    ``Retry-After`` derived from ``retry_after_seconds``. Raised INSIDE the
    command's transaction, so the refused write rolls back whole — the charge and
    the mutation are never half-applied.
    """

    def __init__(
        self, budget: Budget, *, retry_after_seconds: int, counter: str = ACTION
    ) -> None:
        super().__init__(
            f"agent coordination budget exhausted: {budget.coordination_used}/"
            f"{budget.coordination_limit} comments, claims and delegations used "
            f"this {budget.window}"
            if counter == COORDINATION
            else f"agent budget exhausted: {budget.action_used}/{budget.action_limit} "
            f"metered actions used this {budget.window}"
        )
        self.budget = budget
        self.retry_after_seconds = retry_after_seconds
        #: Which ceiling refused: ``ACTION`` or ``COORDINATION``.
        self.counter = counter


def stamp_window(moment) -> str:
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
        coordination_limit=row["coordination_limit"],
        coordination_used=row["coordination_used"],
    )


def get_budget(conn: sqlite3.Connection, user_id: int) -> Budget | None:
    """This user's budget, or None when unbudgeted (the default = unlimited)."""
    row = conn.execute(
        "SELECT * FROM agent_budgets WHERE user_id = ?", (user_id,)
    ).fetchone()
    return _row_to_budget(row) if row else None


def parse_window_start(budget: Budget):
    """When ``budget``'s stored window began, as an aware datetime."""
    return _parse(budget.window_started_at)


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
        window_started_at=stamp_window(now),
        set_by=budget.set_by,
        coordination_limit=budget.coordination_limit,
        coordination_used=0,
    )


def observed(conn: sqlite3.Connection, user_id: int) -> Budget | None:
    """The budget a reader should see: the stored row rolled forward to now, so a
    stale window never displays as spent. Read-only — an expired window is
    persisted by the next charge, not by looking at it."""
    budget = get_budget(conn, user_id)
    return None if budget is None else _rolled(budget, utc_now())


def charge(
    conn: sqlite3.Connection,
    actor: dict | None,
    *,
    cost: int = 1,
    counter: str = ACTION,
) -> None:
    """Consume ``cost`` from one of ``actor``'s two counters, or refuse.

    ``counter`` is ``ACTION`` (a write that changes the work; the default) or
    ``COORDINATION`` (a comment, a claim, a delegation). Call this INSIDE the
    metered command's ``db.transaction(immediate=True)``: the read-check-write
    runs under SQLite's writer reservation, so two concurrent writes cannot both
    spend the last unit, and a later failure in the same command rolls the
    charge back with the mutation.

    No-ops for an unbudgeted actor (the default), and for a coordination write
    on a budget with no coordination ceiling, so metering is strictly opt-in.
    A charge that finds the stored window over stores the fresh window with
    both counters reset. Raises :class:`BudgetExhausted` when the counter's
    limit is already reached; the caller records the refusal after the rollback
    (see ``budget_commands.record_exhaustion``).
    """
    if counter not in (ACTION, COORDINATION):
        raise ValueError(f"unknown budget counter: {counter}")
    if actor is None:
        return
    budget = get_budget(conn, actor["id"])
    if budget is None:
        return
    if counter == COORDINATION and budget.coordination_limit is None:
        return
    now = utc_now()
    budget = _rolled(budget, now)
    used, limit = (
        (budget.coordination_used, budget.coordination_limit)
        if counter == COORDINATION
        else (budget.action_used, budget.action_limit)
    )
    assert limit is not None
    if used + cost > limit:
        window_ends = _parse(budget.window_started_at) + WINDOWS[budget.window]
        retry_after = max(1, int((window_ends - now).total_seconds()))
        raise BudgetExhausted(budget, retry_after_seconds=retry_after, counter=counter)
    action_used = budget.action_used + (cost if counter == ACTION else 0)
    coordination_used = budget.coordination_used + (
        cost if counter == COORDINATION else 0
    )
    conn.execute(
        "UPDATE agent_budgets SET action_used = ?, coordination_used = ?, "
        "window_started_at = ?, updated_at = datetime('now') WHERE user_id = ?",
        (action_used, coordination_used, budget.window_started_at, actor["id"]),
    )
