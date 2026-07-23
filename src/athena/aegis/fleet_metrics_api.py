"""Strict REST adapter for the visibility-safe fleet metrics projection."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from athena.aegis import fleet_metrics
from athena.core import identity, tokens
from athena.core.deps import get_conn


router = APIRouter(prefix="/fleet", tags=["aegis"])

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, X-Athena-Actor",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PeriodOut(_StrictModel):
    start: str
    end_exclusive: str
    timezone: Literal["UTC"]
    boundary: Literal["[start,end)"]
    days: int


class FiltersOut(_StrictModel):
    project_id: int | None
    actor_id: int | None


class FlowOut(_StrictModel):
    created: int
    completed: int
    net: int


class CycleTimeOut(_StrictModel):
    visibility_complete: bool
    median_seconds: float | None
    sample_count: int
    excluded_completions: int


class ActorTypeTotalsOut(_StrictModel):
    human: int
    agent: int
    unknown: int


class ActorOut(_StrictModel):
    actor_id: int
    actor_name: str
    actor_type: Literal["human", "agent", "mixed", "unknown"]
    completions: int
    human_completions: int
    agent_completions: int
    unknown_completions: int


class CoverageOut(_StrictModel):
    visible_candidate_events: int
    included_typed_events: int
    excluded_legacy_events: int
    excluded_imported_events: int
    excluded_restricted_events: int
    excluded_orphan_events: int
    excluded_malformed_events: int
    cycle_samples_excluded: int
    complete: bool


class LimitsOut(_StrictModel):
    max_window_days: int
    evidence_event_limit: int
    history_event_limit: int
    actor_limit: int
    actor_rows_available: int
    actor_rows_truncated: bool


class DefinitionsOut(_StrictModel):
    created: str
    completed: str
    net_flow: str
    cycle_time: str
    attribution: str


class FleetMetricsOut(_StrictModel):
    schema_: Literal["athena.fleet_metrics.v1"] = Field(alias="schema")
    scope: Literal["visible_to_request_actor"]
    period: PeriodOut
    filters: FiltersOut
    flow: FlowOut
    cycle_time: CycleTimeOut
    completion_by_actor_type: ActorTypeTotalsOut
    actors: list[ActorOut]
    coverage: CoverageOut
    limits: LimitsOut
    definitions: DefinitionsOut


def _query_from_request(request: Request) -> fleet_metrics.FleetMetricsQuery:
    try:
        return fleet_metrics.parse_query_pairs(list(request.query_params.multi_items()))
    except fleet_metrics.FleetMetricsError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.detail,
            headers=_PRIVATE_HEADERS,
        ) from exc


def _fleet_metrics_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_athena_actor: str | None = Header(default=None, alias=identity.ACTOR_HEADER),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | None:
    """Allow absent credentials, but never downgrade presented-invalid ones."""
    try:
        actor = identity.optional_actor(request, authorization, x_athena_actor, conn)
    except HTTPException as exc:
        headers = dict(exc.headers or {})
        headers.update(_PRIVATE_HEADERS)
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from exc
    if actor is None and (authorization is not None or x_athena_actor is not None):
        raise HTTPException(
            status_code=401,
            detail="invalid or revoked authentication credential",
            headers=_PRIVATE_HEADERS,
        )
    return actor


def _require_aegis_read_scope(actor: dict | None) -> None:
    if actor is None or actor.get("_token_id") is None:
        return
    if not (
        identity.token_has_scope(actor, tokens.READ_SCOPE)
        or identity.token_has_scope(actor, tokens.ISSUE_WRITE_SCOPE)
    ):
        raise identity.ScopeDenied(
            actor,
            "read or issue:write",
            headers=_PRIVATE_HEADERS,
        )


@router.get("/metrics", response_model=FleetMetricsOut)
def show_fleet_metrics(
    request: Request,
    response: Response,
    actor: dict | None = Depends(_fleet_metrics_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> fleet_metrics.FleetMetricsPayload:
    """Return one bounded metrics snapshot for the request actor."""

    response.headers.update(_PRIVATE_HEADERS)
    _require_aegis_read_scope(actor)
    query = _query_from_request(request)
    try:
        return fleet_metrics.build_fleet_metrics(conn, query, actor=actor)
    except fleet_metrics.FleetMetricsError as exc:
        status_code = 404 if exc.kind == "not_found" else 422
        raise HTTPException(
            status_code=status_code,
            detail=exc.detail,
            headers=_PRIVATE_HEADERS,
        ) from exc
