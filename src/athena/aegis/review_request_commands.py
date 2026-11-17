"""Record an exact candidate request without assigning or claiming either issue."""

import sqlite3

from pydantic import TypeAdapter, ValidationError

from athena.aegis import contributors, issues, leases, review_commands, statuses
from athena.aegis.issue_commands import IssueCommandError, _resolve_write_actor
from athena.aegis.review_models import (
    CandidateReviewRequest,
    CandidateReviewRequestReceipt,
    ReviewCandidateId,
)
from athena.core import access, activity, db, identity, tokens, users

_ID = TypeAdapter(ReviewCandidateId)
_NOT_FOUND = "no such review target"
_CHANGED = "review request is unavailable or changed"


def _check_live_scope(conn: sqlite3.Connection, actor: dict) -> None:
    """The transport may have authenticated a bearer before another writer revoked it."""
    token_id = actor.get("_token_id")
    if token_id is None:
        return
    row = conn.execute(
        "SELECT scopes FROM api_tokens WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (token_id, actor["id"]),
    ).fetchone()
    if row is None:
        raise IssueCommandError("unauthorized", "live bearer token required")
    try:
        scopes = tokens.parse_scopes(row["scopes"])
    except (ValueError, AttributeError) as exc:
        raise IssueCommandError(
            "forbidden", "token scope required: issue:write"
        ) from exc
    if not identity.token_has_scope(
        {"_token_scopes": scopes}, tokens.ISSUE_WRITE_SCOPE
    ):
        raise IssueCommandError("forbidden", "token scope required: issue:write")


def _check_reviewer(
    conn: sqlite3.Connection, *, issue: dict, candidate: dict, reviewer_id: int
) -> None:
    reviewer = users.get_user(conn, reviewer_id)
    if (
        reviewer is None
        or reviewer["removed_at"] is not None
        or not access.can_see_issue(conn, reviewer, issue["id"])
        or review_commands.visible_candidate(conn, candidate["id"], reviewer) is None
    ):
        raise IssueCommandError("not_found", _NOT_FOUND)
    if (
        reviewer["paused_at"] is not None
        or not identity.can_write(reviewer)
        or reviewer_id == candidate["actor_id"]
        or (
            issue["assignee_id"] != reviewer_id
            and not contributors.is_contributor(conn, issue["id"], reviewer_id)
        )
    ):
        raise IssueCommandError("conflict", "reviewer is ineligible for this request")


def request_candidate_review(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    review_issue_id: int,
    candidate_id: int,
    reviewer_id: int,
) -> dict:
    """Publish or recover one exact request under a clean writer reservation."""
    try:
        _ID.validate_python(review_issue_id)
        payload = CandidateReviewRequest(
            candidate_id=candidate_id, reviewer_id=reviewer_id
        )
    except ValidationError as exc:
        raise IssueCommandError("invalid", "invalid review request IDs") from exc
    if conn.in_transaction:
        raise IssueCommandError("invalid", "review request requires a clean connection")

    with db.transaction(conn, immediate=True):
        try:
            actor, issue = _resolve_write_actor(
                conn, actor, review_issue_id, enforce_actor_policy=True
            )
        except IssueCommandError as exc:
            if exc.kind == "not_found":
                raise IssueCommandError("not_found", _NOT_FOUND) from exc
            raise
        _check_live_scope(conn, actor)
        prior = review_commands.latest_visible_request(
            conn, review_issue_id=review_issue_id, actor=actor
        )
        candidate = review_commands.visible_candidate(conn, candidate_id, actor)
        if candidate is None:
            raise IssueCommandError("not_found", _NOT_FOUND)
        source = issues.get_issue(conn, candidate["issue_id"])
        assert source is not None
        if (
            source["id"] == review_issue_id
            or issue["archived_at"] is not None
            or source["archived_at"] is not None
            or issue["project_id"] is None
            or issue["project_id"] != source["project_id"]
            or candidate_id != review_commands.latest_candidate_id(conn, source["id"])
        ):
            raise IssueCommandError("conflict", _CHANGED)
        _check_reviewer(conn, issue=issue, candidate=candidate, reviewer_id=reviewer_id)
        if prior is not None:
            if prior["source_issue_id"] != source["id"]:
                raise IssueCommandError("conflict", _CHANGED)
            if prior["candidate_id"] == candidate_id:
                if prior["reviewer_id"] != reviewer_id:
                    raise IssueCommandError("conflict", _CHANGED)
                return CandidateReviewRequestReceipt(
                    request_id=prior["id"],
                    review_issue_id=review_issue_id,
                    **payload.model_dump(),
                ).model_dump()

        lease = leases.get_lease(conn, review_issue_id)
        if statuses.is_done(conn, issue["project_id"], issue["status"]) or (
            lease is not None and lease["active"]
        ):
            raise IssueCommandError(
                "conflict", "a new review request needs an open, unclaimed issue"
            )
        event = activity.record(
            conn,
            actor_id=actor["id"],
            verb="review_requested",
            target_kind="issue",
            target_id=review_issue_id,
            commit=False,
        )
        inserted = conn.execute(
            "INSERT INTO issue_review_requests(candidate_id,review_issue_id,reviewer_id,request_event_id) "
            "VALUES (?,?,?,?)",
            (candidate_id, review_issue_id, reviewer_id, event["id"]),
        )
        assert inserted.lastrowid is not None
        return CandidateReviewRequestReceipt(
            request_id=inserted.lastrowid,
            review_issue_id=review_issue_id,
            **payload.model_dump(),
        ).model_dump()
