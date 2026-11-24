"""The Intervene nav's counts for one request: read on first use, at most once.

The session middleware used to build the fleet-attention rollup for every request
an admin's cookie rode on: JSON routes, health checks, 404s, and twice on the
dashboard (once for the badge, once for the card). The counts are only ever
shown by a page that renders the nav, or by the attention card's live refresh,
so they are read there, when a template first asks, and kept for the rest of
the request. The dashboard hands the same ``needs_you`` result to its card, so
the badge and the card in one response are one read (U6).
"""

from __future__ import annotations

from functools import cached_property
import sqlite3

from athena.aegis import fleet_attention
from athena.core import notifications


class NavCounts:
    """The unread inbox count and the "needs you" rollup for one request.

    ``needs_you`` is admin-only (every input is an admin-scoped read) and is
    ``None`` for anyone else, so a member's badge carries nothing, not zeros."""

    def __init__(self, conn: sqlite3.Connection | None, user: dict | None):
        self._conn = conn
        self._user = user

    @cached_property
    def unread_count(self) -> int:
        """Gated by visibility, so it matches the inbox: an unread notification
        on a target the user can no longer see does not inflate the count."""
        if self._conn is None or self._user is None:
            return 0
        return notifications.unread_count(
            self._conn, self._user["id"], actor=self._user
        )

    @cached_property
    def needs_you(self) -> dict | None:
        """``fleet_attention.needs_you`` for this admin, or ``None``."""
        if (
            self._conn is None
            or self._user is None
            or self._user.get("role") != "admin"
        ):
            return None
        return fleet_attention.needs_you(self._conn, actor=self._user)

    @property
    def attention_total(self) -> int:
        counts = self.needs_you
        return 0 if counts is None else int(counts["total"])

    @property
    def attention_signals(self) -> list[dict]:
        counts = self.needs_you
        return [] if counts is None else list(counts["signals"])
