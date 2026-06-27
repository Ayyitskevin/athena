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

from athena.core import webhooks
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
    # Start at the current tip so the endpoint receives only events from now on,
    # not the entire backlog.
    return webhooks.create_webhook(
        conn,
        url=url,
        event_kind=event_kind,
        created_by=actor["id"],
        start_cursor=webhooks.current_tip(conn),
    )


@router.get("/{webhook_id}", response_model=WebhookOut)
def show(
    webhook_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    webhook = webhooks.get_webhook(conn, webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail="no such webhook")
    return webhook


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update(
    webhook_id: int,
    payload: WebhookUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Pause/resume is the one supported edit: it keeps the cursor (no replay/skip) and
    # lets an operator stop a misbehaving endpoint without losing where it was up to.
    updated = webhooks.set_webhook_active(conn, webhook_id, payload.active)
    if updated is None:
        raise HTTPException(status_code=404, detail="no such webhook")
    return updated


@router.delete("/{webhook_id}", status_code=204)
def remove(
    webhook_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    if not webhooks.delete_webhook(conn, webhook_id):
        raise HTTPException(status_code=404, detail="no such webhook")
