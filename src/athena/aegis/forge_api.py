"""The inbound forge endpoint, and admin management of the sources feeding it.

One route accepts events. Everything about it is shaped by the fact that it is
**the only endpoint in Athena an unauthenticated stranger is expected to hit**.

**The signature is checked before the payload is parsed.** This handler takes the
raw ``Request`` and declares no Pydantic body model, which is a deliberate
difference from the Icarus callback: FastAPI parses a declared body *before* the
handler runs, so a declared model would put the JSON parser and its validation in
front of the authentication check. Here the order is: bound the size, read the
bytes, verify the HMAC over exactly those bytes, and only then parse. An
unsigned or mis-signed delivery never reaches a parser at all.

**A refusal reveals nothing.** An unknown source name and a bad signature return
the same 401, so this endpoint cannot be used to enumerate which sources a
workspace has registered.

**Acceptance is not agreement.** A 202 means "authentic and understood", not
"true". Every landed row is imported history — Athena's record of what it was
*told*.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from athena.aegis import forge
from athena.core import event_source_commands, event_sources, forge_events
from athena.core.deps import get_conn
from athena.core.identity import current_actor, is_admin

router = APIRouter(tags=["forge"])


class SourceCreate(BaseModel):
    name: str
    kind: str = forge_events.SOURCE_GITHUB
    host: str = "github.com"


class EnabledUpdate(BaseModel):
    enabled: bool


def _admin_or_403(actor: dict) -> None:
    # Registering a source hands out a credential that writes history. That is an
    # admin decision, like minting a token or registering a webhook.
    if not is_admin(actor):
        raise HTTPException(status_code=403, detail="admin only")


@router.get("/event-sources")
def list_event_sources(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """Registered sources and their health. Never includes a secret."""
    _admin_or_403(actor)
    return event_sources.list_sources(conn)


@router.post("/event-sources", status_code=201)
def create_event_source(
    payload: SourceCreate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Register a source, returning its signing secret **once**.

    The secret is not stored anywhere readable and cannot be retrieved again — the
    same contract as a webhook secret or an API token. Losing it means
    re-registering.
    """
    _admin_or_403(actor)
    name = payload.name.strip()
    host = payload.host.strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="source name is required")
    if not host:
        raise HTTPException(status_code=422, detail="source host is required")
    try:
        return event_source_commands.register_source(
            conn, actor_id=actor["id"], name=name, kind=payload.kind, host=host
        )
    except event_source_commands.EventSourceCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.put("/event-sources/{source_id}/enabled")
def set_event_source_enabled(
    source_id: int,
    payload: EnabledUpdate,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Pause or resume acceptance without rotating the secret."""
    _admin_or_403(actor)
    try:
        return event_source_commands.set_source_enabled(
            conn, actor_id=actor["id"], source_id=source_id, enabled=payload.enabled
        )
    except event_source_commands.EventSourceCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.delete("/event-sources/{source_id}", status_code=204)
def delete_event_source(
    source_id: int,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    """Revoke a source. History it already landed is kept — those events were
    authentic when recorded, and revoking a credential is not a reason to rewrite
    the trail."""
    _admin_or_403(actor)
    if not event_source_commands.delete_source(
        conn, actor_id=actor["id"], source_id=source_id
    ):
        raise HTTPException(status_code=404, detail="no such event source")


@router.get("/forge/help")
def forge_help() -> dict:
    """The inbound vocabulary and limits as data, emitted by the parser itself."""
    return forge_events.describe()


@router.post("/forge/{source_name}", status_code=202)
async def receive_forge_event(
    source_name: str,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Accept one signed event from a registered source.

    Note what this signature does NOT declare: a request body. FastAPI would parse
    and validate a declared model before this function ran, which would place the
    JSON parser ahead of authentication on a publicly reachable route. Reading the
    raw bytes and verifying first is the whole point.

    Returns what happened — how many events landed and how many named nothing —
    so the sender's delivery log shows whether Athena did anything with it. A
    delivery that matches no issue is a **success**: authentic, understood, and
    about work this workspace does not track.
    """
    source = event_sources.get_source_by_name(conn, source_name)

    # Bound BEFORE reading: content-length is a claim, but an honest sender sets
    # it, and refusing early keeps an oversized body out of memory.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > forge_events.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > forge_events.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    # An unknown source and a bad signature are the SAME refusal. Answering "no
    # such source" would turn this route into a directory of the workspace's
    # integrations for anyone who can reach it.
    if source is None or not event_sources.verify_signature(
        conn, source["id"], body, x_hub_signature_256
    ):
        raise HTTPException(status_code=401, detail="invalid signature")
    if not source["enabled"]:
        # Authenticated, but the operator has switched this channel off. 403 (not
        # 401) because the credential is good — the answer is "not right now".
        raise HTTPException(status_code=403, detail="event source is paused")

    # Authenticated. Only now does untrusted input reach a parser.
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="payload is not JSON") from exc

    parser = forge_events.PARSERS.get(source["kind"])
    if parser is None:  # unreachable: kind is closed at registration
        raise HTTPException(status_code=500, detail="no parser for this source kind")
    try:
        facts = parser(x_github_event or "", payload)
    except forge_events.ForgeEventError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = forge.land_delivery(conn, source=source, facts=facts)
    return {
        "source": source["name"],
        "event": x_github_event,
        "landed": result["matched"],
        "unmatched": result["unmatched"],
        "issues": sorted({row["issue_id"] for row in result["landed"]}),
    }
