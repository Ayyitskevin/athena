"""Acknowledging notifications: one call for one, a set, or everything up to a
bound (F7, D18).

``core/notifications.py`` owns the inbox rows and their visibility gate; this
command owns the acknowledgement of a set, in one transaction, for
``POST /notifications/read_all`` and the ``mark_notification_read`` tool.

What it keeps:

- an account acknowledges only its own notifications, and only those whose
  event it can still see. An id that is another account's, does not exist, or
  is on a target hidden since is answered alike, under ``not_found``, and
  nothing about it changes;
- acknowledging is idempotent: an own notification already read is not "not
  found", and a repeat changes nothing and answers the same ``not_found``;
- ``through_id`` acknowledges the caller's unread notifications with an id up
  to and including it, and nothing newer: a notification that arrived after
  the caller's read keeps its unread state for the next read to find.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from athena.core import db, notifications

ErrorKind = Literal["invalid"]

#: The most ids one acknowledgement names. A larger inbox is acknowledged with
#: ``through_id``.
MAX_ACKNOWLEDGE_IDS = 200


class NotificationCommandError(Exception):
    """A refused acknowledgement. ``kind`` is the closed vocabulary adapters map
    to a status; the command knows no transport."""

    def __init__(self, kind: ErrorKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _positive_id(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise NotificationCommandError("invalid", f"{name} must be a positive integer")
    return value


def acknowledge(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    ids: list[int] | None = None,
    through_id: int | None = None,
) -> dict[str, Any]:
    """Mark the actor's own visible unread notifications read and return
    ``{count}``: how many changed. With ``ids`` the answer also carries
    ``not_found``, the ids that are not the actor's visible notifications, in the
    order given. With ``through_id``, every one up to and including it. With
    neither, every one, as ``read_all`` always has."""
    if ids is not None and through_id is not None:
        raise NotificationCommandError("invalid", "send ids or through_id, not both")
    if ids is not None:
        if not isinstance(ids, list | tuple) or not ids:
            raise NotificationCommandError("invalid", "ids must be a non-empty list")
        if len(ids) > MAX_ACKNOWLEDGE_IDS:
            raise NotificationCommandError(
                "invalid",
                f"ids takes at most {MAX_ACKNOWLEDGE_IDS}; use through_id for more",
            )
        ids = list(dict.fromkeys(_positive_id("ids", value) for value in ids))
    if through_id is not None:
        through_id = _positive_id("through_id", through_id)
    with db.transaction(conn, immediate=True):
        found = (
            notifications.visible_own_ids(conn, actor["id"], ids, actor=actor)
            if ids is not None
            else None
        )
        count = notifications.mark_read_bounded(
            conn,
            actor["id"],
            ids=None if found is None else sorted(found),
            through_id=through_id,
            actor=actor,
            commit=False,
        )
    answer: dict[str, Any] = {"count": count}
    if ids is not None:
        answer["not_found"] = [value for value in ids if value not in (found or set())]
    return answer
