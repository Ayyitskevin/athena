"""The desk REST surface: your orientation, and your cursor.

Two endpoints, both about the CALLER. Neither takes a user id — there is no
cross-reader desk to address, which is the simplest possible authorization
story: you get yours.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from athena.aegis import (
    answer_glossary,
    desk_commands,
    notification_groups,
    operator_since,
    ready_queue,
)
from athena.core import desk_cursors, packet_time
from athena.core.deps import get_conn
from athena.core.identity import current_actor, personal_write_actor
from athena.core.ids import MAX_SQLITE_INTEGER
from athena.core.packet_time import Timestamp
from athena.core.refusals import Refusal

router = APIRouter(tags=["core"])


def _refuse(exc: desk_commands.DeskCommandError) -> Refusal:
    return Refusal(
        status_code=desk_commands.STATUS_BY_KIND.get(exc.kind, 400),
        detail=exc.detail,
        code=desk_commands.CODE_BY_KIND.get(exc.kind, "bad_request"),
    )


def _refusal_response(exc: desk_commands.DeskCommandError) -> JSONResponse:
    """A refusal that carries a machine code, with the fields it names.

    ``cursor_ahead`` adds the newest event id the caller can see — the highest
    value it may pass — so an agent can correct the call without another read.
    """
    return JSONResponse(
        status_code=desk_commands.STATUS_BY_KIND.get(exc.kind, 400),
        content={
            "detail": exc.detail,
            "code": exc.code,
            "latest_visible_event_id": exc.latest_visible_event_id,
        },
    )


class CursorOut(BaseModel):
    user_id: int
    name: str
    after_id: int
    # The desk's `cursor` lane, as the advance answers it: one shape (A13).
    updated_at: Timestamp


class AdvanceCursorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounded at the schema so an out-of-range id is a 422 rather than an
    # OverflowError from the driver (core/ids.py carries the reasoning).
    after_id: int = Field(..., ge=1, le=MAX_SQLITE_INTEGER)


@router.get("/office")
def read_office(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """The cubicle: one chair, fenced paths, checkout hint. Not a lock."""
    from athena.aegis import office

    now = packet_time.clock()
    return packet_time.stamp(office.build_office(conn, actor=actor, now=now), now=now)


@router.get("/desk")
def read_desk(
    lean: bool = Query(False, description=answer_glossary.LEAN_QUERY),
    group: Literal["issue", "none"] = Query(
        "none",
        description="issue: signals.notifications one row per target, as "
        "GET /notifications/grouped answers (AD12); the MCP desk's default",
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Deliberately un-modelled by a response_model: the desk composes other
    # surfaces' projections verbatim, and re-declaring their shapes here would
    # create a second place for them to drift. The schema field names the
    # contract version instead. Every timestamp leaves in one shape and every
    # expiry with its seconds, from the one clock read the lanes were built at
    # (A13, core/packet_time.py).
    now = packet_time.clock()
    desk = packet_time.stamp(
        desk_commands.read_desk(conn, actor=actor, now=now), now=now
    )
    if group == "issue":
        signals = desk["signals"]
        signals["notifications"] = notification_groups.group_rows(
            signals["notifications"]
        )
    return answer_glossary.lean(desk) if lean else desk


@router.get(answer_glossary.ROUTE)
def read_answer_glossary(actor: dict = Depends(current_actor)) -> dict:
    """What lean desk and work-context answers leave out, by schema and path:
    every fixed explanation, once. The MCP resource athena://answers/glossary
    is the same markdown."""
    return answer_glossary.document()


@router.get("/desk/ready")
def read_ready(
    limit: int = Query(ready_queue.READY_LIMIT, ge=1, le=ready_queue.MAX_READY_LIMIT),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Your ready lane alone (the desk's ``work.ready``): what you could claim
    now, in claim order, with its own total. A read, not a reservation."""
    now = packet_time.clock()
    return packet_time.stamp(
        ready_queue.build_ready(conn, actor=actor, limit=limit, now=now), now=now
    )


@router.get("/desk/since")
def read_since(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """What changed after your desk cursor, by kind. A read: it never moves the
    cursor. Advance it with ``POST /desk/cursor`` to ``last_shown_event_id``."""
    now = packet_time.clock()
    return packet_time.stamp(
        operator_since.build_since(conn, actor=actor, now=now), now=now
    )


@router.post("/desk/cursor", response_model=CursorOut)
def advance_cursor(
    payload: AdvanceCursorIn,
    # A cursor advance is a personal-state WRITE (the F-1 design says exactly
    # that), so it takes the personal-state gate watches and read-marks use:
    # a read-only bearer token must never mutate anything, its own rows
    # included. Shipped briefly on current_actor; this closes that.
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    # No idempotency-key plumbing: advancing to a value you already hold is a
    # no-op that returns current state, and advancing below it is refused, so
    # a retry is safe by construction rather than by receipt.
    try:
        return desk_commands.advance_cursor(
            conn, actor=actor, after_id=payload.after_id, name=desk_cursors.DESK_CURSOR
        )
    except desk_commands.DeskCommandError as exc:
        if exc.code is not None:
            return _refusal_response(exc)
        raise _refuse(exc) from exc
