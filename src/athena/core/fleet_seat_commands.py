"""Application commands for the declared fleet seats (U4, decision D27).

The seats the Fleet roster and "Assign to a seat" read live in ``fleet_seats``
(migration 0093), not in source. These three commands own every write to that
table, each with its trail event in one transaction: an admin declares a seat,
edits it, or removes it. The REST routes (``core/fleet_seats_api.py``) and the
cockpit forms (``web/admin_agents.py``) are adapters over them.

A seat carries public facts only: a slug, a name, the Athena email it answers
for, a systemd unit and the manager that owns it, a Buzz public key and a kind.
The slug is the stable key agents and the cockpit use (Buzz, systemd, branch
names, the desk's ``seat_slug``), so it is lower case and never edited; remove
the seat and declare another to rename one. An email belongs to at most one
seat, because it is how an account is matched to its seat.

Refusals carry a transport-neutral kind (``forbidden``, ``not_found``,
``invalid``, ``conflict``); the adapters map it to a status and a code.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
import sqlite3
from typing import Any

from athena.core import activity, db, fleet_roster, users

VERB_DECLARED = "declared_fleet_seat"
VERB_EDITED = "edited_fleet_seat"
VERB_REMOVED = "removed_fleet_seat"

TARGET_KIND = "fleet_seat"

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
#: A systemd unit name. It reaches ``systemctl`` as an argument (never a
#: shell), and a leading ``-`` would make it an option there, so it may not
#: start with one.
UNIT_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9@._:-]{0,199}$"
PUBKEY_PATTERN = r"^[0-9a-f]{64}$"
NAME_MAX = 100
EMAIL_MAX = 254

#: The fields an edit may change. The slug is the seat's key and is not one.
EDITABLE = ("name", "email", "kind", "unit", "unit_scope", "buzz_pubkey")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class FleetSeatCommandError(Exception):
    """A transport-neutral refusal: a kind and a human detail, no status."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _require_admin(actor: Mapping[str, Any]) -> None:
    if actor.get("role") != users.ADMIN_ROLE:
        raise FleetSeatCommandError("forbidden", "admin role required")


def _slug(value: Any) -> str:
    slug = str(value or "").strip().lower()
    if not re.fullmatch(SLUG_PATTERN, slug):
        raise FleetSeatCommandError(
            "invalid",
            "slug must be 1-40 lower-case letters, digits or hyphens, "
            "starting with a letter or digit",
        )
    return slug


def _name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > NAME_MAX or _CONTROL.search(name):
        raise FleetSeatCommandError(
            "invalid", f"name must be 1-{NAME_MAX} printable characters"
        )
    return name


def _email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    if not email:
        return None
    local, at, domain = email.partition("@")
    if (
        not at
        or not local
        or not domain
        or len(email) > EMAIL_MAX
        or any(char.isspace() for char in email)
        or _CONTROL.search(email)
    ):
        raise FleetSeatCommandError(
            "invalid", "email must be an address, or empty for a seat with no handle"
        )
    return email


def _kind(value: Any) -> str:
    kind = str(value or "seat").strip()
    if kind not in fleet_roster.SEAT_KINDS:
        raise FleetSeatCommandError(
            "invalid", f"kind must be one of: {', '.join(fleet_roster.SEAT_KINDS)}"
        )
    return kind


def _unit(value: Any) -> str | None:
    unit = str(value or "").strip()
    if not unit:
        return None
    if not re.fullmatch(UNIT_PATTERN, unit):
        raise FleetSeatCommandError(
            "invalid",
            "unit must be a systemd unit name (letters, digits and @ . _ : -, "
            "not starting with -), up to 200 characters",
        )
    return unit


def _unit_scope(value: Any) -> str:
    scope = str(value or "user").strip()
    if scope not in fleet_roster.UNIT_SCOPES:
        raise FleetSeatCommandError(
            "invalid",
            f"unit_scope must be one of: {', '.join(fleet_roster.UNIT_SCOPES)}",
        )
    return scope


def _pubkey(value: Any) -> str | None:
    pubkey = str(value or "").strip().lower()
    if not pubkey:
        return None
    if not re.fullmatch(PUBKEY_PATTERN, pubkey):
        raise FleetSeatCommandError(
            "invalid", "buzz_pubkey must be 64 hexadecimal characters"
        )
    return pubkey


_NORMALIZE = {
    "name": _name,
    "email": _email,
    "kind": _kind,
    "unit": _unit,
    "unit_scope": _unit_scope,
    "buzz_pubkey": _pubkey,
}


def _refuse_taken_email(
    conn: sqlite3.Connection, email: str | None, *, slug: str
) -> None:
    if email is None:
        return
    holder = fleet_roster.find_seat_by_email(conn, email)
    if holder is not None and holder["slug"] != slug:
        raise FleetSeatCommandError(
            "conflict", f"{email} already belongs to seat {holder['slug']}"
        )


def _describe(seat: Mapping[str, Any]) -> str:
    unit = seat.get("unit")
    where = "none" if not unit else f"{unit} [{seat['unit_scope']}]"
    return (
        f"{seat['slug']} ({seat['name']}, {seat['kind']}): "
        f"email {seat.get('email') or 'none'}, unit {where}"
    )


def declare_seat(
    conn: sqlite3.Connection,
    *,
    actor: Mapping[str, Any],
    slug: str,
    name: str,
    email: str | None = None,
    kind: str = "seat",
    unit: str | None = None,
    unit_scope: str = "user",
    buzz_pubkey: str | None = None,
) -> dict:
    """Declare one seat and record ``declared_fleet_seat``. Admin only."""
    _require_admin(actor)
    key = _slug(slug)
    email_key = _email(email)
    row = (
        key,
        _name(name),
        email_key,
        _unit(unit),
        _unit_scope(unit_scope),
        _pubkey(buzz_pubkey),
        _kind(kind),
    )
    with db.transaction(conn, immediate=True):
        if fleet_roster.find_declared_seat(conn, key) is not None:
            raise FleetSeatCommandError("conflict", f"seat {key} is already declared")
        _refuse_taken_email(conn, email_key, slug=key)
        cursor = conn.execute(
            "INSERT INTO fleet_seats "
            "(slug, name, email, unit, unit_scope, buzz_pubkey, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        seat_id = int(cursor.lastrowid or 0)
        seat = fleet_roster.find_declared_seat(conn, key)
        assert seat is not None and seat["id"] == seat_id
        activity.record(
            conn,
            actor_id=int(actor["id"]),
            verb=VERB_DECLARED,
            target_kind=TARGET_KIND,
            target_id=seat_id,
            detail=_describe(seat),
            commit=False,
        )
    return seat


def update_seat(
    conn: sqlite3.Connection,
    *,
    actor: Mapping[str, Any],
    slug: str,
    changes: Mapping[str, Any],
) -> dict:
    """Change the named fields of one seat and record ``edited_fleet_seat``.

    A field absent from ``changes`` is kept; a nullable field given as None or
    empty is cleared. The slug is not editable. Admin only."""
    _require_admin(actor)
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise FleetSeatCommandError(
            "invalid", f"not an editable seat field: {', '.join(unknown)}"
        )
    for field in ("name", "kind", "unit_scope"):
        if field in changes and changes[field] is None:
            raise FleetSeatCommandError("invalid", f"{field} cannot be cleared")
    normalized = {field: _NORMALIZE[field](value) for field, value in changes.items()}
    key = str(slug or "").strip().lower()
    with db.transaction(conn, immediate=True):
        before = fleet_roster.find_declared_seat(conn, key)
        if before is None:
            raise FleetSeatCommandError("not_found", f"no declared seat {key}")
        if "email" in normalized:
            _refuse_taken_email(conn, normalized["email"], slug=before["slug"])
        changed = {
            field: value
            for field, value in normalized.items()
            if before.get(field) != value
        }
        if not changed:
            return before
        assignments = ", ".join(f"{field} = ?" for field in changed)
        conn.execute(
            f"UPDATE fleet_seats SET {assignments}, updated_at = datetime('now') "
            "WHERE id = ?",
            (*changed.values(), before["id"]),
        )
        seat = fleet_roster.find_declared_seat(conn, before["slug"])
        assert seat is not None
        activity.record(
            conn,
            actor_id=int(actor["id"]),
            verb=VERB_EDITED,
            target_kind=TARGET_KIND,
            target_id=int(seat["id"]),
            detail=(
                f"{seat['slug']}: "
                + "; ".join(
                    f"{field} {before.get(field) or 'none'} -> "
                    f"{seat.get(field) or 'none'}"
                    for field in changed
                )
            ),
            commit=False,
        )
    return seat


def remove_seat(
    conn: sqlite3.Connection, *, actor: Mapping[str, Any], slug: str
) -> dict:
    """Remove one seat and record ``removed_fleet_seat`` with what it held.

    The account the seat named is untouched: it reads as an agent no seat
    declares. Returns the seat as it was and the event's id. Admin only."""
    _require_admin(actor)
    key = str(slug or "").strip().lower()
    with db.transaction(conn, immediate=True):
        seat = fleet_roster.find_declared_seat(conn, key)
        if seat is None:
            raise FleetSeatCommandError("not_found", f"no declared seat {key}")
        conn.execute("DELETE FROM fleet_seats WHERE id = ?", (seat["id"],))
        event = activity.record(
            conn,
            actor_id=int(actor["id"]),
            verb=VERB_REMOVED,
            target_kind=TARGET_KIND,
            target_id=int(seat["id"]),
            detail=_describe(seat),
            commit=False,
        )
    return {"seat": seat, "event_id": int(event["id"])}
