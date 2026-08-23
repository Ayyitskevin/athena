"""Commands for the desk: read it, and move your own cursor.

The read is a composition (``core/desk.py``); this module owns the
authorization and the one write — advancing a reader's position.

**Why a command owner for personal state.** The cursor write satisfies every
criterion of COMMAND_MIGRATION.md's personal-state category (owner-scoped,
single logical write, exactly one data-module writer) and that category is the
citation for recording **no activity event**: a read receipt is the reader's
own bookkeeping, not fleet history. But the category is documented as an
exception rather than a lane, and this write has a real refusal to express —
a cursor may not move backward — so it gets a command owner anyway, with the
transport-neutral kind dialect. Strictly more conservative than the exception
permits.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3

from athena.aegis import desk
from athena.core import db, desk_cursors
from athena.core.ids import MAX_SQLITE_INTEGER

#: The closed refusal vocabulary. `not_found` is deliberately absent: every
#: authenticated actor has a desk, so there is nothing here to miss.
ErrorKind = str

STATUS_BY_KIND: dict[str, int] = {
    "invalid": 422,
    "conflict": 409,
}


class DeskCommandError(Exception):
    """A refusal with a transport-neutral kind. The adapter maps it."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def read_desk(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
) -> dict:
    """This actor's own desk. Any authenticated actor may read theirs.

    Authorization is the identity itself — there is no cross-reader desk, and
    no parameter that could name someone else's. An operator who wants the
    fleet's state reads the admin surfaces, which stay admin-only.
    """
    return desk.build_desk(conn, actor=actor, now=now)


def advance_cursor(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    after_id: object,
    name: str = desk_cursors.DESK_CURSOR,
) -> dict:
    """Move this actor's cursor forward to ``after_id``.

    Idempotent at the same value (a repeated acknowledgement is a no-op that
    returns current state) and refused below it (``conflict``): a cursor that
    could rewind would let a reader unsay an acknowledgement it had already
    made. The 0073 trigger refuses the same move at the database, for a writer
    that skips this command.

    Records no activity event — see the module docstring.
    """
    if isinstance(after_id, bool) or not isinstance(after_id, int):
        raise DeskCommandError("invalid", "after_id must be an integer")
    if after_id < 1:
        raise DeskCommandError("invalid", "after_id must be a positive integer")
    if after_id > MAX_SQLITE_INTEGER:
        raise DeskCommandError(
            "invalid", f"after_id must be at most {MAX_SQLITE_INTEGER}"
        )
    if name not in (desk_cursors.DESK_CURSOR,):
        raise DeskCommandError("invalid", f"unknown cursor name: {name}")

    user_id = int(actor["id"])
    # One immediate transaction so the read that decides "is this a rewind?"
    # and the write that acts on it cannot be interleaved by a concurrent
    # advance — the personal-state category explicitly permits this shape.
    with db.transaction(conn, immediate=True):
        current = desk_cursors.get_cursor(conn, user_id=user_id, name=name)
        if current is not None and after_id < int(current["after_id"]):
            raise DeskCommandError(
                "conflict",
                f"cursor is already at {current['after_id']}; "
                "a cursor may not move backward",
            )
        return desk_cursors.set_cursor(
            conn, user_id=user_id, name=name, after_id=after_id
        )
