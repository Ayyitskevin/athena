"""Closed response shapes for the compact, advisory project read."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Excerpt(_StrictModel):
    text: str
    total_chars: int
    clipped: bool


class Person(_StrictModel):
    id: int
    name: Excerpt


class Issue(_StrictModel):
    id: int
    key: str | None
    title: Excerpt
    status: Excerpt
    status_category: Literal["todo", "doing", "done"] | None
    priority: str
    assignee: Person | None
    work_context_url: str


class BlockerIssue(Issue):
    project_id: int | None = None
    archived_at: str | None


class ActiveClaim(_StrictModel):
    issue: Issue
    holder: Person
    expires_at: str


class BlockerEdge(_StrictModel):
    issue: Issue
    blocked_by: BlockerIssue


class OpenHandoff(_StrictModel):
    issue: Issue
    reason: Literal["needs_input", "blocked", "capacity"]
    blocking_question: Excerpt
    yield_event_id: int
    yielded_at: str
    yielded_by: Person


class LatestCompletion(_StrictModel):
    issue: Issue
    event_id: int
    reported_at: str
    reported_by: Person
    report_available: bool
    reported_verdict: Literal["PASS", "PASS-WITH-NOTES", "HOLD", "BLOCKED"] | None
    reported_findings: dict[Literal["P1", "P2", "P3"], int] | None


T = TypeVar("T")


class Group(_StrictModel, Generic[T]):
    items: list[T]
    visible_total: int
    returned: int
    limit: int
    clipped: bool


class Project(_StrictModel):
    id: int
    key: str
    name: Excerpt


class Summary(_StrictModel):
    open_issue_count: int
    unassigned_open_issue_count: int


class Semantics(_StrictModel):
    snapshot: Literal["current_visible_snapshot"]
    group_counts: Literal["independent scopes; not additive"]
    active_claims: Literal["live possession, not process liveness"]
    blockers: Literal["visible edges only; zero is not readiness"]
    open_handoffs: Literal["awaiting resume, not unanswered comments"]
    latest_completions: Literal["advisory historical actor reports, not verification"]
    authority: Literal["read only; no state change or authority granted"]


class ProjectBrief(_StrictModel):
    schema_: Literal["athena.project_brief.v1"] = Field(alias="schema")
    scope: Literal["visible_to_request_actor"]
    observed_at: str
    project: Project
    summary: Summary
    active_claims: Group[ActiveClaim]
    blockers: Group[BlockerEdge]
    open_handoffs: Group[OpenHandoff]
    latest_completions: Group[LatestCompletion]
    semantics: Semantics
