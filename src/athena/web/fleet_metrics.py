"""Server-rendered adapter for the shared fleet metrics projection."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from athena.aegis import fleet_metrics
from athena.core.deps import get_conn
from athena.web.router import get_templates


router = APIRouter()
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie"}


def _preset_links(end: date) -> list[dict[str, object]]:
    return [
        {
            "days": days,
            "href": "/aegis/fleet-metrics?"
            + urlencode(
                {
                    "start": (end - timedelta(days=days)).isoformat(),
                    "end": end.isoformat(),
                }
            ),
        }
        for days in (7, 30, 90)
    ]


@router.get("/aegis/fleet-metrics", response_class=HTMLResponse)
def fleet_metrics_page(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Render exactly the transport-neutral projection for the session viewer."""

    viewer = getattr(request.state, "user", None)
    utc_today = datetime.now(timezone.utc).date()
    try:
        query = fleet_metrics.parse_query_pairs(
            list(request.query_params.multi_items()), today=utc_today
        )
        projection = fleet_metrics.build_fleet_metrics(
            conn, query, actor=viewer
        )
    except fleet_metrics.FleetMetricsError as exc:
        status_code = 404 if exc.kind == "not_found" else 400
        heading = (
            "Metrics scope not found"
            if status_code == 404
            else "Invalid metrics request"
        )
        return HTMLResponse(
            f"<h1>{heading}</h1><p>{escape(exc.detail)}</p>",
            status_code=status_code,
            headers=_PRIVATE_HEADERS,
        )

    return get_templates().TemplateResponse(
        request=request,
        name="aegis/fleet_metrics.html",
        context={
            "metrics": projection,
            "preset_links": _preset_links(utc_today + timedelta(days=1)),
        },
        headers=_PRIVATE_HEADERS,
    )
