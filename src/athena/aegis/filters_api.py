"""The saved-filters REST API — a user's named, reusable issue queries.

Filters are PERSONAL (your queries), like the notification inbox, so every route
requires only an authenticated actor — not the issue-write role — and is scoped to
the acting user: you can only see, run, change, or delete your OWN filters, and a
filter that isn't yours reads as 404 (we don't reveal that it exists).

Running a filter returns issues through the SAME shape and label-composition the
issue endpoints use (api.IssueOut / api._with_labels_many), so a saved query and an
ad-hoc one are indistinguishable to the client.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from athena.aegis import api, issues, saved_filters
from athena.core import access
from athena.core.ids import RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import current_actor, personal_write_actor

router = APIRouter(prefix="/filters", tags=["aegis"])

SavedAssigneeId = Annotated[
    int,
    Field(strict=True, ge=0, le=issues.MAX_SQLITE_INTEGER),
]


class FilterCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Every dimension optional; an all-unset object is a legal "match everything"
    # filter. priority/project are semantically validated in the handler (via
    # saved_filters.validate_criteria) so the API and the web form reject the same
    # values the same way; the rest are free-form (an unknown value matches none).
    status: str | None = None
    priority: str | None = None
    assignee_id: SavedAssigneeId | None = None
    label: str | None = None
    project: str | None = None
    search: str | None = None
    # A work-query string. Mutually exclusive with the dimensions above (rejected
    # by validate_criteria, not merged) so a saved filter is one thing or the
    # other and never a surprising intersection. Parsed at write time, so a filter
    # that cannot run cannot be stored.
    query: str | None = None


class FilterCreate(BaseModel):
    name: str
    criteria: FilterCriteria = FilterCriteria()


class FilterUpdate(BaseModel):
    # Partial: send name and/or criteria. An omitted field is left unchanged; a
    # criteria sent as {} clears every constraint ("match everything").
    name: str | None = None
    criteria: FilterCriteria | None = None


class FilterOut(BaseModel):
    id: int
    owner_id: int
    name: str
    # None distinguishes a malformed/non-object stored payload from valid {}.
    criteria: dict | None
    created_at: str
    updated_at: str


def _clean_criteria(payload: FilterCriteria) -> dict:
    """Normalize a submitted criteria model and validate it, or raise 422. Returns
    the canonical stored form — the one place the JSON API turns criteria input
    into something safe to persist and run."""
    try:
        criteria = saved_filters.normalize_criteria(
            payload.model_dump(exclude_none=True)
        )
    except saved_filters.InvalidFilterCriteria as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    reason = saved_filters.validate_criteria(criteria)
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)
    return criteria


def _owned_filter_or_404(conn: sqlite3.Connection, filter_id: int, actor: dict) -> dict:
    """Fetch a filter that belongs to the actor, or raise 404. A filter owned by
    someone else is indistinguishable from a missing one — a user can't probe
    another's saved queries."""
    flt = saved_filters.get_filter(conn, filter_id)
    if flt is None or flt["owner_id"] != actor["id"]:
        raise HTTPException(status_code=404, detail="no such filter")
    return flt


@router.post("", response_model=FilterOut, status_code=201)
def create(
    payload: FilterCreate,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="filter name is required")
    criteria = _clean_criteria(payload.criteria)
    try:
        return saved_filters.create_filter(
            conn, owner_id=actor["id"], name=name, criteria=criteria
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="you already have a filter with that name"
        ) from exc


@router.get("", response_model=list[FilterOut])
def index(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    return saved_filters.list_filters(conn, actor["id"])


@router.get("/{filter_id}", response_model=FilterOut)
def show(
    filter_id: RowIdPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    return _owned_filter_or_404(conn, filter_id, actor)


@router.patch("/{filter_id}", response_model=FilterOut)
def update(
    filter_id: RowIdPath,
    payload: FilterUpdate,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    name = payload.name.strip() if payload.name is not None else None
    if name is not None and not name:
        raise HTTPException(status_code=422, detail="filter name cannot be empty")
    criteria = (
        _clean_criteria(payload.criteria) if payload.criteria is not None else None
    )
    try:
        # Ownership is enforced IN the mutation's SQL (owner-scoped WHERE), not
        # by a fetch-then-check here: 0 rows means missing-or-not-yours, and no
        # rowid-reuse race can land this write on another user's filter.
        updated = saved_filters.update_filter(
            conn, filter_id, owner_id=actor["id"], name=name, criteria=criteria
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="you already have a filter with that name"
        ) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="no such filter")
    return updated


@router.delete("/{filter_id}", status_code=204)
def delete(
    filter_id: RowIdPath,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    if not saved_filters.delete_filter(conn, filter_id, owner_id=actor["id"]):
        raise HTTPException(status_code=404, detail="no such filter")


@router.get("/{filter_id}/issues", response_model=list[api.IssueOut])
def run(
    filter_id: RowIdPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    flt = _owned_filter_or_404(conn, filter_id, actor)
    rows = saved_filters.run_filter(
        conn,
        flt["criteria"],
        visible_project_ids=access.visible_project_filter(conn, actor),
        # The runner's identity, so a saved `assignee:@me` resolves to whoever is
        # running it — the reason to save that query rather than a fixed user id.
        actor=actor,
    )
    return api._with_labels_many(conn, rows)
