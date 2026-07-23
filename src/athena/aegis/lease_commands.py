"""Application commands for the delegation claim/lease protocol — claim, renew,
yield, decline, and complete — so two agents cannot silently pull the same work.

An issue may be delegated to several agents (the contributor set). A lease is the
exclusive "I am actively working this now" token. Claim and renewal require the caller's
exact current root-issue revision, preventing acceptance of stale work. Completion
releases finished work, yield records a holder's honest non-completion reason, and
decline rejects the delegation outright. The lease remains distinct from the assignee
(accountable) and contributor set (eligible).

Each command owns one transaction: lease state and its audit event commit or roll back
together. Authorization is resolved here, reusing the issue command policy so REST and
MCP share one boundary.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from athena.aegis import (
    contributors as contributors_data,
    issue_activity,
    leases,
)
from athena.aegis.issue_commands import (
    IssueCommandError,
    _check_issue_precondition,
    _require_issue_writer,
    _visible_issue,
)
from athena.core import db, identity

ClaimYieldReason = Literal["needs_input", "blocked", "capacity"]
CLAIM_YIELD_REASONS = frozenset({"needs_input", "blocked", "capacity"})
MAX_CLAIM_YIELD_NOTE_CHARS = 500
CLAIM_PRECONDITION_REQUIRED_DETAIL = (
    "If-Match with exactly one strong issue ETag is required to claim"
)


def _claimant_or_reject(
    conn: sqlite3.Connection, issue: dict, actor: dict
) -> None:
    """A lease is for whoever actually works the issue: its assignee, a delegated
    contributor, or an admin. Deliberately NARROWER than the general issue-write gate
    (which also lets the creator in) — creating an issue is not the same as being handed
    it to work."""
    if (
        issue["assignee_id"] == actor["id"]
        or contributors_data.is_contributor(conn, issue["id"], actor["id"])
        or identity.is_admin(actor)
    ):
        return
    raise IssueCommandError(
        "forbidden",
        "only the assignee, a delegated contributor, or an admin may claim this issue",
    )


def claim_issue(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    if_match: list[str] | None = None,
    lease_seconds: int = leases.DEFAULT_LEASE_SECONDS,
) -> dict:
    """Take the exclusive lease on an issue (accept). Returns the lease
    {issue_id, holder_id, holder_name, claimed_at, expires_at, active}.

    Rejects with IssueCommandError('conflict') if the issue is already held by a DIFFERENT
    actor whose lease is still active — the interlock that stops double-work; the detail
    names the current holder and expiry. The same actor re-claiming RENEWS the window
    (idempotent extend), and a lease whose window has passed is reclaimable by anyone
    eligible, so a crashed holder never pins the work. 401/403 for the scope/writer and
    claimant gates, 404 for an unseeable issue, 422 for an out-of-range lease window.

    Read-then-write runs under BEGIN IMMEDIATE, so two agents racing to claim the same
    free issue serialize: the first acquires, the second re-reads the now-active lease and
    is rejected."""
    actor = _require_issue_writer(actor)
    if not (leases.MIN_LEASE_SECONDS <= lease_seconds <= leases.MAX_LEASE_SECONDS):
        raise IssueCommandError(
            "invalid",
            f"lease_seconds must be between {leases.MIN_LEASE_SECONDS} and "
            f"{leases.MAX_LEASE_SECONDS}",
        )
    with db.transaction(conn, immediate=True):
        issue = _visible_issue(conn, actor, issue_id)
        _claimant_or_reject(conn, issue, actor)
        existing = leases.get_lease(conn, issue_id)
        renewed = False
        if existing is not None and existing["active"]:
            if existing["holder_id"] != actor["id"]:
                raise IssueCommandError(
                    "conflict",
                    f"issue is claimed by {existing['holder_name']} until "
                    f"{existing['expires_at']}",
                )
            renewed = True  # the holder re-claiming just extends its own window
        _check_issue_precondition(
            conn,
            issue,
            if_match,
            required_detail=CLAIM_PRECONDITION_REQUIRED_DETAIL,
            exact=True,
        )
        lease = leases.upsert_lease(
            conn, issue_id, actor["id"], lease_seconds, commit=False
        )
        issue_activity.record_issue_claimed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            expires_at=lease["expires_at"],
            renewed=renewed,
            commit=False,
        )
        return lease


def _normalize_claim_yield(reason: str, note: str | None) -> tuple[str, str | None]:
    if not isinstance(reason, str) or reason not in CLAIM_YIELD_REASONS:
        raise IssueCommandError(
            "invalid",
            "reason must be one of: needs_input, blocked, capacity",
        )
    if note is not None and not isinstance(note, str):
        raise IssueCommandError("invalid", "note must be a string or null")
    if note is not None and len(note) > MAX_CLAIM_YIELD_NOTE_CHARS:
        raise IssueCommandError(
            "invalid",
            f"note must be at most {MAX_CLAIM_YIELD_NOTE_CHARS} characters",
        )
    normalized_note = note.strip() if note is not None else None
    return reason, normalized_note or None


def yield_claim(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    reason: str,
    note: str | None = None,
) -> None:
    """Release the caller's active lease without pretending the work completed.

    Yield is deliberately holder-only, including for admins: recording another
    actor as voluntarily yielding would make the audit trail lie. It preserves
    assignment, contributors, status, and dependencies, and never auto-routes
    the issue. The lease deletion and run-stamped activity event are atomic.
    """
    actor = _require_issue_writer(actor)
    reason, note = _normalize_claim_yield(reason, note)
    with db.transaction(conn, immediate=True):
        _visible_issue(conn, actor, issue_id)
        existing = leases.get_lease(conn, issue_id)
        if existing is None or not existing["active"]:
            raise IssueCommandError("conflict", "no active claim to yield")
        if existing["holder_id"] != actor["id"]:
            raise IssueCommandError(
                "conflict",
                f"issue is claimed by {existing['holder_name']}, not you",
            )
        leases.delete_lease(conn, issue_id, commit=False)
        issue_activity.record_claim_yielded(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            reason=reason,
            note=note,
            commit=False,
        )


def complete_claim(
    conn: sqlite3.Connection, *, actor: dict | None, issue_id: int
) -> None:
    """Release the lease by completing the claimed work (complete) — the issue is freed for
    the next claimant. The actor must hold the ACTIVE lease (an admin may release anyone's,
    a moderation lever). Rejects with IssueCommandError('conflict') if there is no active
    lease or it belongs to someone else; 404 for an unseeable issue. Completion releases the
    coordination lease only — it does not change the issue's status; the agent transitions
    status through the ordinary (audited) status command."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        issue = _visible_issue(conn, actor, issue_id)
        _claimant_or_reject(conn, issue, actor)
        existing = leases.get_lease(conn, issue_id)
        if existing is None or not existing["active"]:
            raise IssueCommandError("conflict", "no active claim to complete")
        if existing["holder_id"] != actor["id"] and not identity.is_admin(actor):
            raise IssueCommandError(
                "conflict",
                f"issue is claimed by {existing['holder_name']}, not you",
            )
        leases.delete_lease(conn, issue_id, commit=False)
        issue_activity.record_claim_completed(
            conn, actor_id=actor["id"], issue_id=issue_id, commit=False
        )


def decline_delegation(
    conn: sqlite3.Connection, *, actor: dict | None, issue_id: int
) -> list[dict]:
    """Decline a delegation handed to you (decline): remove YOURSELF from the contributor
    set so the work is visibly refused, not silently dropped, and can be re-routed. Returns
    the remaining contributor list. Self-service — the actor removes only itself; rejects
    with IssueCommandError('not_found') if the actor was not a delegated contributor. Any
    lease the actor held on the issue is released with the same act (giving up the
    delegation gives up the claim)."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        _visible_issue(conn, actor, issue_id)
        if not contributors_data.remove_contributor(
            conn, issue_id, actor["id"], commit=False
        ):
            raise IssueCommandError(
                "not_found", "you are not a delegated contributor on this issue"
            )
        held = leases.get_lease(conn, issue_id)
        if held is not None and held["holder_id"] == actor["id"]:
            leases.delete_lease(conn, issue_id, commit=False)
        issue_activity.record_delegation_declined(
            conn, actor_id=actor["id"], issue_id=issue_id, commit=False
        )
        return contributors_data.list_contributors(conn, issue_id)
