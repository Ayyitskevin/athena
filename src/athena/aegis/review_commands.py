"""Completion-owned candidate checks and immutable inserts.

The caller owns BEGIN IMMEDIATE and proves active own generation before release.
No report is copied here; the freshly recorded completion event owns that fact.
"""

import sqlite3

from pydantic import TypeAdapter, ValidationError

from athena.aegis import issues
from athena.aegis.issue_commands import IssueCommandError
from athena.aegis.review_models import (
    CandidateReviewReceipt,
    CandidateSubmission,
    CandidateSubmissionReceipt,
    ReviewCandidateId,
)
from athena.core import access

_REVIEW_ID = TypeAdapter(ReviewCandidateId)
_PREDECESSOR_CONFLICT = "candidate predecessor is unavailable or changed"


def normalize_extensions(
    candidate_submission: object, review_candidate_id: object
) -> tuple[CandidateSubmission | None, int | None]:
    if candidate_submission is not None and review_candidate_id is not None:
        raise IssueCommandError(
            "invalid", "completion extensions are mutually exclusive"
        )
    try:
        submission = (
            None
            if candidate_submission is None
            else CandidateSubmission.model_validate(candidate_submission)
        )
        review_id = (
            None
            if review_candidate_id is None
            else _REVIEW_ID.validate_python(review_candidate_id)
        )
    except ValidationError as exc:
        # ValidationError text includes raw input; it must not echo credentials.
        raise IssueCommandError(
            "invalid", "invalid candidate completion extension"
        ) from exc
    return submission, review_id


def latest_candidate_id(conn: sqlite3.Connection, issue_id: int) -> int | None:
    return conn.execute(
        "SELECT MAX(id) FROM issue_review_candidates WHERE issue_id = ?", (issue_id,)
    ).fetchone()[0]


def visible_candidate(
    conn: sqlite3.Connection, candidate_id: int, actor: dict | None
) -> dict | None:
    gate, params = access.event_visibility_clause(conn, actor, alias="a")
    row = conn.execute(
        "SELECT c.*, a.actor_id FROM issue_review_candidates c "
        "JOIN activity a ON a.id = c.completion_event_id "
        "JOIN issues i ON i.id = c.issue_id "
        "WHERE c.id = ? AND a.imported_at IS NULL AND a.visibility_restricted = 0 "
        + (f"AND ({gate})" if gate else ""),
        [candidate_id, *params],
    ).fetchone()
    return dict(row) if row is not None else None


def prepare_submission(
    conn: sqlite3.Connection,
    *,
    issue: dict,
    actor: dict,
    submission: CandidateSubmission,
) -> None:
    if issue["archived_at"] is not None or issue["project_id"] is None:
        raise IssueCommandError(
            "conflict", "candidate submission requires an unarchived project issue"
        )
    latest = latest_candidate_id(conn, issue["id"])
    # R2: original-scope authority precedes EVERY equality comparison, including
    # correct guesses and explicit null. Never reveal a hidden predecessor ID.
    if latest is not None and visible_candidate(conn, latest, actor) is None:
        raise IssueCommandError("conflict", _PREDECESSOR_CONFLICT)
    if latest != submission.expected_previous_candidate_id:
        raise IssueCommandError("conflict", _PREDECESSOR_CONFLICT)


def prepare_review(
    conn: sqlite3.Connection, *, issue: dict, actor: dict, candidate_id: int
) -> None:
    candidate = visible_candidate(conn, candidate_id, actor)
    if candidate is None:
        raise IssueCommandError("not_found", "no such review candidate")
    source = issues.get_issue(conn, candidate["issue_id"])
    assert source is not None
    if (
        issue["archived_at"] is not None
        or source["archived_at"] is not None
        or issue["project_id"] is None
        or issue["project_id"] != source["project_id"]
        or issue["id"] == source["id"]
        or actor["id"] == candidate["actor_id"]
    ):
        raise IssueCommandError(
            "conflict",
            "candidate review requires distinct accounts and unarchived issues in one project",
        )
    if candidate_id != latest_candidate_id(conn, source["id"]):
        raise IssueCommandError("conflict", "review candidate is no longer current")


def insert_submission(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    event_id: int,
    submission: CandidateSubmission,
) -> dict:
    cur = conn.execute(
        "INSERT INTO issue_review_candidates (issue_id,completion_event_id,kind,source,revision,previous_candidate_id) "
        "VALUES (?,?,?,?,?,?)",
        (
            issue_id,
            event_id,
            submission.identity.kind,
            submission.identity.source,
            submission.identity.revision,
            submission.expected_previous_candidate_id,
        ),
    )
    return CandidateSubmissionReceipt.model_validate(
        {"candidate_id": cur.lastrowid, "completion_event_id": event_id}
    ).model_dump()


def insert_review(
    conn: sqlite3.Connection, *, issue_id: int, event_id: int, candidate_id: int
) -> dict:
    cur = conn.execute(
        "INSERT INTO issue_candidate_reviews (candidate_id,review_issue_id,completion_event_id) VALUES (?,?,?)",
        (candidate_id, issue_id, event_id),
    )
    return CandidateReviewReceipt.model_validate(
        {
            "review_id": cur.lastrowid,
            "candidate_id": candidate_id,
            "completion_event_id": event_id,
        }
    ).model_dump()
