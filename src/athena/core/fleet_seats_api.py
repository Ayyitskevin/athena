"""The declared fleet seats over REST (U4, decision D27).

An admin reads, declares, edits and removes the seats the Fleet roster and
"Assign to a seat" use. The writes are ``core/fleet_seat_commands.py``'s, the
same the cockpit's forms call; these routes translate transport only. Every
route is admin only, the read included: the roster is the operator's page, and
a non-admin sees no more of it than before.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from athena.core import fleet_roster, fleet_seat_commands, list_cursor
from athena.core.deps import get_conn
from athena.core.identity import current_actor, require_admin
from athena.core.packet_time import Timestamp
from athena.core.refusals import Refusal
from athena.core.request_models import RequestBody

router = APIRouter(prefix="/fleet/seats", tags=["core"])

STATUS_BY_KIND: dict[str, int] = {
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "invalid": 422,
}
CODE_BY_KIND: dict[str, str] = {
    "forbidden": "forbidden",
    "not_found": "not_found",
    "conflict": "conflict",
    "invalid": "validation_error",
}

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
_CURSOR = "fleet-seats"

SeatKind = Literal["operator", "seat", "local_ollama"]
UnitScope = Literal["user", "system"]
SlugPath = Path(max_length=40, description="the seat's slug")


class SeatOut(BaseModel):
    slug: str = Field(description="the stable key: Buzz, systemd, the desk's seat_slug")
    name: str
    email: str | None = Field(description="the Athena account this seat is; null: none")
    kind: SeatKind
    unit: str | None = Field(description="the systemd unit; null: none")
    unit_scope: UnitScope = Field(description="the systemd manager that owns the unit")
    buzz_pubkey: str | None
    created_at: Timestamp
    updated_at: Timestamp


class SeatsOut(BaseModel):
    """One page, paged as every new list is (D2): while `has_more` is true,
    send `next_cursor` back as `cursor`."""

    items: list[SeatOut]
    # Opaque: pass it back unchanged. Null on the last page.
    next_cursor: str | None
    has_more: bool


class SeatIn(RequestBody):
    slug: str = Field(
        max_length=40,
        description="lower-case letters, digits and hyphens; unique; never edited",
    )
    name: str = Field(max_length=100)
    email: str | None = Field(
        default=None,
        max_length=254,
        description="the Athena account this seat is; one seat per email",
    )
    kind: SeatKind = "seat"
    unit: str | None = Field(default=None, max_length=200)
    unit_scope: UnitScope = "user"
    buzz_pubkey: str | None = Field(
        default=None, max_length=64, description="64 hexadecimal characters"
    )


class SeatChanges(RequestBody):
    """The fields to change. An absent field is kept; null clears one that may
    be empty (email, unit, buzz_pubkey). The slug is not editable."""

    name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    kind: SeatKind | None = None
    unit: str | None = Field(default=None, max_length=200)
    unit_scope: UnitScope | None = None
    buzz_pubkey: str | None = Field(default=None, max_length=64)


class RemovedSeatOut(BaseModel):
    slug: str
    event_id: int = Field(description="the trail event that records the removal")


def _refuse(exc: fleet_seat_commands.FleetSeatCommandError) -> Refusal:
    return Refusal(
        status_code=STATUS_BY_KIND[exc.kind],
        detail=exc.detail,
        code=CODE_BY_KIND[exc.kind],
    )


@router.get("", response_model=SeatsOut)
def list_seats(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(
        None,
        max_length=list_cursor.MAX_CURSOR_CHARS,
        description="the previous page's next_cursor, unchanged; omit for the first page",
    ),
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """The declared seats, in the order they were declared. Admin only."""
    require_admin(actor)
    after_id = 0
    if cursor is not None:
        try:
            (after_id,) = list_cursor.decode(cursor, _CURSOR, (int,))
        except list_cursor.CursorError as exc:
            raise Refusal(
                status_code=422, detail=str(exc), code="validation_error"
            ) from exc
    rows = fleet_roster.list_seats(conn, after_id=after_id, limit=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": rows,
        "next_cursor": (
            list_cursor.encode(_CURSOR, [rows[-1]["id"]]) if has_more else None
        ),
        "has_more": has_more,
    }


@router.post("", response_model=SeatOut, status_code=201)
def declare_seat(
    body: SeatIn,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Declare a seat. A slug or email another seat holds is refused 409
    `conflict`. Recorded on the trail. Admin only."""
    require_admin(actor)
    try:
        return fleet_seat_commands.declare_seat(conn, actor=actor, **body.model_dump())
    except fleet_seat_commands.FleetSeatCommandError as exc:
        raise _refuse(exc) from exc


@router.patch("/{slug}", response_model=SeatOut)
def edit_seat(
    body: SeatChanges,
    slug: str = SlugPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Change a seat's fields; the slug stays. Recorded on the trail when
    anything changed. Admin only."""
    require_admin(actor)
    try:
        return fleet_seat_commands.update_seat(
            conn,
            actor=actor,
            slug=slug,
            changes=body.model_dump(include=body.model_fields_set),
        )
    except fleet_seat_commands.FleetSeatCommandError as exc:
        raise _refuse(exc) from exc


@router.delete("/{slug}", response_model=RemovedSeatOut)
def remove_seat(
    slug: str = SlugPath,
    actor: dict = Depends(current_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Remove a seat. The account it named is kept and reads as an agent no
    seat declares. Recorded on the trail. Admin only."""
    require_admin(actor)
    try:
        removed = fleet_seat_commands.remove_seat(conn, actor=actor, slug=slug)
    except fleet_seat_commands.FleetSeatCommandError as exc:
        raise _refuse(exc) from exc
    return {"slug": removed["seat"]["slug"], "event_id": removed["event_id"]}
