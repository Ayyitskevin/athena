"""Atomic queue pickup, composed with the ordinary claim owner.

Only this operation refuses another pickup while the account holds live work.
Direct claims retain their existing policy and exact reviewed-revision contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena.aegis import (
    issue_etags,
    issues,
    lease_commands,
    leases,
    ready_queue,
    work_context,
)
from athena.aegis.issue_commands import IssueCommandError, _require_issue_writer
from athena.core import access, db
from athena.core.ids import MAX_SQLITE_INTEGER

SCHEMA = "athena.ready_claim.v1"
MAX_ATTEMPTS = 5


def claim_next_ready(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    limit: int = 1,
    lease_seconds: int | None = None,
    project_id: int | None = None,
) -> dict:
    """Select and acquire under one writer reservation, on a clean connection.

    The root tag is selected here, not an endorsement of a caller-reviewed
    revision. Workers still fetch full context and explicitly resume handoffs.
    Returned observations can be historical when replayed by idempotency.
    """
    actor = _require_issue_writer(actor)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_ATTEMPTS
    ):
        raise IssueCommandError(
            "invalid", f"limit must be between 1 and {MAX_ATTEMPTS}"
        )
    duration = leases.DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not leases.MIN_LEASE_SECONDS <= duration <= leases.MAX_LEASE_SECONDS
    ):
        raise IssueCommandError(
            "invalid", "lease_seconds is outside the allowed integer bounds"
        )
    if project_id is not None and (
        isinstance(project_id, bool)
        or not isinstance(project_id, int)
        or not 1 <= project_id <= MAX_SQLITE_INTEGER
    ):
        raise IssueCommandError(
            "invalid", "project_id must be a positive signed 64-bit integer"
        )
    if conn.in_transaction:
        raise IssueCommandError(
            "invalid", "ready pickup requires a connection without an open transaction"
        )

    with db.transaction(conn, immediate=True):
        # Sampling before BEGIN IMMEDIATE could reuse a clock from before a wait.
        now = datetime.now(UTC)
        if project_id is not None and not access.can_see_project(
            conn, actor, project_id
        ):
            raise IssueCommandError("not_found", "no such project")
        result: dict = {
            "schema": SCHEMA,
            "observed_at": leases.clock_stamp(now),
            "claimed": False,
            "attempts": [],
        }
        held = leases.count_active_leases_held_by(
            conn, holder_id=int(actor["id"]), now=now
        )
        if held:
            return {**result, "reason": "already_holding", "held_by_you": held}
        lane = ready_queue.build_ready(
            conn, actor=actor, now=now, limit=limit, project_id=project_id
        )
        if not lane["items"]:
            return {**result, "reason": "nothing_ready", "ready_total": 0}
        for item in lane["items"]:
            issue_id = item["issue"]["id"]
            issue = issues.get_issue(conn, issue_id)
            assert issue is not None
            tag = issue_etags.current_etag(conn, issue)
            try:
                lease = lease_commands.claim_issue(
                    conn,
                    actor=actor,
                    issue_id=issue_id,
                    if_match=[tag],
                    lease_seconds=duration,
                )
            except IssueCommandError as exc:
                if exc.kind != "conflict":
                    raise
                result["attempts"].append({"issue_id": issue_id, "outcome": "conflict"})
                continue
            context = work_context.build_work_context(conn, str(issue_id), actor=actor)
            if context is None:
                raise RuntimeError("selected issue disappeared inside ready pickup")
            return {
                **result,
                "claimed": True,
                "issue": item["issue"],
                "lease": lease,
                "issue_etag": context["issue_etag"],
                "work_context_etag": work_context.context_etag(context),
                "previous_lease": item["previous_lease"],
                "open_handoff": item["open_handoff"],
            }
        return {**result, "reason": "candidates_conflicted"}
