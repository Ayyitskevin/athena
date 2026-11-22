"""Audited budget ceiling writes.

``core/budgets.py`` still owns the charge and the reads. Setting or clearing a
ceiling is the operator's write, so it lives in a command module the transports
call directly; so does recording an exhaustion, which the app's refusal handler
writes after the refused command rolled back.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db
from athena.core._util import utc_now
from athena.core.budgets import (
    VERB_BUDGET_CLEARED,
    VERB_BUDGET_EXHAUSTED,
    VERB_BUDGET_SET,
    WINDOWS,
    Budget,
    stamp_window,
    get_budget,
)


class BudgetCommandError(Exception):
    """A budget write was refused. ``kind`` is the closed vocabulary adapters map
    to a status (today only ``forbidden``); the command knows no transport."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _refuse_own_budget(actor_id: int, target_user_id: int) -> None:
    # D13: a budget bounds the account that holds it, so that account cannot move
    # it. Checked before the no-op paths, so even an idempotent self-write refuses.
    if actor_id == target_user_id:
        raise BudgetCommandError("forbidden", "you cannot change your own budget")


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

    The caller enforces admin; this command refuses with ``forbidden`` when the
    target is the actor, whatever the actor's role (D13). Raises ValueError for an
    unknown window or a negative limit — the boundary turns that into a 422.
    """
    if window not in WINDOWS:
        raise ValueError(f"window must be one of: {', '.join(sorted(WINDOWS))}")
    if not isinstance(action_limit, int) or isinstance(action_limit, bool):
        raise ValueError("action_limit must be an integer")
    if action_limit < 0:
        raise ValueError("action_limit must not be negative")
    _refuse_own_budget(actor_id, target_user_id)
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
                (
                    target_user_id,
                    window,
                    action_limit,
                    stamp_window(utc_now()),
                    actor_id,
                ),
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
    Returns False when there was nothing to clear — no event for a no-op. Refuses
    with ``forbidden`` when the target is the actor (D13)."""
    _refuse_own_budget(actor_id, target_user_id)
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
        commit=True,
    )
