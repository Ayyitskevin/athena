"""The desk REST surface: your orientation, and your cursor.

Two endpoints, both about the CALLER. Neither takes a user id — there is no
cross-reader desk to address, which is the simplest possible authorization
story: you get yours.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from athena.aegis import desk_commands
from athena.core import desk_cursors
from athena.core.deps import get_conn
from athena.core.identity import current_actor, personal_write_actor
from athena.core.ids import MAX_SQLITE_INTEGER

router = APIRouter(tags=["core"])


def _refuse(exc: desk_commands.DeskCommandError) -> HTTPException:
    return HTTPException(
        status_code=desk_commands.STATUS_BY_KIND.get(exc.kind, 400),
        detail=exc.detail,
    )


class CursorOut(BaseModel):
    user_id: int
    name: str
    after_id: int
    updated_at: str


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

    return office.build_office(conn, actor=actor)


@router.get("/desk")
def read_desk(
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Deliberately un-modelled by a response_model: the desk composes other
    # surfaces' projections verbatim, and re-declaring their shapes here would
    # create a second place for them to drift. The schema field names the
    # contract version instead.
    return desk_commands.read_desk(conn, actor=actor)


@router.post("/desk/cursor", response_model=CursorOut)
def advance_cursor(
    payload: AdvanceCursorIn,
    # A cursor advance is a personal-state WRITE (the F-1 design says exactly
    # that), so it takes the personal-state gate watches and read-marks use:
    # a read-only bearer token must never mutate anything, its own rows
    # included. Shipped briefly on current_actor; this closes that.
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # No idempotency-key plumbing: advancing to a value you already hold is a
    # no-op that returns current state, and advancing below it is refused, so
    # a retry is safe by construction rather than by receipt.
    try:
        return desk_commands.advance_cursor(
            conn, actor=actor, after_id=payload.after_id, name=desk_cursors.DESK_CURSOR
        )
    except desk_commands.DeskCommandError as exc:
        raise _refuse(exc) from exc
