"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from athena.aegis import comments, issues
from athena.core import users
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/issues", tags=["aegis"])

# Reject any status outside the lifecycle at the boundary (422), so bad input
# never reaches the DB. Built from the one canonical list in issues.py.
Status = Literal[issues.STATUSES]


class IssueCreate(BaseModel):
    title: str
    body: str = ""
    status: Status = "open"


class IssueUpdate(BaseModel):
    # A partial edit: send any subset. Unset fields are left unchanged. status
    # is still constrained to the lifecycle when present.
    title: str | None = None
    body: str | None = None
    status: Status | None = None


class AssigneeUpdate(BaseModel):
    # None clears the assignee (unassign); an int assigns to that user.
    assignee_id: int | None


class IssueOut(BaseModel):
    id: int
    title: str
    body: str
    status: str
    created_by: int
    created_at: str
    assignee_id: int | None = None
    assignee_name: str | None = None


class CommentCreate(BaseModel):
    body: str


class CommentOut(BaseModel):
    id: int
    issue_id: int
    author_id: int
    author_name: str
    body: str
    created_at: str


@router.post("", response_model=IssueOut, status_code=201)
def create(
    payload: IssueCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # created_by is the authenticated actor, never a value the caller supplied.
    return issues.create_issue(
        conn,
        title=payload.title,
        body=payload.body,
        status=payload.status,
        created_by=actor["id"],
    )


@router.get("", response_model=list[IssueOut])
def index(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    return issues.list_issues(conn)


@router.patch("/{issue_id}", response_model=IssueOut)
def update(
    issue_id: int,
    payload: IssueUpdate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may edit any issue (no per-issue ownership yet).
    # Only the fields the client actually sent are touched (exclude_unset).
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    title = payload.title.strip() if payload.title is not None else None
    if title is not None and not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    updated = issues.update_issue(
        conn, issue_id, title=title, body=payload.body, status=payload.status
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return updated


@router.put("/{issue_id}/assignee", response_model=IssueOut)
def set_assignee(
    issue_id: int,
    payload: AssigneeUpdate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Reject an unknown user here (422) rather than letting the FK raise a 500.
    # None is always valid — it means "unassign".
    if payload.assignee_id is not None and users.get_user(
        conn, payload.assignee_id
    ) is None:
        raise HTTPException(status_code=422, detail="no such user")
    updated = issues.set_assignee(conn, issue_id, payload.assignee_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return updated


@router.post("/{issue_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    issue_id: int,
    payload: CommentCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # author is the authenticated actor, never a caller-supplied field.
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    return comments.add_comment(
        conn, issue_id=issue_id, author_id=actor["id"], body=body
    )


@router.get("/{issue_id}/comments", response_model=list[CommentOut])
def list_comments(
    issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict]:
    if issues.get_issue(conn, issue_id) is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return comments.list_comments(conn, issue_id)
