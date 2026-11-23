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
    review_request_projection,
    work_context,
)
from athena.aegis.issue_commands import IssueCommandError, _require_issue_writer
from athena.core import access, db
from athena.core.ids import MAX_SQLITE_INTEGER

SCHEMA = "athena.ready_claim.v1"
MAX_ATTEMPTS = 5
FENCE_CODES = frozenset(
    {lease_commands.FENCE_OVERLAP_CODE, lease_commands.FENCE_UNRESOLVED_CODE}
)


def waiting_review(
    conn: sqlite3.Connection, *, actor: dict, now: datetime
) -> dict | None:
    """The review the desk is asking this account for, named by an answer that
    claims nothing (D16): the newest visible request naming the caller that is
    still ``requested``, and the claim that takes its review issue. Pickup does
    not claim it: the review issue may be blocked by the build it reviews, and
    a blocker keeps its meaning. The desk's own read decides, so a request the
    caller cannot see reads as absent and the two never disagree."""
    lane = review_request_projection.visible_requests(
        conn, actor=actor, lane="desk", scope_id=int(actor["id"]), now=now
    )
    pending = [
        row for row in (lane or {}).get("items", []) if row["status"] == "requested"
    ]
    if not pending:
        return None
    request = pending[0]
    issue = issues.get_issue(conn, request["review_issue_id"])
    assert issue is not None  # the projection read it under this snapshot
    return {
        "request_id": request["request_id"],
        "review_issue_id": request["review_issue_id"],
        "candidate_id": request["candidate_id"],
        "source_issue_id": request["source_issue_id"],
        # Requests still waiting among the desk lane's newest, not a count of
        # every request ever made.
        "pending_total": len(pending),
        "next": {
            "tool": "claim_issue",
            "arguments": {
                "issue_id": request["review_issue_id"],
                "if_match": issue_etags.current_etag(conn, issue),
            },
            "hint": (
                "pickup does not take a review; claim its review issue directly "
                "(if_match is its ETag at this answer, so a 412 means re-read "
                "it), then read the source's work context"
            ),
        },
    }


def claim_next_ready(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    limit: int = 1,
    lease_seconds: int | None = None,
    project_id: int | None = None,
    paths: object = None,
) -> dict:
    """Select and acquire under one writer reservation, on a clean connection.

    The root tag is selected here, not an endorsement of a caller-reviewed
    revision. Workers still fetch full context and explicitly resume handoffs.
    Returned observations can be historical when replayed by idempotency.

    ``paths`` are declared on the claim this makes, through the claim command's
    own fence check (C6). A fence refusal does not depend on the candidate (the
    caller's paths against every other active lease), so the first one ends the
    pickup with ``reason`` set to its code and the refusal's ``fence`` object.
    """
    actor = _require_issue_writer(actor)
    # Validation precedes selection: a malformed path refuses before anything
    # is read, never only once a candidate exists.
    declared_paths = lease_commands.normalize_declared_paths(paths)
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
        # Every answer that claims nothing names a review the desk is asking
        # for, or says null: present on each, so absence is never a default.
        if held:
            return {
                **result,
                "reason": "already_holding",
                "held_by_you": held,
                "review_request": waiting_review(conn, actor=actor, now=now),
            }
        lane = ready_queue.build_ready(
            conn, actor=actor, now=now, limit=limit, project_id=project_id
        )
        if not lane["items"]:
            return {
                **result,
                "reason": "nothing_ready",
                "ready_total": 0,
                "review_request": waiting_review(conn, actor=actor, now=now),
            }
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
                    paths=declared_paths,
                )
            except IssueCommandError as exc:
                if exc.kind != "conflict":
                    raise
                result["attempts"].append({"issue_id": issue_id, "outcome": "conflict"})
                if exc.code in FENCE_CODES:
                    assert exc.extra is not None
                    return {
                        **result,
                        "reason": exc.code,
                        "fence": exc.extra["fence"],
                        "review_request": waiting_review(conn, actor=actor, now=now),
                    }
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
        return {
            **result,
            "reason": "candidates_conflicted",
            "review_request": waiting_review(conn, actor=actor, now=now),
        }
