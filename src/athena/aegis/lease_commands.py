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

import secrets
import sqlite3
from typing import Literal

from athena.aegis import (
    claim_handoffs,
    contributors as contributors_data,
    issue_activity,
    leases,
    statuses,
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
MAX_HANDOFF_ATTEMPTED_WORK_CHARS = 4_000
MAX_HANDOFF_EVIDENCE_ITEMS = 10
MAX_HANDOFF_EVIDENCE_ITEM_CHARS = 1_000
MAX_HANDOFF_EVIDENCE_JSON_CHARS = 8_000
MAX_HANDOFF_BLOCKING_QUESTION_CHARS = 1_000
MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS = 4_000
MAX_HANDOFF_RESUME_NOTE_CHARS = 2_000
CLAIM_PRECONDITION_REQUIRED_DETAIL = (
    "If-Match with exactly one strong issue ETag is required to claim"
)
LEASE_GENERATION_REQUIRED_DETAIL = (
    "the exact current lease generation is required for this operation"
)
MAX_DECLARED_PATHS = 32
MAX_DECLARED_PATH_CHARS = 256


def claimant_is_eligible(
    conn: sqlite3.Connection, issue: dict, actor: dict | None
) -> bool:
    """Whether ``actor`` passes the command-owned claimant policy.

    Read surfaces may use this to advertise an affordance, but it grants no
    authority: :func:`claim_issue` evaluates the same function again under its
    write transaction.
    """
    return actor is not None and (
        issue["assignee_id"] == actor["id"]
        or contributors_data.is_contributor(conn, issue["id"], actor["id"])
        or identity.is_admin(actor)
    )


def _claimant_or_reject(conn: sqlite3.Connection, issue: dict, actor: dict) -> None:
    """A lease is for whoever actually works the issue: its assignee, a delegated
    contributor, or an admin. Deliberately NARROWER than the general issue-write gate
    (which also lets the creator in) — creating an issue is not the same as being handed
    it to work."""
    if claimant_is_eligible(conn, issue, actor):
        return
    raise IssueCommandError(
        "forbidden",
        "only the assignee, a delegated contributor, or an admin may claim this issue",
    )


def _normalize_lease_generation(
    generation: object | None, *, required: bool
) -> str | None:
    if generation is None:
        if required:
            raise IssueCommandError(
                "lease_generation_required",
                LEASE_GENERATION_REQUIRED_DETAIL,
            )
        return None
    if not leases.is_valid_generation(generation):
        raise IssueCommandError(
            "invalid_lease_generation",
            "lease generation must be exactly 32 lowercase hexadecimal characters",
        )
    return generation


def _matching_lease_generation(existing: dict, generation: object | None) -> str:
    normalized = _normalize_lease_generation(generation, required=True)
    assert normalized is not None
    if not secrets.compare_digest(existing["generation"], normalized):
        raise IssueCommandError(
            "lease_generation_mismatch",
            "lease generation is stale; fetch the current lease before retrying",
        )
    return normalized


def normalize_declared_paths(raw: object) -> list[str]:
    """Repo-relative POSIX paths. Empty means issue-fence only."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise IssueCommandError("invalid", "paths must be a list of strings")
    if len(raw) > MAX_DECLARED_PATHS:
        raise IssueCommandError(
            "invalid",
            f"at most {MAX_DECLARED_PATHS} declared paths",
        )
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise IssueCommandError("invalid", "paths must be a list of strings")
        text = item.strip().replace("\\", "/")
        if not text or "\x00" in text:
            raise IssueCommandError("invalid", "declared path is empty or invalid")
        if text.startswith("/"):
            raise IssueCommandError("invalid", "declared paths must be relative")
        parts: list[str] = []
        for part in text.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise IssueCommandError(
                    "invalid", "declared paths may not contain '..'"
                )
            parts.append(part)
        if not parts:
            raise IssueCommandError("invalid", "declared path is empty or invalid")
        normalized = "/".join(parts)
        if len(normalized) > MAX_DECLARED_PATH_CHARS:
            raise IssueCommandError(
                "invalid",
                f"declared path must be at most {MAX_DECLARED_PATH_CHARS} characters",
            )
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _refuse_overlapping_paths(
    conn: sqlite3.Connection, *, issue_id: int, paths: list[str]
) -> None:
    if not paths:
        return
    for other in leases.list_active_leases(conn, except_issue_id=issue_id):
        for mine in paths:
            for theirs in other.get("declared_paths") or []:
                if paths_overlap(mine, theirs):
                    raise IssueCommandError(
                        "conflict",
                        f"declared path '{mine}' overlaps '{theirs}' on issue "
                        f"{other['issue_id']} held by {other['holder_name']}",
                    )


def claim_issue(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    if_match: list[str] | None = None,
    generation: object | None = None,
    lease_seconds: int = leases.DEFAULT_LEASE_SECONDS,
    paths: object = None,
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
        current_generation: str | None = None
        if existing is not None and existing["active"]:
            if existing["holder_id"] != actor["id"]:
                raise IssueCommandError(
                    "conflict",
                    f"issue is claimed by {existing['holder_name']} until "
                    f"{existing['expires_at']}",
                )
            # Supplying the current generation explicitly selects renew mode. An
            # omitted token may never turn a delayed acquire into a renewal.
            current_generation = _matching_lease_generation(existing, generation)
            renewed = True
        elif generation is not None:
            # Supplying a token explicitly selects renew mode. An expired/absent
            # possession cannot be renewed and must not silently become an acquire.
            _normalize_lease_generation(generation, required=True)
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation is stale; acquire again without a generation",
            )
        _check_issue_precondition(
            conn,
            issue,
            if_match,
            required_detail=CLAIM_PRECONDITION_REQUIRED_DETAIL,
            exact=True,
        )
        declared_paths = normalize_declared_paths(paths)
        _refuse_overlapping_paths(conn, issue_id=issue_id, paths=declared_paths)
        lease = leases.upsert_lease(
            conn,
            issue_id,
            actor["id"],
            lease_seconds,
            generation=current_generation,
            declared_paths=declared_paths,
            commit=False,
        )
        issue_activity.record_issue_claimed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            expires_at=lease["expires_at"],
            generation=lease["generation"],
            renewed=renewed,
            commit=False,
        )
        lease["open_claim_handoff"] = claim_handoffs.get_open_handoff(conn, issue_id)
        return lease


def _normalize_claim_yield(reason: str, note: str | None) -> tuple[str, str | None]:
    if not isinstance(reason, str) or reason not in CLAIM_YIELD_REASONS:
        raise IssueCommandError(
            "invalid",
            "reason must be one of: needs_input, blocked, capacity",
        )
    normalized_note = _normalize_handoff_text(
        note,
        name="note",
        maximum=MAX_CLAIM_YIELD_NOTE_CHARS,
        required=False,
    )
    return reason, normalized_note or None


def _normalize_handoff_text(
    value: object | None,
    *,
    name: str,
    maximum: int,
    required: bool,
) -> str | None:
    if value is None:
        if required:
            raise IssueCommandError("invalid", f"{name} is required")
        return None
    if not isinstance(value, str):
        suffix = "a string" if required else "a string or null"
        raise IssueCommandError("invalid", f"{name} must be {suffix}")
    if len(value) > maximum:
        raise IssueCommandError(
            "invalid", f"{name} must be at most {maximum} characters"
        )
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise IssueCommandError(
            "invalid", f"{name} contains an unsupported control character"
        )
    normalized = value.strip()
    if required and not normalized:
        raise IssueCommandError("invalid", f"{name} must not be blank")
    return normalized or None


def _normalize_handoff_evidence(evidence: object) -> list[str]:
    if not isinstance(evidence, list):
        raise IssueCommandError("invalid", "evidence must be an array of strings")
    if len(evidence) > MAX_HANDOFF_EVIDENCE_ITEMS:
        raise IssueCommandError(
            "invalid",
            f"evidence must contain at most {MAX_HANDOFF_EVIDENCE_ITEMS} items",
        )
    normalized: list[str] = []
    for index, item in enumerate(evidence):
        text = _normalize_handoff_text(
            item,
            name=f"evidence[{index}]",
            maximum=MAX_HANDOFF_EVIDENCE_ITEM_CHARS,
            required=True,
        )
        assert text is not None
        normalized.append(text)
    if (
        len(claim_handoffs.encode_evidence(normalized))
        > MAX_HANDOFF_EVIDENCE_JSON_CHARS
    ):
        raise IssueCommandError(
            "invalid",
            f"encoded evidence must be at most {MAX_HANDOFF_EVIDENCE_JSON_CHARS} characters",
        )
    return normalized


def _normalize_handoff_token(handoff_token: object) -> str:
    if not claim_handoffs.is_valid_handoff_token(handoff_token):
        raise IssueCommandError(
            "invalid",
            "handoff token must be exactly 32 lowercase hexadecimal characters",
        )
    assert isinstance(handoff_token, str)
    return handoff_token


def yield_claim(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    generation: object | None,
    reason: str,
    note: str | None = None,
    attempted_work: object,
    evidence: object,
    blocking_question: object,
    resume_instructions: object,
) -> dict:
    """Release the caller's active lease without pretending the work completed.

    Yield is deliberately holder-only, including for admins: recording another
    actor as voluntarily yielding would make the audit trail lie. It preserves
    assignment, contributors, status, and dependencies, and never auto-routes
    the issue. The lease deletion and run-stamped activity event are atomic.
    """
    actor = _require_issue_writer(actor)
    reason, note = _normalize_claim_yield(reason, note)
    normalized_attempted_work = _normalize_handoff_text(
        attempted_work,
        name="attempted_work",
        maximum=MAX_HANDOFF_ATTEMPTED_WORK_CHARS,
        required=True,
    )
    normalized_evidence = _normalize_handoff_evidence(evidence)
    normalized_blocking_question = _normalize_handoff_text(
        blocking_question,
        name="blocking_question",
        maximum=MAX_HANDOFF_BLOCKING_QUESTION_CHARS,
        required=True,
    )
    normalized_resume_instructions = _normalize_handoff_text(
        resume_instructions,
        name="resume_instructions",
        maximum=MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS,
        required=True,
    )
    assert normalized_attempted_work is not None
    assert normalized_blocking_question is not None
    assert normalized_resume_instructions is not None
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
        current_generation = _matching_lease_generation(existing, generation)
        if claim_handoffs.get_open_handoff(conn, issue_id) is not None:
            raise IssueCommandError(
                "conflict",
                "an open claim handoff must be resumed before another can be created",
            )
        handoff_token = claim_handoffs.new_handoff_token()
        event = issue_activity.record_claim_yielded(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            generation=current_generation,
            handoff_token=handoff_token,
            reason=reason,
            blocking_question=normalized_blocking_question,
            commit=False,
        )
        handoff = claim_handoffs.create_handoff(
            conn,
            issue_id=issue_id,
            yield_event_id=event["id"],
            lease_generation=current_generation,
            reason=reason,
            note=note,
            attempted_work=normalized_attempted_work,
            evidence=normalized_evidence,
            blocking_question=normalized_blocking_question,
            resume_instructions=normalized_resume_instructions,
            handoff_token=handoff_token,
        )
        if not leases.delete_lease(conn, issue_id, current_generation, commit=False):
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation changed before release",
            )
        return handoff


def resume_claim_handoff(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    handoff_token: object,
    generation: object | None,
    resume_note: object | None = None,
) -> dict:
    """Acknowledge one open handoff as the exact current leaseholder.

    This transition means the context was received. It never asserts that the
    blocker was solved, work completed, approval granted, or instructions trusted.
    """
    actor = _require_issue_writer(actor)
    normalized_token = _normalize_handoff_token(handoff_token)
    normalized_note = _normalize_handoff_text(
        resume_note,
        name="resume_note",
        maximum=MAX_HANDOFF_RESUME_NOTE_CHARS,
        required=False,
    )
    with db.transaction(conn, immediate=True):
        _visible_issue(conn, actor, issue_id)
        existing = leases.get_lease(conn, issue_id)
        if existing is None or not existing["active"]:
            raise IssueCommandError(
                "conflict", "an active claim is required to resume a handoff"
            )
        if existing["holder_id"] != actor["id"]:
            raise IssueCommandError(
                "conflict", f"issue is claimed by {existing['holder_name']}, not you"
            )
        current_generation = _matching_lease_generation(existing, generation)
        handoff = claim_handoffs.get_handoff(
            conn, issue_id=issue_id, handoff_token=normalized_token
        )
        if handoff is None:
            raise IssueCommandError("not_found", "claim handoff not found")
        if handoff["state"] != "awaiting_resume":
            raise IssueCommandError("conflict", "claim handoff was already resumed")
        event = issue_activity.record_claim_handoff_resumed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            handoff_token=normalized_token,
            generation=current_generation,
            commit=False,
        )
        resumed = claim_handoffs.resume_handoff(
            conn,
            issue_id=issue_id,
            handoff_token=normalized_token,
            resume_event_id=event["id"],
            lease_generation=current_generation,
            resume_note=normalized_note,
        )
        if resumed is None:
            raise IssueCommandError("conflict", "claim handoff was already resumed")
        return resumed


def complete_claim(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    generation: object | None,
) -> dict:
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
        current_generation = _matching_lease_generation(existing, generation)
        if (
            existing["holder_id"] == actor["id"]
            and claim_handoffs.get_open_handoff(conn, issue_id) is not None
        ):
            raise IssueCommandError(
                "conflict",
                "resume the open claim handoff before completing this possession",
            )
        if not leases.delete_lease(conn, issue_id, current_generation, commit=False):
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation changed before release",
            )
        issue_activity.record_claim_completed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            generation=current_generation,
            commit=False,
        )
        still_open = (
            statuses.category_of(conn, issue.get("project_id"), issue["status"])
            != "done"
        )
        return {
            "released": True,
            "issue_id": issue_id,
            "issue_status": issue["status"],
            "issue_still_open": still_open,
            "next": (
                "complete_claim released the lease only; issue status is unchanged"
            ),
        }


def decline_delegation(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    generation: object | None = None,
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
        if not contributors_data.is_contributor(conn, issue_id, actor["id"]):
            raise IssueCommandError(
                "not_found", "you are not a delegated contributor on this issue"
            )
        held = leases.get_lease(conn, issue_id)
        released_generation = None
        if held is not None and held["holder_id"] == actor["id"]:
            if held["active"]:
                released_generation = _matching_lease_generation(held, generation)
            else:
                # An expired row is not a current possession. A generationless
                # pre-claim decline therefore leaves it for the next acquisition
                # to replace instead of performing any unfenced deletion.
                if generation is not None:
                    released_generation = _matching_lease_generation(held, generation)
                else:
                    # The immediate writer lock makes the server-observed expired
                    # generation an exact fence; no replacement can appear between
                    # this read and deletion.
                    released_generation = held["generation"]
        elif generation is not None:
            _normalize_lease_generation(generation, required=True)
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation is stale; fetch the current lease before retrying",
            )
        if not contributors_data.remove_contributor(
            conn, issue_id, actor["id"], commit=False
        ):
            raise RuntimeError("delegation disappeared inside its write transaction")
        if released_generation is not None and not leases.delete_lease(
            conn, issue_id, released_generation, commit=False
        ):
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation changed before release",
            )
        issue_activity.record_delegation_declined(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            released_generation=released_generation,
            commit=False,
        )
        return contributors_data.list_contributors(conn, issue_id)
