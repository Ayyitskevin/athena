"""One operator action: put an issue on a seat's desk, then radio Buzz.

Writes go through Aegis issue commands. The Buzz ping is optional and never
rolls back a successful assign.
"""

from __future__ import annotations

import sqlite3

from athena import config
from athena.aegis import issue_commands, issues
from athena.core import buzz_radio, fleet_roster, users


class AssignError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def assign_issue_to_seat(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    issue_id: int,
    seat_slug: str,
    note: str = "",
    radio: object | None = None,
) -> dict:
    spec = fleet_roster.find_declared_seat(seat_slug)
    if spec is None:
        raise AssignError(f"unknown seat {seat_slug!r}")
    if spec.get("kind") == "operator":
        raise AssignError("assign work to an agent seat, not the operator")
    email = spec.get("email")
    if not email:
        raise AssignError(f"{spec['name']} has no Athena handle yet")
    target = users.get_user_by_email(conn, str(email))
    if target is None or not target.get("is_agent"):
        raise AssignError(f"{spec['name']} has no Athena agent account")

    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        raise AssignError("issue not found")

    updated = issue_commands.update_issue(
        conn,
        actor=actor,
        issue_id=issue_id,
        assignee_id=int(target["id"]),
    )
    issue_commands.add_contributor(
        conn,
        actor=actor,
        issue_id=issue_id,
        user_id=int(target["id"]),
        require_agent=True,
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
    return {
        "issue_id": updated["id"],
        "issue_key": key,
        "issue_title": updated.get("title"),
        "seat": spec["slug"],
        "seat_name": spec["name"],
        "agent_id": target["id"],
        "radio": ping,
    }
