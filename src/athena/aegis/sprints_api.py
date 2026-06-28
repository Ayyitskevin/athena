"""The sprints REST API.

Sprints are a project's iterations, so the management endpoints are gated like the
project itself: reading is open (like listing issues), but creating/editing/running
a sprint is the PROJECT CREATOR's call — sprints shape how a project runs. The
lifecycle transitions (start/complete) surface the data layer's SprintStateError as
a 409. The issue↔sprint assignment lives on the issues router (aegis/api.py), since
it's a write on the issue, not the sprint.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from athena.aegis import projects, sprints
from athena.core import access
from athena.core.deps import get_conn
from athena.core.identity import issue_write_actor, optional_actor

router = APIRouter(tags=["aegis"])


class SprintOut(BaseModel):
    id: int
    project_id: int
    name: str
    goal: str
    state: str
    start_date: str | None = None
    end_date: str | None = None
    created_at: str


class SprintCreate(BaseModel):
    name: str
    goal: str = ""
    start_date: str | None = None
    end_date: str | None = None


class SprintEdit(BaseModel):
    # Partial edit; only fields actually sent are written (exclude_unset). A date may
    # be sent as null to clear it. State is not edited here — it moves via start/complete.
    name: str | None = None
    goal: str | None = None
    start_date: str | None = None
    end_date: str | None = None


def _project_for_sprint_write(
    conn: sqlite3.Connection, project_id: int, actor: dict
) -> dict:
    """The project whose sprints the actor may manage, or raise: 404 if no such
    project, 403 if the actor isn't its creator. Managing a project's cadence is the
    creator's call, matching project edit/delete."""
    project = projects.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    if project["created_by"] != actor["id"]:
        raise HTTPException(
            status_code=403, detail="only the project creator may manage its sprints"
        )
    return project


def _sprint_for_write(conn: sqlite3.Connection, sprint_id: int, actor: dict) -> dict:
    """The sprint the actor may manage (404 if missing, 403 if not the project
    creator)."""
    sprint = sprints.get_sprint(conn, sprint_id)
    if sprint is None:
        raise HTTPException(status_code=404, detail="no such sprint")
    _project_for_sprint_write(conn, sprint["project_id"], actor)
    return sprint


@router.get("/projects/{project_id}/sprints", response_model=list[SprintOut])
def list_project_sprints(
    project_id: int,
    state: str | None = Query(None, description="filter to one state"),
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading a project's sprints is open, like listing its issues — but a private
    # project the caller can't see is a 404, indistinguishable from a missing one.
    if not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    if state is not None and state not in sprints.STATES:
        raise HTTPException(
            status_code=422, detail=f"state must be one of: {', '.join(sprints.STATES)}"
        )
    return sprints.list_sprints(conn, project_id=project_id, state=state)


@router.post(
    "/projects/{project_id}/sprints", response_model=SprintOut, status_code=201
)
def create_sprint(
    project_id: int,
    payload: SprintCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _project_for_sprint_write(conn, project_id, actor)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="sprint name is required")
    return sprints.create_sprint(
        conn,
        project_id=project_id,
        name=name,
        goal=payload.goal,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )


@router.get("/sprints/{sprint_id}", response_model=SprintOut)
def show_sprint(
    sprint_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    sprint = sprints.get_sprint(conn, sprint_id)
    # A sprint in a private project the caller can't see is a 404 — gated by its
    # project, so neither the sprint nor the project's existence leaks.
    if sprint is None or not access.can_see_project(conn, actor, sprint["project_id"]):
        raise HTTPException(status_code=404, detail="no such sprint")
    return sprint


@router.patch("/sprints/{sprint_id}", response_model=SprintOut)
def update_sprint(
    sprint_id: int,
    payload: SprintEdit,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _sprint_for_write(conn, sprint_id, actor)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="sprint name cannot be empty")
        fields["name"] = name
    updated = sprints.update_sprint(conn, sprint_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="no such sprint")
    return updated


@router.post("/sprints/{sprint_id}/start", response_model=SprintOut)
def start_sprint(
    sprint_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _sprint_for_write(conn, sprint_id, actor)
    try:
        return sprints.start_sprint(conn, sprint_id)
    except sprints.SprintStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sprints/{sprint_id}/complete", response_model=SprintOut)
def complete_sprint(
    sprint_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _sprint_for_write(conn, sprint_id, actor)
    try:
        return sprints.complete_sprint(conn, sprint_id)
    except sprints.SprintStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/sprints/{sprint_id}", status_code=204)
def delete_sprint(
    sprint_id: int,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    _sprint_for_write(conn, sprint_id, actor)
    # Refuse rather than detach: a sprint that still holds issues must be emptied
    # first (move them to the backlog or another sprint), mirroring project delete.
    if sprints.count_issues_in_sprint(conn, sprint_id) > 0:
        raise HTTPException(
            status_code=409, detail="move its issues out of the sprint first"
        )
    sprints.delete_sprint(conn, sprint_id)
