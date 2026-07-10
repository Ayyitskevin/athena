"""JSON dashboard API — same snapshot as the web overview."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from athena.aegis import dashboard
from athena.core import access
from athena.core.deps import get_conn
from athena.core.identity import optional_actor

router = APIRouter(prefix="/api/aegis", tags=["aegis-dashboard"])


@router.get("/dashboard")
def dashboard_json(
    conn: sqlite3.Connection = Depends(get_conn),
    actor: dict | None = Depends(optional_actor),
) -> dict:
    """Read-only workspace glance for humans and agents.

    Visibility-gated the same way as the HTML dashboard. Counts only; never a
    write path.
    """
    vis = access.visible_project_filter(conn, actor)
    snap = dashboard.snapshot(
        conn,
        visible_project_ids=vis,
        user_id=actor["id"] if actor else None,
    )
    return snap
