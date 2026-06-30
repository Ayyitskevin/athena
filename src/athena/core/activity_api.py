"""The activity-log REST API.

A read-only window onto the audit trail (core/activity.py). Like search, the feed
spans every issue and actor at once, so it is a privileged cross-cutting read:
authentication is the gate (the same bar as listing users or searching). Writes
are never made here — activity rows are recorded as a side effect of the actions
that cause them, at those endpoints.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from athena.core import activity
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/activity", tags=["core"])

# How many rows one CSV export may return. Higher than the JSON feed's 200 cap
# because an operator/compliance export wants bulk; bounded so one request can't
# pull an unbounded trail into memory. Page the whole trail with before_id.
_EXPORT_MAX = 10000


class ActivityOut(BaseModel):
    id: int
    actor_id: int
    actor_name: str
    verb: str
    target_kind: str
    target_id: int
    detail: str
    created_at: str
    # The run this event belongs to (the X-Athena-Run it was recorded under), or
    # None for untagged actions; and the run that spawned that run (lineage), or None
    # at top level.
    run_id: str | None = None
    parent_run_id: str | None = None
    # The parent activity event this event's run forked from, if the caller used the
    # run-forking contract. NULL means ordinary run or unknown fork point.
    forked_from_event_id: int | None = None


class RunOut(BaseModel):
    # A reconstructed run: one actor's stretch of work, with the events (oldest-first)
    # so a consumer can replay the sequence it represents. run_id is the X-Athena-Run
    # the run's events share (deterministic run), or None for a gap-reconstructed one;
    # parent_run_id is the run that spawned it (lineage), or None at top level.
    actor_id: int
    actor_name: str
    run_id: str | None = None
    parent_run_id: str | None = None
    forked_from_event_id: int | None = None
    # True when the run may be clipped by the reconstruction window — its totals are a
    # lower bound; widen `limit` to see the rest. Only the oldest run can be partial.
    partial: bool = False
    started_at: str
    ended_at: str
    first_id: int
    last_id: int
    event_count: int
    events: list[ActivityOut]


class RunNodeOut(BaseModel):
    # One run in a lineage tree. The FOCAL run carries its full events (oldest-first,
    # replayable); ancestors/descendants are light (events empty — drill in by their
    # own run_id). `children` are the runs this one spawned (set on descendants only).
    actor_id: int
    actor_name: str
    run_id: str | None = None
    parent_run_id: str | None = None
    forked_from_event_id: int | None = None
    partial: bool = False
    started_at: str
    ended_at: str
    first_id: int
    last_id: int
    event_count: int
    events: list[ActivityOut] = []
    children: list["RunNodeOut"] = []


class RunLineageOut(BaseModel):
    # A run's place in the causal tree, reconstructed from the log: the originating
    # goal down to this run (ancestors, root-first), the run itself, and the runs it
    # spawned (descendants).
    run_id: str
    ancestors: list[RunNodeOut]
    run: RunNodeOut
    descendants: list[RunNodeOut]


class RunForkContractOut(BaseModel):
    # A read-only contract for starting a child run from a specific parent event.
    # The caller uses `headers` on its next writes; those events then become the fork.
    parent_run_id: str
    fork_run_id: str
    fork_from_event_id: int
    fork_from_event: ActivityOut
    shared_prefix_events: list[ActivityOut]
    shared_prefix_event_count: int
    shared_prefix_partial: bool
    headers: dict[str, str]


@router.get("", response_model=list[ActivityOut])
def feed(
    target_kind: str | None = Query(
        None, description="filter by kind; with target_id, one target's history"
    ),
    target_id: int | None = Query(None),
    actor_id: int | None = Query(None, description="filter to one actor's actions"),
    actor_type: Literal["agent", "human"] | None = Query(
        None, description="filter by actor type: agents only, or humans only"
    ),
    run_id: str | None = Query(
        None, description="replay one run: exactly the events tagged with this id"
    ),
    parent_run_id: str | None = Query(
        None, description="lineage: the events of the child runs this run spawned"
    ),
    verb: str | None = Query(None, description="filter to one event type"),
    q: str | None = Query(
        None, description="search actor, verb, target, detail, or timestamp text"
    ),
    before_id: int | None = Query(
        None, description="paging cursor: only events older than this id"
    ),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # target_id is meaningless without target_kind (an id with no idea what kind of
    # thing it is), so reject that half-query. target_kind alone is fine — it's a
    # valid feed filter ("all issue events").
    if target_id is not None and target_kind is None:
        raise HTTPException(
            status_code=422,
            detail="target_id requires target_kind",
        )
    return activity.list_activity(
        conn,
        target_kind=target_kind,
        target_id=target_id,
        actor_id=actor_id,
        actor_is_agent=None if actor_type is None else actor_type == "agent",
        run_id=run_id,
        parent_run_id=parent_run_id,
        verb=verb,
        search=q,
        before_id=before_id,
        limit=limit,
        actor=actor,
    )


@router.get("/export.csv")
def export_csv(
    target_kind: str | None = Query(None, description="filter by kind"),
    target_id: int | None = Query(None),
    actor_id: int | None = Query(None, description="filter to one actor's actions"),
    actor_type: Literal["agent", "human"] | None = Query(
        None, description="filter by actor type: agents only, or humans only"
    ),
    run_id: str | None = Query(None, description="only events tagged with this run"),
    parent_run_id: str | None = Query(
        None, description="lineage: events of the child runs this run spawned"
    ),
    verb: str | None = Query(None, description="filter to one event type"),
    q: str | None = Query(None, description="search actor, verb, target, or detail"),
    before_id: int | None = Query(
        None, description="paging cursor: only events older than this id"
    ),
    limit: int = Query(1000, ge=1, le=_EXPORT_MAX),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    # The audit trail as a CSV download — the REST twin of the web Activity page's
    # export, for compliance/archival tooling. Same authenticated gate and the SAME
    # filter set as the JSON feed, over the same data-access read, so the two can
    # never disagree on what matches; only the representation (CSV) and the higher
    # row cap differ. Page the full trail by passing the oldest id back as before_id.
    if target_id is not None and target_kind is None:
        raise HTTPException(status_code=422, detail="target_id requires target_kind")
    rows = activity.list_activity(
        conn,
        target_kind=target_kind,
        target_id=target_id,
        actor_id=actor_id,
        actor_is_agent=None if actor_type is None else actor_type == "agent",
        run_id=run_id,
        parent_run_id=parent_run_id,
        verb=verb,
        search=q,
        before_id=before_id,
        limit=limit,
        actor=actor,
    )
    return Response(
        activity.to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="athena-activity.csv"'},
    )


@router.get("/runs", response_model=list[RunOut])
def runs(
    actor_id: int = Query(..., description="reconstruct this actor's runs"),
    gap_seconds: int = Query(
        1800,
        ge=1,
        le=86400,
        description="max seconds between consecutive events within one run",
    ),
    limit: int = Query(
        200, ge=1, le=500, description="how many recent events to reconstruct from"
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # A reading lens over the trail: group one actor's recent events into work
    # sessions (runs). actor_id is required — a "runs" feed mixing actors would be
    # meaningless, since a run is by definition one actor's uninterrupted stretch.
    return activity.reconstruct_runs(
        conn, actor_id=actor_id, gap_seconds=gap_seconds, limit=limit, actor=actor
    )


@router.get("/runs/{run_id}/lineage", response_model=RunLineageOut)
def run_lineage(
    run_id: str,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The causal tree of one tagged run: the originating goal down to it (ancestors),
    # the run with its events, and the runs it spawned (descendants) — a pure
    # projection of run_id/parent_run_id over the log. Visibility-gated by the actor
    # (events on targets they can't see are dropped); a run with no visible events is a
    # 404, indistinguishable from one that never existed.
    lineage = activity.run_lineage(conn, run_id, actor=actor)
    if lineage is None:
        raise HTTPException(status_code=404, detail="no such run")
    return lineage


@router.get("/runs/{run_id}/fork", response_model=RunForkContractOut)
def run_fork_contract(
    run_id: str,
    from_event_id: int = Query(
        ...,
        ge=1,
        description="event id inside the parent run where the child should branch",
    ),
    fork_run_id: str = Query(
        ...,
        min_length=1,
        max_length=200,
        description="client-chosen id for the new child run",
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # This creates no state. It validates the visible fork point and returns the
    # exact headers a client should put on subsequent writes to make the child run
    # replayable and lineage-linked from this parent event.
    try:
        contract = activity.run_fork_contract(
            conn,
            run_id,
            fork_from_event_id=from_event_id,
            fork_run_id=fork_run_id,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if contract is None:
        raise HTTPException(status_code=404, detail="no such fork point")
    return contract
