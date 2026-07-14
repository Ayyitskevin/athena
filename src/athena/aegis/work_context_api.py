"""REST adapter for an issue's actor-visible agent work context.

The projection in :mod:`athena.aegis.work_context` owns composition, visibility,
clipping, and snapshot semantics.  This module only exposes that projection over
HTTP and whitelists its public contract with explicit response models.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from athena.aegis import work_context
from athena.core.deps import get_conn
from athena.core.identity import optional_actor

router = APIRouter(prefix="/issues", tags=["aegis"])

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, X-Athena-Actor",
}


class TextExcerptOut(BaseModel):
    text: str
    total_chars: int
    truncated: bool


class LabelOut(BaseModel):
    id: int
    name: str
    color: str


class IssueSummaryOut(BaseModel):
    id: int
    key: str | None
    title: str
    description: TextExcerptOut
    status: str
    status_category: str | None
    priority: str
    project_id: int | None
    project_name: str | None
    assignee_id: int | None
    assignee_name: str | None
    created_by: int
    created_at: str
    archived_at: str | None
    labels: list[LabelOut]


class RelatedIssueOut(BaseModel):
    id: int
    key: str | None
    title: str
    status: str
    status_category: str | None
    priority: str
    project_id: int | None
    project_name: str | None
    archived_at: str | None


class RelatedIssueGroupOut(BaseModel):
    items: list[RelatedIssueOut]
    visible_total: int
    clipped: bool


class HierarchyOut(BaseModel):
    parent: RelatedIssueOut | None
    children: RelatedIssueGroupOut


class DependenciesOut(BaseModel):
    open_blockers: RelatedIssueGroupOut
    blocked_by: RelatedIssueGroupOut
    blocks: RelatedIssueGroupOut
    relates: RelatedIssueGroupOut


class ContributorOut(BaseModel):
    user_id: int
    name: str
    is_agent: bool
    added_by: int | None
    added_at: str


class ContributorGroupOut(BaseModel):
    items: list[ContributorOut]
    visible_total: int
    clipped: bool


class AttachmentOut(BaseModel):
    id: int
    filename: str
    content_type: str
    byte_size: int
    sha256: str
    uploaded_by: int
    created_at: str


class AttachmentGroupOut(BaseModel):
    items: list[AttachmentOut]
    visible_total: int
    clipped: bool


class CommentOut(BaseModel):
    id: int
    author_id: int
    author_name: str
    body: TextExcerptOut
    created_at: str


class CommentGroupOut(BaseModel):
    items: list[CommentOut]
    visible_total: int
    clipped: bool


class OutgoingReferenceOut(BaseModel):
    kind: Literal["issue", "page"]
    id: int
    title: str | None
    issue_key: str | None
    status: str | None
    status_category: str | None
    priority: str | None
    space_id: int | None
    space_key: str | None
    space_name: str | None
    body: TextExcerptOut | None


class OutgoingReferenceGroupOut(BaseModel):
    items: list[OutgoingReferenceOut]
    visible_total: int
    clipped: bool


class BacklinkOut(BaseModel):
    kind: Literal["issue", "page"]
    id: int
    title: str | None
    issue_key: str | None
    space_key: str | None


class BacklinkGroupOut(BaseModel):
    items: list[BacklinkOut]
    visible_total: int
    clipped: bool


class ReferencesOut(BaseModel):
    outgoing: OutgoingReferenceGroupOut
    backlinks: BacklinkGroupOut


class ActivityOut(BaseModel):
    id: int
    actor_id: int
    actor_name: str
    verb: str
    target_kind: str
    target_id: int
    detail: TextExcerptOut
    created_at: str
    run_id: str | None
    parent_run_id: str | None
    forked_from_event_id: int | None
    imported_at: str | None


class ActivityGroupOut(BaseModel):
    items: list[ActivityOut]
    visible_total: int
    clipped: bool


AssertionNotMade = Literal[
    "claimed",
    "ready",
    "unblocked",
    "running",
    "live",
    "replay_safe",
]


class WorkContextSemanticsOut(BaseModel):
    snapshot: Literal["current_visible_state"]
    does_not_assert: list[AssertionNotMade]


class IssueWorkContextOut(BaseModel):
    # ``schema`` is a BaseModel method name, so keep a safe Python attribute while
    # accepting and serializing the exact public JSON key.
    schema_: Literal["athena.issue_work_context.v1"] = Field(alias="schema")
    scope: Literal["visible_to_request_actor"]
    semantics: WorkContextSemanticsOut
    issue: IssueSummaryOut
    issue_etag: str
    warnings: list[str]
    hierarchy: HierarchyOut
    dependencies: DependenciesOut
    contributors: ContributorGroupOut
    attachments: AttachmentGroupOut
    comments: CommentGroupOut
    references: ReferencesOut
    recent_activity: ActivityGroupOut


@router.get("/{ref}/work-context", response_model=IssueWorkContextOut)
def show_issue_work_context(
    ref: str,
    response: Response,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Return one visibility-safe, bounded snapshot for an agent or operator."""
    payload = work_context.build_work_context(conn, ref, actor=actor)
    if payload is None:
        # A hidden issue is deliberately indistinguishable from a missing one.
        raise HTTPException(
            status_code=404,
            detail="no such issue",
            headers=_PRIVATE_HEADERS,
        )
    response.headers.update(_PRIVATE_HEADERS)
    response.headers["ETag"] = work_context.context_etag(payload)
    return payload
