"""One operator action: put an issue on a seat's desk, then radio Buzz.

Writes go through Aegis issue commands. The Buzz ping is optional and never
rolls back a successful assign.
"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
from typing import Literal

from athena import config
from athena.aegis import issue_commands, issues
from athena.core import activity, buzz_radio, db, fleet_roster, users


#: The refusal kinds this command raises. A transport maps each to its own
#: answer (``fleet_assign_api.STATUS_BY_KIND``); the cockpit shows the detail.
#: An unknown seat, the operator's seat, a seat with no handle or no agent
#: account are ``invalid`` (the seat is in the request body, like an account id
#: the issue commands refuse as ``invalid``); a missing issue is ``not_found``
#: with the issue commands' own detail, so it reads exactly as a hidden one.
AssignErrorKind = Literal["invalid", "not_found", "conflict"]


class AssignError(Exception):
    def __init__(self, kind: AssignErrorKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def assign_issue_to_seat(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    issue_id: int,
    seat_slug: str,
    note: str = "",
    radio: Callable[..., dict] | None = None,
) -> dict:
    spec = fleet_roster.find_declared_seat(conn, seat_slug)
    if spec is None:
        raise AssignError(
            "invalid",
            f"unknown seat {seat_slug!r}; GET /fleet/seats/directory "
            "(MCP: list_fleet_seats) lists the seat slugs",
        )
    if spec.get("kind") == "operator":
        raise AssignError("invalid", "assign work to an agent seat, not the operator")
    email = spec.get("email")
    if not email:
        raise AssignError("invalid", f"{spec['name']} has no Athena handle yet")
    if conn.in_transaction:
        # The radio can only announce a durable transfer. A caller-owned
        # transaction could still roll back after our savepoint returns.
        raise AssignError(
            "conflict",
            "fleet assignment requires a connection without an open transaction",
        )

    with db.transaction(conn, immediate=True):
        target = users.get_user_by_email(conn, str(email))
        if target is None or not target.get("is_agent"):
            raise AssignError("invalid", f"{spec['name']} has no Athena agent account")
        if issues.get_issue(conn, issue_id) is None:
            raise AssignError("not_found", "no such issue")

        # The current assignee may hand off work even if this transfer ends
        # their authority. Delegate while both commands can check that authority.
        issue_commands.add_contributor(
            conn,
            actor=actor,
            issue_id=issue_id,
            user_id=int(target["id"]),
            require_agent=True,
        )
        updated = issue_commands.update_issue(
            conn, actor=actor, issue_id=issue_id, assignee_id=int(target["id"])
        )
    key = updated.get("key") or f"#{updated['id']}"
    base = config.public_base_url()
    url = (
        f"{base}/aegis/issues/{updated['id']}"
        if base
        else f"/aegis/issues/{updated['id']}"
    )
    sender = radio if radio is not None else buzz_radio.send_assignment
    ping = sender(
        seat_name=str(spec["name"]),
        buzz_pubkey=spec.get("buzz_pubkey"),
        issue_key=str(key),
        title=str(updated.get("title") or ""),
        url=url,
        note=note,
    )
    receipt = _record_radio_receipt(
        conn,
        actor=actor,
        issue_id=int(updated["id"]),
        seat_name=str(spec["name"]),
        issue_key=str(key),
        ping=ping,
    )
    return {
        "issue_id": updated["id"],
        "issue_key": key,
        "issue_title": updated.get("title"),
        "seat": spec["slug"],
        "seat_name": spec["name"],
        "agent_id": target["id"],
        "radio": ping,
        "receipt": receipt,
    }


def _record_radio_receipt(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    issue_id: int,
    seat_name: str,
    issue_key: str,
    ping: dict,
) -> dict:
    """Write the ``radioed_assignment`` row that ties an assign to its ping.

    Only a ping that both landed AND produced a usable permalink earns a row.
    A receipt whose whole purpose is to be followed is worth nothing without
    somewhere to follow it to, and an event with no link would just be a second
    way of saying what ``assigned`` already said.

    This never raises into the assign. The module's contract is that the radio
    is optional and never rolls back a successful assignment, and that has to
    hold for the audit row as much as for the ping: the desk write is already
    committed by the time we get here, so letting a bookkeeping failure escape
    would report a completed assign as an error. The failure is returned rather
    than swallowed, so the caller can see it happened.
    """
    if ping.get("status") != "sent":
        return {"status": "skipped", "detail": f"radio {ping.get('status')}"}
    permalink = ping.get("permalink")
    if not permalink:
        return {"status": "skipped", "detail": "ping landed without a usable receipt"}
    try:
        event = activity.record(
            conn,
            actor_id=int(actor["id"]),
            verb=buzz_radio.VERB_RADIOED,
            target_kind="issue",
            target_id=issue_id,
            detail=f"{issue_key} radioed to {seat_name} — {permalink}",
            commit=True,
        )
    except sqlite3.Error as exc:  # reported to the caller, never raised into the assign
        return {"status": "failed", "detail": f"receipt not recorded: {exc}"}
    return {
        "status": "recorded",
        "activity_id": event.get("id"),
        "permalink": permalink,
    }
