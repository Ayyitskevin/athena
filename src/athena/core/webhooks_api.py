"""The webhooks REST API — register endpoints that receive pushed events.

Managing webhooks is an operator action (they cause the server to make outbound
requests), so every route requires an admin actor, like user administration. The
signing secret is returned exactly once, at creation; the read paths never expose
it again. URL safety (SSRF) is enforced here at the boundary, and re-checked at
delivery time in core/webhooks.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from athena.core import webhook_commands, webhooks
from athena.core.ids import RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import admin_actor

router = APIRouter(prefix="/webhooks", tags=["core"])


class WebhookCreate(BaseModel):
    url: str
    # Optional filter: deliver only events whose target_kind matches (e.g. "issue"
    # or "page"). Omit to receive every event.
    event_kind: str | None = None


class WebhookOut(BaseModel):
    id: int
    url: str
    event_kind: str | None = None
    active: int
    cursor: int
    failure_count: int
    last_error: str | None = None
    last_attempt_at: str | None = None
    next_attempt_at: str | None = None
    last_success_at: str | None = None
    created_by: int
    created_at: str


class WebhookCreated(WebhookOut):
    # Only the create response carries the secret — shown once, never again.
    secret: str


class WebhookUpdate(BaseModel):
    # Pause (false) or resume (true) delivery without deleting the row (and its
    # cursor). The only mutable field — url/secret/event_kind are fixed at creation.
    active: bool


@router.get("", response_model=list[WebhookOut])
def list_all(
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    return webhooks.list_webhooks(conn)


@router.post("", response_model=WebhookCreated, status_code=201)
def create(
    payload: WebhookCreate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    url = payload.url.strip()
    ok, reason = webhooks.is_safe_url(url)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)
    event_kind = (payload.event_kind or "").strip() or None
    # The command owns the registration, its atomic 'registered_webhook' audit event,
    # AND the "start at tip" cursor (only future events, never the backlog), so a new
    # outbound endpoint is never silent.
    return webhook_commands.register_webhook(
        conn, actor_id=actor["id"], url=url, event_kind=event_kind
    )


@router.get("/{webhook_id}", response_model=WebhookOut)
def show(
    webhook_id: RowIdPath,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    webhook = webhooks.get_webhook(conn, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="no such webhook")
    return webhook


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update(
    webhook_id: RowIdPath,
    payload: WebhookUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Pause/resume is the one supported edit: it keeps the cursor (no replay/skip) and
    # lets an operator stop a misbehaving endpoint without losing where it was up to.
    # The command records the flip atomically.
    try:
        return webhook_commands.set_webhook_active(
            conn, actor_id=actor["id"], webhook_id=webhook_id, active=payload.active
        )
    except webhook_commands.WebhookCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.delete("/{webhook_id}", status_code=204)
def remove(
    webhook_id: RowIdPath,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    # The command records the deletion atomically (naming the URL that is going away).
    if not webhook_commands.delete_webhook(
        conn, actor_id=actor["id"], webhook_id=webhook_id
    ):
        raise HTTPException(status_code=404, detail="no such webhook")
