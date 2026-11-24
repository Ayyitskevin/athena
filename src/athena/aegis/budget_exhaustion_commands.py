"""A seat out of budget while it holds work: who is told, and what it holds (F13).

A budget exhausted mid-task used to say nothing useful to anyone: the seat's
429 named no next step, the conductor that delegated the work learned nothing
until the lease lapsed, and the operator's trail gained a row for every refused
request (FLEET-13).

Two things live here, both asked by the app's refusal handler after the refused
command rolled back (``main.py``; ``core/budget_commands`` owns the exhaustion
itself and may not import ``aegis``):

* :func:`held_leases` is what the refusal carries, every time: the issue and
  generation of each lease the refused account holds, so it can yield each with
  reason ``capacity``, which is not metered.
* :func:`tell_accountable` runs on the first refusal of a budget window only. It
  records one ``holder_budget_exhausted`` event on each held issue and puts it
  in the inboxes of the two accounts that answer for the work, the issue's
  assignee and the account that delegated it to the holder, as a lapse does
  (C4). The holder is not told again: it was refused to its face.

Nothing here releases a lease or changes an issue: ruling K1 stands. The holder
yields, or the lease lapses and says so.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import accountable, issue_activity, leases
from athena.core import db, notifications, users

#: A seat holds one lease by the pickup rule and a handful at most by direct
#: claims; the bound keeps one refusal's answer and writes small whatever an
#: account managed to collect.
MAX_HELD_LEASES = 20


def held_leases(conn: sqlite3.Connection, holder_id: int) -> list[dict]:
    """The issue and generation of each live lease ``holder_id`` holds, by
    issue id. Its own possessions: it reads them on its desk already."""
    held = leases.active_leases_held_by(
        conn, holder_id=holder_id, limit=MAX_HELD_LEASES
    )
    return sorted(
        (
            {"issue_id": int(lease["issue_id"]), "generation": lease["generation"]}
            for lease in held
        ),
        key=lambda lease: lease["issue_id"],
    )


def tell_accountable(
    conn: sqlite3.Connection, *, holder_id: int, retry_after_seconds: int
) -> list[dict]:
    """Record that ``holder_id`` ran out of budget on each issue it holds, and
    tell who answers for that work. Returns the events written.

    Call it once per budget window, on the refusal ``record_exhaustion``
    recorded. One transaction: the events and their inbox rows commit or roll
    back together."""
    with db.transaction(conn, immediate=True):
        holder = users.get_user(conn, holder_id)
        if holder is None:
            return []
        events = []
        for lease in held_leases(conn, holder_id):
            event = issue_activity.record_holder_budget_exhausted(
                conn,
                holder_id=holder_id,
                holder_name=str(holder["name"]),
                issue_id=lease["issue_id"],
                generation=lease["generation"],
                retry_after_seconds=retry_after_seconds,
                commit=False,
            )
            notifications.notify_users(
                conn,
                event_id=event["id"],
                user_ids=accountable.answering_for(
                    conn, holder_id=holder_id, issue_id=lease["issue_id"]
                ),
            )
            events.append(event)
        return events
