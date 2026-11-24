"""What a pending approval asks about, as far as the viewer may read it (U9).

COCKPIT-11: the queue named the requester, the action kind and "issue #8", so
the operator had to leave the queue to learn what they were deciding. This read
adds, per request, the target issue's key and title and, where the gated action
closes the issue (``issue.close``, the act that follows a completion), the
completion the ask rests on: who reported it, when, and its verdict.

**Which completion.** The ask is tied to the moment it was made: the newest
native ``claim_completed`` on the issue recorded before the request's own
``approval_requested`` event, the requester's own when it has one, otherwise the
newest by any account. A completion recorded after the ask never replaces it; it
is reported beside it as ``newer``, so a later BLOCKED cannot silently turn the
PASS the operator is approving into something else, and cannot hide either.

Nothing here is new authority or a new fact. The issue is read only when the
viewer may see it (``access.can_see_issue``) and every completion only through
the trail's own visibility gate (``activity.list_activity`` with ``actor=``), so
a row never tells a viewer more than the issue page and the trail already
would. A verdict is what an account asserted, parsed by the same reader every
other surface uses (``lease_commands.reported_completion``); Athena has not
checked it. Read-only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import sqlite3
from typing import Any

from athena.aegis import issues, lease_commands
from athena.core import access, activity, approvals

_COMPLETED = "claim_completed"


def _ask_event_id(conn: sqlite3.Connection, request: Mapping[str, Any]) -> int | None:
    """The request's own ``approval_requested`` event: the instant of the ask,
    in trail order (timestamps share a second; event ids do not)."""
    row = conn.execute(
        "SELECT MIN(id) AS id FROM activity WHERE target_kind = 'issue' "
        "AND target_id = ? AND verb = ? AND detail = ? AND imported_at IS NULL",
        (
            request["target_id"],
            approvals.VERB_REQUESTED,
            f"{request['action_kind']} (approval #{request['id']})",
        ),
    ).fetchone()
    return None if row is None or row["id"] is None else int(row["id"])


def _completion(event: dict, requester_id: int) -> dict[str, Any]:
    report = lease_commands.reported_completion(event)
    return {
        "event_id": int(event["id"]),
        "by": event["actor_name"],
        "by_requester": int(event["actor_id"]) == requester_id,
        "at": event["created_at"],
        "verdict": None if report is None else report.get("verdict"),
    }


def _completions(
    conn: sqlite3.Connection,
    viewer: dict | None,
    request: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The completion the ask rests on, and a newer one if any exists."""
    issue_id = request["target_id"]
    requester_id = int(request["requested_by"])
    ask_id = _ask_event_id(conn, request)

    def newest(*, actor_id: int | None = None, before_id: int | None = None):
        rows = activity.list_activity(
            conn,
            target_kind="issue",
            target_id=issue_id,
            actor_id=actor_id,
            verb=_COMPLETED,
            before_id=before_id,
            limit=1,
            actor=viewer,
        )
        return rows[0] if rows else None

    based = None
    if ask_id is not None:
        based = newest(actor_id=requester_id, before_id=ask_id) or newest(
            before_id=ask_id
        )
    latest = newest()
    newer = (
        latest
        if latest is not None and (ask_id is None or int(latest["id"]) > ask_id)
        else None
    )
    return (
        None if based is None else _completion(based, requester_id),
        None if newer is None else _completion(newer, requester_id),
    )


def describe(
    conn: sqlite3.Connection,
    *,
    viewer: dict | None,
    requests: Iterable[Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Per request id: ``issue`` ({id, key, title}) or None; for a close,
    ``completion`` (the one the ask rests on: ``by``, ``by_requester``, ``at``,
    ``verdict``) and ``newer`` (a completion recorded after the ask), each None
    when there is none the viewer may read.

    ``issue`` is None for a non-issue target or one the viewer cannot see, and
    then nothing else is read."""
    described: dict[int, dict[str, Any]] = {}
    for request in requests:
        entry: dict[str, Any] = {"issue": None, "completion": None, "newer": None}
        described[int(request["id"])] = entry
        if request["target_kind"] != "issue" or not access.can_see_issue(
            conn, viewer, request["target_id"]
        ):
            continue
        issue = issues.get_issue(conn, request["target_id"])
        if issue is None:
            continue
        entry["issue"] = {
            "id": issue["id"],
            "key": issue.get("key"),
            "title": issue["title"],
        }
        if request["action_kind"] == approvals.ACTION_ISSUE_CLOSE:
            entry["completion"], entry["newer"] = _completions(conn, viewer, request)
    return described
