"""REST projection for an authenticated actor's delegated-work inbox."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from athena.aegis import delegations
from athena.core import tokens
from athena.core.deps import get_conn
from athena.core.identity import current_actor, require_token_scope


router = APIRouter(prefix="/delegations", tags=["aegis"])


class DelegationSubjectOut(BaseModel):
    id: int
    name: str
    is_agent: bool


class DelegatorOut(BaseModel):
    id: int
    name: str


class LabelOut(BaseModel):
    id: int
    name: str
    color: str


class DelegatedIssueOut(BaseModel):
    id: int
    key: str | None
    title: str
    body: str
    status: str
    status_category: Literal["todo", "doing", "done"]
    # Match the canonical IssueOut boundary: legacy/imported databases may contain
    # priorities predating today's validated write vocabulary.
    priority: str
    project_id: int | None
    project_name: str | None
    assignee_id: int | None
    assignee_name: str | None
    labels: list[LabelOut]


class VisibleBlockerOut(BaseModel):
    id: int
    key: str | None
    title: str
    status: str


DelegationWarning = Literal[
    "empty_description",
    "no_accountable_assignee",
    "subject_cannot_see_issue",
    "visible_open_blockers",
]


class DelegationItemOut(BaseModel):
    issue: DelegatedIssueOut
    delegated_at: str
    delegated_by: DelegatorOut
    visible_to_subject: bool
    visible_open_blockers: list[VisibleBlockerOut]
    visible_open_blocker_count: int
    visible_open_blockers_truncated: bool
    warnings: list[DelegationWarning]


class DelegationInboxOut(BaseModel):
    subject: DelegationSubjectOut
    items: list[DelegationItemOut]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    blocker_scope: Literal["visible_to_subject"]


@router.get("/me", response_model=DelegationInboxOut)
def my_delegations(
    include_closed: bool = False,
    limit: int = Query(delegations.DEFAULT_LIMIT, ge=1, le=delegations.MAX_LIMIT),
    offset: int = Query(0, ge=0, le=delegations.MAX_OFFSET),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """List work where the current actor is a contributor.

    This is pickup context, not a claim, lease, progress report, or liveness signal.
    Blocker data is restricted to issues the same actor may see.
    """
    require_token_scope(actor, tokens.READ_SCOPE)
    return delegations.list_delegations(
        conn,
        actor,
        include_closed=include_closed,
        limit=limit,
        offset=offset,
    )
