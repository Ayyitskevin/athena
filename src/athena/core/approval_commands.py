"""Audited approval decisions and policy writes.

Split out of ``core/approvals.py``, which still owns the gate, the request
rows, and the reads. These three functions are the durable writes: each one
commits the row change and its activity event in one transaction, and refuses
with a transport-neutral kind rather than an HTTP status.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db
from athena.core.approvals import (
    ACTION_KINDS,
    VERB_APPROVED,
    VERB_POLICY_CLEARED,
    VERB_POLICY_SET,
    VERB_REJECTED,
    ApprovalRequest,
    get_request,
    is_gated,
)


class ApprovalDecisionError(Exception):
    """A decision or policy write could not be applied.

    ``kind`` is the closed vocabulary adapters map to a status. The command
    does not know which transport is asking.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def decide(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    request_id: int,
    decision: str,
    note: str | None = None,
) -> ApprovalRequest:
    """Approve or reject a pending request atomically with its audit event.

    Only a *pending* request can be decided: re-deciding a settled one raises
    rather than silently flipping an answer the agent may already have acted on.
    Authorization (admin, and human-only) is the caller's job — the route enforces
    it, mirroring the other core commands.
    """
    if decision not in ("approve", "reject"):
        raise ApprovalDecisionError("invalid", "decision must be 'approve' or 'reject'")
    with db.transaction(conn, immediate=True):
        request = get_request(conn, request_id)
        if request is None:
            raise ApprovalDecisionError("not_found", "no such approval request")
        if request.state != "pending":
            raise ApprovalDecisionError(
                "conflict", f"approval request is already {request.state}"
            )
        state = "approved" if decision == "approve" else "rejected"
        conn.execute(
            "UPDATE approval_requests SET state = ?, decided_by = ?, "
            "decided_at = datetime('now'), decision_note = ? WHERE id = ?",
            (state, actor_id, note, request_id),
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_APPROVED if state == "approved" else VERB_REJECTED,
            target_kind=request.target_kind,
            target_id=request.target_id,
            detail=f"{request.action_kind} (approval #{request_id})",
            commit=False,
        )
        decided = get_request(conn, request_id)
        assert decided is not None
        return decided


def set_policy(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int, action_kind: str
) -> bool:
    """Gate an action kind for a user, atomically with its audit event. Returns
    False when already gated (idempotent, records nothing). Raises ValueError for
    an unknown action kind — the vocabulary is closed on purpose."""
    if action_kind not in ACTION_KINDS:
        raise ValueError(
            f"action_kind must be one of: {', '.join(sorted(ACTION_KINDS))}"
        )
    with db.transaction(conn, immediate=True):
        if is_gated(conn, target_user_id, action_kind):
            return False
        conn.execute(
            "INSERT INTO agent_approval_policies (user_id, action_kind, set_by) "
            "VALUES (?, ?, ?)",
            (target_user_id, action_kind, actor_id),
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_POLICY_SET,
            target_kind="user",
            target_id=target_user_id,
            detail=action_kind,
            commit=False,
        )
        return True


def clear_policy(
    conn: sqlite3.Connection, *, actor_id: int, target_user_id: int, action_kind: str
) -> bool:
    """Ungate an action kind for a user, atomically with its audit event. Returns
    False when it was not gated (idempotent, records nothing)."""
    with db.transaction(conn, immediate=True):
        if not is_gated(conn, target_user_id, action_kind):
            return False
        conn.execute(
            "DELETE FROM agent_approval_policies WHERE user_id = ? AND action_kind = ?",
            (target_user_id, action_kind),
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_POLICY_CLEARED,
            target_kind="user",
            target_id=target_user_id,
            detail=action_kind,
            commit=False,
        )
        return True
