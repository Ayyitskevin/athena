"""Athena's one clock (G6): where the app reads "now".

:func:`now` is the current instant, UTC and aware. The lease lifecycle reads it
for every verdict it makes: whether a lease is live or lapsed, the expiry a
claim grants or a renewal extends, and the lapse the event feed observes
(``aegis/leases.py``, ``lease_commands``, ``lease_lapse_commands`` and the
reads that pass their ``now`` into them). ``core/_util.utc_now`` and
``core/packet_time.clock`` read it too, so a packet's countdown and the
verdict beside it come from the same clock.

In a running app it is the system clock, always. Nothing in ``src/athena``
replaces it, and no setting, route or tool can: the only seam is the private
``_source`` below, which a test replaces through ``tests/conftest.py``'s
``clock`` fixture (``monkeypatch``, undone when the test ends). An autouse
guard there fails a test that leaves it replaced, so a frozen clock cannot
reach the next test.

SQLite has a clock of its own, ``datetime('now')``. The lease code binds its
comparisons and its stamps from :func:`now` instead, but the triggers
migrations 0057 and 0058 put on ``issue_leases`` and ``issue_claim_handoffs``
decide liveness with SQLite's, and an applied migration is never edited. So
while the clock is replaced, :func:`athena.core.db.connect` installs
:func:`sqlite_datetime` as each new connection's ``datetime()``, which reads
``'now'`` from this clock. With the system clock it installs nothing: SQLite
reads its own clock, which is the same system clock, and production SQL runs
exactly as it always has. Only ``datetime()`` is bridged; ``julianday``,
``strftime``, ``unixepoch`` and ``CURRENT_TIMESTAMP`` keep SQLite's clock, and
no lease decision uses them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import sqlite3
import threading


def _system() -> datetime:
    return datetime.now(UTC)


#: Where :func:`now` reads. The system clock in every running app; a test
#: replaces it for its own duration only (see the module docstring).
_source: Callable[[], datetime] = _system


def now() -> datetime:
    """The current instant, UTC and timezone-aware."""
    return _source()


def replaced() -> bool:
    """Whether a test has replaced the system clock. Always False in an app."""
    return _source is not _system


# SQLite's own datetime(), on a private in-memory connection that never has the
# bridge installed: the bridge hands it every argument, with 'now' spelled out.
_plain: sqlite3.Connection | None = None
_plain_lock = threading.Lock()


def sqlite_datetime(*args: object) -> str | None:
    """SQLite's ``datetime(...)`` with ``'now'`` read from :func:`now`.

    ``'now'`` (SQLite matches it in any case, and a bare ``datetime()`` means
    it) becomes this clock's instant to the millisecond, as SQLite's own
    ``'now'`` is; every other argument and modifier is SQLite's to evaluate,
    so the answer has the shape and the rules ``datetime()`` always had."""
    global _plain
    values = list(args) or ["now"]
    if isinstance(values[0], str) and values[0].lower() == "now":
        values[0] = now().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    placeholders = ", ".join("?" * len(values))
    with _plain_lock:
        if _plain is None:
            _plain = sqlite3.connect(":memory:", check_same_thread=False)
        return _plain.execute(f"SELECT datetime({placeholders})", values).fetchone()[0]
