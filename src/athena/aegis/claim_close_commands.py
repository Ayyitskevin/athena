"""Close the issue in the same call that releases its lease (AD13).

A seat's last two calls on a slice were always ``complete_claim`` and then
``update_issue(status=...)``. ``complete_claim`` now takes an optional
``close_status`` and makes the second call for it, through the same command
(:func:`issue_commands.update_issue`), so every gate on a status change still
applies: visibility, the blocked-close policy, the durable budget and approval.

The two acts stay two acts. The release commits first and is never undone by a
refused close. The answer says what happened to each: the release in its usual
fields, the close under ``close``, with ``applied`` false and the refusal's code
when it did not go through. A status the issue's project does not offer is
refused before anything is released, because that is a mistake in the call,
not a gate.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issue_commands
from athena.aegis import statuses
from athena.core import approval_commands, approvals, budgets, run_context


def check_offered(
    conn: sqlite3.Connection, *, actor: dict, issue_id: int, status: object
) -> None:
    """Refuse a close status the issue's project does not offer, before the
    release: the same refusal (and list of offered names) ``update_issue``
    gives. An issue the actor cannot see is left to the release's own 404."""
    issue = issue_commands.visible_issue_or_none(conn, actor, issue_id)
    if issue is None:
        return
    if not isinstance(status, str):
        raise issue_commands.IssueCommandError(
            "invalid", "close_status must be a string"
        )
    if not statuses.is_valid(conn, issue["project_id"], status):
        raise issue_commands.unknown_status(conn, issue["project_id"])


def _not_applied(status: str, code: str, detail: str, **extra: object) -> dict:
    # ``detail`` is built from the refusal's fields, never str() of an
    # exception, so nothing but these fixed sentences reaches the answer.
    return {
        "status": status,
        "applied": False,
        "code": code,
        "detail": f"{detail}. The lease is released; the issue was not changed.",
        **extra,
    }


def close_after_release(
    conn: sqlite3.Connection, *, actor: dict, issue_id: int, status: str, released: dict
) -> dict:
    """``released`` (``complete_claim``'s answer) with the close applied, or
    with ``close.applied`` false and why. Runs after the release committed."""
    retry = f"update_issue with status {status!r}"
    try:
        updated = issue_commands.update_issue(
            conn, actor=actor, issue_id=issue_id, status=status
        )
    except approvals.ApprovalRequired as exc:
        # The refused close rolled back whole; record the ask on the freed
        # connection, as the app's handler does for any gated write.
        asked = approval_commands.open_request(
            conn,
            actor_id=exc.actor_id,
            action_kind=exc.action_kind,
            target_kind=exc.target_kind,
            target_id=exc.target_id,
            run_id=run_context.get_run_id(),
        )
        close = _not_applied(
            status,
            approvals.APPROVAL_REQUIRED_CODE,
            f"{exc.action_kind} requires operator approval",
            approval=asked.public(),
            next=f"once the operator approves, {retry}",
        )
    except approvals.ApprovalRejected as exc:
        close = _not_applied(
            status,
            approvals.APPROVAL_REJECTED_CODE,
            f"{exc.request.action_kind} was rejected by the operator",
            approval=exc.request.public(),
        )
    except budgets.BudgetExhausted as exc:
        close = _not_applied(
            status,
            budgets.BUDGET_EXHAUSTED_CODE,
            "the agent budget is exhausted for this window",
            retry_after_seconds=exc.retry_after_seconds,
            next=f"after {exc.retry_after_seconds}s, {retry}",
        )
    except issue_commands.IssueCommandError as exc:
        close = _not_applied(status, exc.code or exc.kind, exc.detail)
    else:
        still_open = (
            statuses.category_of(conn, updated.get("project_id"), updated["status"])
            != "done"
        )
        return {
            **released,
            "issue_status": updated["status"],
            "issue_still_open": still_open,
            "next": f"released the lease and set the issue to {updated['status']}",
            "close": {"status": updated["status"], "applied": True},
        }
    return {**released, "close": close}
