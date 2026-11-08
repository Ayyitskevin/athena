"""REST inbox with read-time row state, composed from core notifications and Aegis issues.

Core owns the inbox rows and their visibility gate. This route adds ``target_state``
to each row (see ``athena.aegis.notification_state``). It replaces the plain inbox
route that used to live in core, so ``GET /notifications`` has exactly one owner.

The response model is core's ``NotificationOut``, so the documented schema keeps its
name and gains one additive field. The route keeps the ``core`` tag so the generated
API documentation groups it where it was before.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from athena.aegis import notification_state
from athena.core import notifications
from athena.core.deps import get_conn
from athena.core.identity import current_actor
from athena.core.notifications_api import NotificationOut

router = APIRouter(tags=["core"])


@router.get("/notifications", response_model=list[NotificationOut])
def inbox(
    unread: bool = Query(False, description="only unread notifications"),
    limit: int = Query(50, ge=1, le=200),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    rows = notifications.list_notifications(
        conn, actor["id"], unread_only=unread, limit=limit, actor=actor
    )
    return notification_state.annotate_inbox(conn, actor["id"], rows)
