"""REST API for the ranked "Now" attention queue.

This is the read-time twin of the count-based ``fleet_attention`` card used on
the dashboard. It returns individual attention rows ranked by severity and
freshness, each naming the owning surface so the caller knows where to act.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from athena.aegis import fleet_attention
from athena.core.deps import get_conn
from athena.core.identity import admin_actor

router = APIRouter(prefix="/attention/ranking", tags=["aegis"])

_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, X-Athena-Actor",
}


class AttentionRankItemOut(BaseModel):
    signal: str
    severity: str
    source_kind: str
    source_id: int
    owner_id: int | None
    owner_name: str | None
    reason: str
    freshness: str
    examined: int
    total: int
    next_action: str
    command: str | None = None
    link: str | None = None


class AttentionRankOut(BaseModel):
    items: list[AttentionRankItemOut]
    examined: int
    total: int
    signals: list[str]


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
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Return the ranked attention queue for the authenticated admin.

    The queue is built fresh on every request from existing data — there is no
    separate attention table. Unknown signal names in ``?signals=`` produce a 422
    (fail closed). The response always discloses how many candidates were
    examined and considered, even when the queue is empty.
    """
    response.headers.update(_PRIVATE_HEADERS)
    signal_set = _parse_signals(signals)
    try:
        result = fleet_attention.build_attention_ranking(
            conn,
            signals=signal_set,
            actor=actor,
            window_hours=window_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "items": [
            fleet_attention.to_public_rank_item(conn, item, actor=actor)
            for item in result["items"]
        ],
        "examined": result["examined"],
        "total": result["total"],
        "signals": result["signals"],
    }
