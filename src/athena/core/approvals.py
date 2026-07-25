"""Human-in-the-loop approval gates — the Intervene step's "ask me first".

`VISION.md` promises the operator can "approve/reject risky actions" and steer by
exception. The only gate before this was the per-project blocked-close policy,
which can *refuse* but cannot *ask*: there was no way to say "hold that, and I'll
decide".

**Gate + retry, not deferred execution.** A gated command refuses and records a
pending request naming who wanted to do what to which target. When the operator
approves, the agent retries the same write and it succeeds, consuming the
approval. Athena deliberately does NOT serialize the agent's mutation and replay
it later on the agent's behalf — that would re-execute a stored payload under
authorization evaluated at a different moment, which is a new trust boundary
rather than a feature. Because the retry is an ordinary write, it re-validates
everything: visibility, role and scope, the blocked-close policy, the durable
budget, and any `If-Match`. An approval authorizes an **intent**, never a stored
side effect.

**Single-use.** Consuming happens inside the retry's own transaction, so an
approval can never decay into a standing permission: one approval, one action.

**Opt-in.** A user with no policy row is ungated, mirroring budgets and the
blocked-close policy. Applying migration 0063 changes nothing until an operator
gates someone.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from athena.core import activity, db

# The gated vocabulary. Kept here (not in a schema CHECK) so adding a gate is a
# code change with tests, not a migration.
ACTION_ISSUE_CLOSE = "issue.close"
ACTION_KINDS: frozenset[str] = frozenset({ACTION_ISSUE_CLOSE})

VERB_REQUESTED = "approval_requested"
VERB_APPROVED = "approval_approved"
VERB_REJECTED = "approval_rejected"
VERB_CONSUMED = "approval_consumed"
VERB_POLICY_SET = "approval_policy_set"
VERB_POLICY_CLEARED = "approval_policy_cleared"

# The stable machine-readable code every transport echoes, so an agent branches on
# it instead of parsing prose.
APPROVAL_REQUIRED_CODE = "approval_required"
APPROVAL_REJECTED_CODE = "approval_rejected"


@dataclass(frozen=True)
class ApprovalRequest:
    """One recorded ask, and its decision if it has one."""

    id: int
    action_kind: str
    target_kind: str
    target_id: int
    requested_by: int
    run_id: str | None
    state: str
    decided_by: int | None
    decided_at: str | None
    decision_note: str | None
    consumed_at: str | None
    created_at: str

    def public(self) -> dict:
        return {
            "id": self.id,
            "action_kind": self.action_kind,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "requested_by": self.requested_by,
            "run_id": self.run_id,
            "state": self.state,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "consumed_at": self.consumed_at,
            "created_at": self.created_at,
        }


class ApprovalRequired(Exception):
    """A gated action was refused; a pending request awaits an operator decision.

    Raised INSIDE the command's transaction so the refused write rolls back whole.
    The request row itself is recorded afterwards, on the freed connection — see
    :func:`open_request` — because anything written inside that transaction would
    roll back with it.
    """

    def __init__(
        self, *, actor_id: int, action_kind: str, target_kind: str, target_id: int
    ) -> None:
        super().__init__(f"{action_kind} requires operator approval")
        # Carried on the exception (like identity.ScopeDenied) so the app-level
        # handler can record the ask without re-resolving the request's identity.
        self.actor_id = actor_id
        self.action_kind = action_kind
        self.target_kind = target_kind
        self.target_id = target_id


class ApprovalRejected(Exception):
    """The operator explicitly rejected this exact ask. Distinct from
    :class:`ApprovalRequired` so an agent can stop retrying instead of waiting: a
    rejection is an answer, not a delay."""

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__(
            f"{request.action_kind} was rejected by the operator"
            + (f": {request.decision_note}" if request.decision_note else "")
        )
        self.request = request


def _row(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=row["id"],
        action_kind=row["action_kind"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        requested_by=row["requested_by"],
        run_id=row["run_id"],
        state=row["state"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        decision_note=row["decision_note"],
        consumed_at=row["consumed_at"],
        created_at=row["created_at"],
    )


def is_gated(conn: sqlite3.Connection, user_id: int, action_kind: str) -> bool:
    """Whether this user needs approval for this action kind (opt-in: no row = no)."""
    return (
        conn.execute(
            "SELECT 1 FROM agent_approval_policies "
            "WHERE user_id = ? AND action_kind = ?",
            (user_id, action_kind),
        ).fetchone()
        is not None
    )


def _live_request(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    action_kind: str,
    target_kind: str,
    target_id: int,
) -> ApprovalRequest | None:
    """The one live (pending, or approved-and-unspent) request for this exact
    intent, or None. A partial unique index guarantees at most one."""
    row = conn.execute(
        "SELECT * FROM approval_requests "
        "WHERE requested_by = ? AND action_kind = ? AND target_kind = ? "
        "AND target_id = ? AND state != 'rejected' AND consumed_at IS NULL",
        (user_id, action_kind, target_kind, target_id),
    ).fetchone()
    return _row(row) if row else None


def _latest_rejection(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    action_kind: str,
    target_kind: str,
    target_id: int,
) -> ApprovalRequest | None:
    row = conn.execute(
        "SELECT * FROM approval_requests "
        "WHERE requested_by = ? AND action_kind = ? AND target_kind = ? "
        "AND target_id = ? AND state = 'rejected' ORDER BY id DESC LIMIT 1",
        (user_id, action_kind, target_kind, target_id),
    ).fetchone()
    return _row(row) if row else None


def require(
    conn: sqlite3.Connection,
    actor: dict | None,
    *,
    action_kind: str,
    target_kind: str,
    target_id: int,
) -> None:
    """Open the gate for this exact intent, or refuse.

    Call INSIDE the gated command's ``db.transaction(immediate=True)``: consuming
    an approval must commit with the write it authorized, so a write that fails
    afterwards leaves the approval unspent and retryable.

    No-ops for an ungated actor (the default). Otherwise:

    * an approved, unconsumed request is **spent** here and the write proceeds;
    * an explicit rejection raises :class:`ApprovalRejected` — an answer, so the
      agent should stop rather than wait;
    * anything else raises :class:`ApprovalRequired`.
    """
    if actor is None:
        return
    if not is_gated(conn, actor["id"], action_kind):
        return
    live = _live_request(
        conn,
        user_id=actor["id"],
        action_kind=action_kind,
        target_kind=target_kind,
        target_id=target_id,
    )
    if live is not None and live.state == "approved":
        conn.execute(
            "UPDATE approval_requests SET consumed_at = datetime('now') WHERE id = ?",
            (live.id,),
        )
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_CONSUMED,
            target_kind=target_kind,
            target_id=target_id,
            detail=f"{action_kind} (approval #{live.id})",
            commit=False,
        )
        return
    rejected = _latest_rejection(
        conn,
        user_id=actor["id"],
        action_kind=action_kind,
        target_kind=target_kind,
        target_id=target_id,
    )
    if live is None and rejected is not None:
        raise ApprovalRejected(rejected)
    raise ApprovalRequired(
        actor_id=actor["id"],
        action_kind=action_kind,
        target_kind=target_kind,
        target_id=target_id,
    )


def open_request(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    action_kind: str,
    target_kind: str,
    target_id: int,
    run_id: str | None,
) -> ApprovalRequest:
    """Record (or return) the pending ask for a refused gated action.

    Called AFTER the refused command's transaction unwound, on the freed
    connection — a row written inside it would roll back with the refusal.
    Idempotent by the live partial unique index: an agent retrying while it waits
    re-reads its existing ask instead of flooding the operator's queue.
    """
    with db.transaction(conn, immediate=True):
        existing = _live_request(
            conn,
            user_id=actor_id,
            action_kind=action_kind,
            target_kind=target_kind,
            target_id=target_id,
        )
        if existing is not None:
            return existing
        cursor = conn.execute(
            "INSERT INTO approval_requests "
            "(action_kind, target_kind, target_id, requested_by, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (action_kind, target_kind, target_id, actor_id, run_id),
        )
        request_id = cursor.lastrowid
        assert request_id is not None
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_REQUESTED,
            target_kind=target_kind,
            target_id=target_id,
            detail=f"{action_kind} (approval #{request_id})",
            commit=False,
        )
        created = get_request(conn, request_id)
        assert created is not None
        return created


def get_request(conn: sqlite3.Connection, request_id: int) -> ApprovalRequest | None:
    row = conn.execute(
        "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
    ).fetchone()
    return _row(row) if row else None


def list_requests(
    conn: sqlite3.Connection, *, state: str | None = None, limit: int = 100
) -> list[ApprovalRequest]:
    """The operator's queue. Pending first, then newest — the shape a
    steer-by-exception surface wants."""
    limit = max(1, min(int(limit), 200))
    if state is not None:
        rows = conn.execute(
            "SELECT * FROM approval_requests WHERE state = ? ORDER BY id DESC LIMIT ?",
            (state, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM approval_requests "
            "ORDER BY (state = 'pending') DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row(row) for row in rows]


class ApprovalDecisionError(Exception):
    """A decision could not be applied. ``status_code`` lets adapters map it."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        raise ApprovalDecisionError(
            "decision must be 'approve' or 'reject'", status_code=422
        )
    with db.transaction(conn, immediate=True):
        request = get_request(conn, request_id)
        if request is None:
            raise ApprovalDecisionError("no such approval request", status_code=404)
        if request.state != "pending":
            raise ApprovalDecisionError(
                f"approval request is already {request.state}", status_code=409
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


def gated_kinds(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """Every action kind gated for this user, for the cockpit and `whoami`."""
    rows = conn.execute(
        "SELECT action_kind FROM agent_approval_policies WHERE user_id = ? "
        "ORDER BY action_kind",
        (user_id,),
    ).fetchall()
    return [row["action_kind"] for row in rows]
