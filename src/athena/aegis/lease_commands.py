"""Application commands for the delegation claim/lease protocol — accept / decline /
complete, so two agents cannot silently pull the same delegated issue.

An issue may be delegated to several agents (the contributor set). Without coordination,
two of them could start the same work. A LEASE is the exclusive "I am actively working
this now" token: claiming acquires it (rejecting a second claimant while it is live),
completing releases it, and declining rejects the delegation outright. The lease is
distinct from the assignee (accountable) and the contributor set (eligible) — it is the
run-time interlock between them.

Like every other Aegis write, each command owns one transaction: the lease row and its
audit event commit or roll back together. Authorization is resolved here (the actor must
be the issue's assignee, a delegated contributor, or an admin), reusing the same
scope/writer gate the issue commands use, so REST and MCP share one policy.
"""
from __future__ import annotations

import sqlite3

from athena.aegis import (
    contributors as contributors_data,
    issue_activity,
    leases,
)
from athena.aegis.issue_commands import (
    IssueCommandError,
    _require_issue_writer,
    _visible_issue,
)
from athena.core import db, identity


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
