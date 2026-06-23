"""The Aegis REST API: issue endpoints.

Pydantic models validate the request body before our code runs (bad input ->
422 automatically). The router is mounted by main.py.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from athena.aegis import comments, issues, labels, projects
from athena.core import users
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/issues", tags=["aegis"])
# Labels are a top-level resource (shared vocabulary), not nested under an issue,
# so they get their own router. Attaching a label TO an issue is a sub-resource
# of /issues and lives on `router` below.
labels_router = APIRouter(prefix="/labels", tags=["aegis"])
# Projects are a top-level resource too (a container issues belong to), so they
# get their own router. Setting an issue's project is a sub-resource of /issues
# and lives on `router` below.
projects_router = APIRouter(prefix="/projects", tags=["aegis"])

# Reject any status outside the lifecycle at the boundary (422), so bad input
# never reaches the DB. Built from the one canonical list in issues.py.
Status = Literal[issues.STATUSES]
Priority = Literal[issues.PRIORITIES]


class IssueCreate(BaseModel):
    title: str
    body: str = ""
    status: Status = "open"
    priority: Priority = "medium"
    project_id: int | None = None


class IssueUpdate(BaseModel):
    # A partial edit: send any subset. Unset fields are left unchanged. status
    # and priority are still constrained to their lifecycles when present.
    title: str | None = None
    body: str | None = None
    status: Status | None = None
    priority: Priority | None = None


class AssigneeUpdate(BaseModel):
    # None clears the assignee (unassign); an int assigns to that user.
    assignee_id: int | None


class ProjectUpdate(BaseModel):
    # None removes the issue from its project; an int moves it into that project.
    project_id: int | None


class ProjectCreate(BaseModel):
    name: str
    description: str = ""


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    created_by: int
    created_at: str


class LabelCreate(BaseModel):
    name: str
    color: str = "#6b7280"


class LabelOut(BaseModel):
    id: int
    name: str
    color: str


class LabelAttach(BaseModel):
    label_id: int


class IssueOut(BaseModel):
    id: int
    title: str
    body: str
    status: str
    priority: str
    created_by: int
    created_at: str
    assignee_id: int | None = None
    assignee_name: str | None = None
    project_id: int | None = None
    project_name: str | None = None
    labels: list[LabelOut] = []


class CommentCreate(BaseModel):
    body: str


class CommentOut(BaseModel):
    id: int
    issue_id: int
    author_id: int
    author_name: str
    body: str
    created_at: str


def _with_labels(conn: sqlite3.Connection, issue: dict) -> dict:
    """Attach the issue's labels under a "labels" key. Issues own their core row
    (issues.py); labels are composed on here so the two modules stay in their
    lanes and reads still come back as one object for the client."""
    issue["labels"] = labels.labels_for_issue(conn, issue["id"])
    return issue


def _with_labels_many(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Same as _with_labels but for a list, using one bulk query (no N+1)."""
    by_issue = labels.labels_for_issues(conn, [r["id"] for r in rows])
    for r in rows:
        r["labels"] = by_issue.get(r["id"], [])
    return rows


@router.post("", response_model=IssueOut, status_code=201)
def create(
    payload: IssueCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # created_by is the authenticated actor, never a value the caller supplied.
    # Reject a blank title at the boundary (422) so the API matches the web form,
    # which already strips and rejects empties — persist the stripped value.
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    # Reject an unknown project here (422) rather than letting the FK raise a 500.
    if payload.project_id is not None and projects.get_project(
        conn, payload.project_id
    ) is None:
        raise HTTPException(status_code=422, detail="no such project")
    issue = issues.create_issue(
        conn,
        title=title,
        body=payload.body,
        status=payload.status,
        priority=payload.priority,
        project_id=payload.project_id,
        created_by=actor["id"],
    )
    return _with_labels(conn, issue)


@router.get("", response_model=list[IssueOut])
def index(
    status: str | None = None,
    label: str | None = None,
    search: str | None = None,
    project: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Optional filters, same semantics the web list uses (one shared path in
    # issues.list_issues). A label name is resolved to issue ids by labels.py so
    # issues.py stays decoupled from the join; an unknown label matches nothing.
    # project is a direct column on the issue, so it's filtered by id here.
    ids = labels.issue_ids_for_label(conn, label) if label else None
    rows = issues.list_issues(
        conn, status=status, search=search, project_id=project, ids=ids
    )
    return _with_labels_many(conn, rows)


@router.get("/{issue_id}", response_model=IssueOut)
def show(
    issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    # Reads are open to everyone (no actor), same as the list endpoint — only
    # writes pass through the creator-or-assignee gate.
    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return _with_labels(conn, issue)


def _issue_for_write(
    conn: sqlite3.Connection, issue_id: int, actor: dict
) -> dict:
    """Fetch an issue the actor is allowed to MODIFY, or raise: 404 if no such
    issue, 403 if the actor is neither its creator nor its current assignee.
    Centralizes the issue write-authorization rule (issues.can_modify) so every
    write path (status/edit/assign) enforces it identically. Reads and comments
    do not go through here."""
    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="no such issue")
    if not issues.can_modify(issue, actor["id"]):
        raise HTTPException(
            status_code=403,
            detail="only the issue creator or assignee may modify it",
        )
    return issue


@router.patch("/{issue_id}", response_model=IssueOut)
def update(
    issue_id: int,
    payload: IssueUpdate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator or assignee only (404 if missing, 403 if not permitted).
    _issue_for_write(conn, issue_id, actor)
    # Only the fields the client actually sent are touched (exclude_unset).
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    title = payload.title.strip() if payload.title is not None else None
    if title is not None and not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    updated = issues.update_issue(
        conn,
        issue_id,
        title=title,
        body=payload.body,
        status=payload.status,
        priority=payload.priority,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such issue")
    return _with_labels(conn, updated)


@router.put("/{issue_id}/assignee", response_model=IssueOut)
def set_assignee(
    issue_id: int,
    payload: AssigneeUpdate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator or assignee only (404 if missing, 403 if not permitted). Checked
    # against the CURRENT assignee — so an unassigned issue can only be assigned
    # by its creator, and an assignee may reassign or unassign themselves.
    _issue_for_write(conn, issue_id, actor)
    # Reject an unknown user here (422) rather than letting the FK raise a 500.
    # None is always valid — it means "unassign".
    if payload.assignee_id is not None and users.get_user(
        conn, payload.assignee_id
    ) is None:
        raise HTTPException(status_code=422, detail="no such user")
    return _with_labels(conn, issues.set_assignee(conn, issue_id, payload.assignee_id))


@router.put("/{issue_id}/project", response_model=IssueOut)
def set_project(
    issue_id: int,
    payload: ProjectUpdate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Moving an issue between projects is a write — creator-or-assignee only
    # (404 if missing, 403 if not permitted), same gate as status/assign/labels.
    _issue_for_write(conn, issue_id, actor)
    # Reject an unknown project here (422) rather than letting the FK raise a 500.
    # None is always valid — it means "remove from project".
    if payload.project_id is not None and projects.get_project(
        conn, payload.project_id
    ) is None:
        raise HTTPException(status_code=422, detail="no such project")
    return _with_labels(conn, issues.set_project(conn, issue_id, payload.project_id))


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


def _author_comment_or_error(
    conn: sqlite3.Connection, issue_id: int, comment_id: int, actor: dict
) -> dict:
    """Fetch a comment that belongs to this issue, requiring the actor to be its
    author. Raises 404 if the comment is missing or hangs off another issue, 403
    if someone other than the author tries to change it. This author-ownership
    check is the one place we enforce per-row ownership today (issues themselves
    are still 'any authenticated actor' — a separate, deferred design)."""
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
        raise HTTPException(status_code=404, detail="no such comment")
    if existing["author_id"] != actor["id"]:
        raise HTTPException(status_code=403, detail="not the comment author")
    return existing


@router.patch("/{issue_id}/comments/{comment_id}", response_model=CommentOut)
def edit_comment(
    issue_id: int,
    comment_id: int,
    payload: CommentCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _author_comment_or_error(conn, issue_id, comment_id, actor)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    return comments.update_comment(conn, comment_id, body=body)


@router.delete("/{issue_id}/comments/{comment_id}", status_code=204)
def delete_comment(
    issue_id: int,
    comment_id: int,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    _author_comment_or_error(conn, issue_id, comment_id, actor)
    comments.delete_comment(conn, comment_id)


# --- Projects: a top-level grouping of issues -----------------------------


@projects_router.get("", response_model=list[ProjectOut])
def list_all_projects(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    # Reading the project list is open, like listing issues.
    return projects.list_projects(conn)


@projects_router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may create a project (like creating a label).
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="project name is required")
    if projects.get_project_by_name(conn, name) is not None:
        raise HTTPException(status_code=409, detail="project already exists")
    return projects.create_project(
        conn, name=name, description=payload.description, created_by=actor["id"]
    )


@projects_router.get("/{project_id}", response_model=ProjectOut)
def show_project(
    project_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    project = projects.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    return project


# --- Labels: a top-level shared vocabulary --------------------------------


@labels_router.get("", response_model=list[LabelOut])
def list_all_labels(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    # Reading the vocabulary is open, like listing issues.
    return labels.list_labels(conn)


@labels_router.post("", response_model=LabelOut, status_code=201)
def create_label(
    payload: LabelCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may add to the shared vocabulary (like commenting).
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="label name is required")
    if labels.get_label_by_name(conn, name) is not None:
        raise HTTPException(status_code=409, detail="label already exists")
    return labels.create_label(conn, name=name, color=payload.color)


# --- Labels on an issue: a write, so creator-or-assignee gated -------------


@router.post("/{issue_id}/labels", response_model=IssueOut, status_code=201)
def attach_label(
    issue_id: int,
    payload: LabelAttach,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Changing an issue's labels is a write — same gate as status/assign.
    issue = _issue_for_write(conn, issue_id, actor)
    if labels.get_label(conn, payload.label_id) is None:
        raise HTTPException(status_code=422, detail="no such label")
    labels.add_label_to_issue(conn, issue_id, payload.label_id)  # idempotent
    return _with_labels(conn, issue)


@router.delete("/{issue_id}/labels/{label_id}", response_model=IssueOut)
def detach_label(
    issue_id: int,
    label_id: int,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    issue = _issue_for_write(conn, issue_id, actor)
    if not labels.remove_label_from_issue(conn, issue_id, label_id):
        raise HTTPException(status_code=404, detail="label not on this issue")
    return _with_labels(conn, issue)
