"""Closed bounded response shapes; all review states remain advisory."""

from typing import Literal

from pydantic import Field

from athena.aegis.project_brief_models import Excerpt, Person
from athena.aegis.review_models import CandidateIdentity, StrictReviewModel


class ReviewSourceIssue(StrictReviewModel):
    id: int
    key: str | None
    title: Excerpt
    status: Excerpt
    status_category: Literal["todo", "doing", "done"] | None
    work_context_url: str


class ReviewIssue(StrictReviewModel):
    id: int
    key: str | None
    title: Excerpt
    work_context_url: str


class SubmittedCandidate(StrictReviewModel):
    candidate_id: int
    identity: CandidateIdentity
    completion_event_id: int
    reported_at: str
    submitted_by: Person


class LatestCandidateReview(StrictReviewModel):
    review_id: int
    review_issue: ReviewIssue
    completion_event_id: int
    reported_at: str
    reported_by: Person
    report_available: bool
    reported_verdict: Literal["PASS", "PASS-WITH-NOTES", "HOLD", "BLOCKED"] | None
    reported_findings: dict[Literal["P1", "P2", "P3"], int] | None


class ReviewQueueItem(StrictReviewModel):
    issue: ReviewSourceIssue
    candidate: SubmittedCandidate
    review_state: Literal[
        "awaiting_review", "reported_pass", "reported_hold", "unknown"
    ]
    latest_review: LatestCandidateReview | None


class HistoricalCandidate(ReviewQueueItem):
    is_current: bool


class CandidateHistory(StrictReviewModel):
    items: list[HistoricalCandidate]
    visible_total: int
    returned: int
    limit: Literal[5]
    clipped: bool


class IssueReviewTracking(StrictReviewModel):
    current_candidate: ReviewQueueItem
    recent_candidates: CandidateHistory


class ReviewProject(StrictReviewModel):
    id: int
    key: str
    name: Excerpt


class ProjectReviewQueue(StrictReviewModel):
    schema_name: Literal["athena.project_review_queue.v1"] = Field(alias="schema")
    scope: Literal["visible_to_request_actor"]
    observed_at: str
    project: ReviewProject
    items: list[ReviewQueueItem]
    visible_total: int
    returned: int
    limit: int
    clipped: bool
    semantics: dict[str, str]
