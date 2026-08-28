"""REST API for the ranked "Now" attention queue.

This is the read-time twin of the count-based ``fleet_attention`` card used on
the dashboard. It returns individual attention rows ranked by severity and
freshness, each naming the owning surface so the caller knows where to act.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from athena.aegis import fleet_attention
from athena.core.deps import get_conn
from athena.core.identity import current_actor

router = APIRouter(prefix="/attention/ranking", tags=["aegis"])

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, X-Athena-Actor",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AttentionRankItemOut(_StrictModel):
    signal: fleet_attention.AttentionSignal
    severity: fleet_attention.Severity
    source_kind: fleet_attention.SourceKind
    source_id: int
    owner_id: int | None
    owner_name: str | None
    reason: str
    freshness: str
    examined: int
    total: int
    next_action: fleet_attention.NextAction
    command: str | None = None
    link: str | None = None
    source_link: str | None = None


class AttentionRankOut(_StrictModel):
    items: list[AttentionRankItemOut]
    examined: int
    total: int
    signals: list[fleet_attention.AttentionSignal]
    returned: int
    limit: int
    clipped: bool


def _parse_signals(value: str | None) -> set[str] | None:
    """Parse the ?signals= query parameter. Supports both repeated params and a
    single comma-separated value. Whitespace around names is ignored."""
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return set(parts) if parts else None


@router.get("", response_model=AttentionRankOut)
def index(
    response: Response,
    signals: str | None = Query(
        default=None,
        description=(
            "comma-separated signal filter, e.g. "
            "open_blocker,pending_approval,failing_webhook"
        ),
    ),
    window_hours: int = Query(
        default=fleet_attention.DEFAULT_WINDOW_HOURS,
        ge=1,
        le=168,
        description="how far back event-counted signals look",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="maximum ranked rows to return",
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Return the ranked attention queue for the authenticated actor.

    Admins see the fleet signals already available on the cockpit. Other actors
    see only signals their existing read gates permit: their own claim/control
    rows and blockers on visible issues. Unknown signal names in ``?signals=``
    produce a 422. The response always discloses bounded denominators.
    """
    response.headers.update(_PRIVATE_HEADERS)
    signal_set = _parse_signals(signals)
    try:
        ranking = fleet_attention.build_attention_ranking(
            conn,
            signals=signal_set,
            actor=actor,
            window_hours=window_hours,
            limit=limit,
        )
        result = fleet_attention.public_attention_ranking(conn, ranking, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "items": result["items"],
        "examined": result["examined"],
        "total": result["total"],
        "signals": result["signals"],
        "returned": result["returned"],
        "limit": result["limit"],
        "clipped": result["clipped"],
    }
