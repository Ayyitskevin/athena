"""One timestamp shape and one expiry rule for agent packets (A13).

Storage keeps the shapes it has always had (``2026-11-21 18:19:13`` from
SQLite's ``datetime('now')``, ``2026-11-21T18:19:13Z`` in budgets, an
``isoformat()`` with microseconds and ``+00:00`` in two conductor reads). An
agent packet is not storage: it is what an agent parses, and one answer used to
carry all three. This module is the edge where packets take one shape:

- every timestamp is RFC 3339 UTC to the second, ``2026-11-21T18:19:13Z``
  (:data:`SHAPE`);
- every expiry ``<x>expires_at`` has ``<x>expires_in_seconds`` beside it,
  whole seconds from the packet's own clock read to the expiry, never below 0.

Two doors, one conversion (:func:`rfc3339`):

- :func:`stamp` walks a packet a REST adapter is about to return, rewriting
  every timestamp key (``*_at``, plus :data:`TIMESTAMP_KEYS`) and adding the
  expiry seconds from the ``now`` the packet was built with. A new ``*_at``
  field takes the shape without anyone remembering to;
- :data:`Timestamp` is the type a response model gives a timestamp field. It
  serializes through :func:`rfc3339` and declares ``format: date-time`` in the
  OpenAPI documents, so the declaration and the value cannot disagree, and a
  write answer that shares a packet's model (a claim answering the lease) takes
  the shape too.

Nothing here touches storage, a stored comparison or a validator: ETags and
cursors are computed from what they always were. The browser formats its own
times and never sees this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import re
from typing import Annotated, Any

from pydantic import PlainSerializer, WithJsonSchema

from athena.core import clock as app_clock

#: The one shape: RFC 3339, UTC, whole seconds.
SHAPE = "%Y-%m-%dT%H:%M:%SZ"
PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
#: The JSON-schema format every timestamp in an agent packet declares.
FORMAT = "date-time"

#: Timestamp fields whose names do not end in ``_at``.
TIMESTAMP_KEYS = frozenset({"at", "since", "freshness"})
EXPIRY_SUFFIX = "expires_at"
SECONDS_SUFFIX = "expires_in_seconds"

# A date and a time of day, optional fraction, and UTC as Z, +00:00 or nothing
# (SQLite's datetime('now') is UTC and says so by saying nothing).
_STORED = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]00:?00)?$"
)


def clock() -> datetime:
    """One clock read for one packet: the instant its verdicts are made at,
    from the app's one clock (``core/clock.py``, G6)."""
    return app_clock.now()


def rfc3339(value: Any) -> Any:
    """``value`` in the one shape when it is a UTC timestamp; anything else
    (null, a date alone, prose, a non-UTC offset) is returned unchanged."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime(SHAPE)
    if isinstance(value, str):
        match = _STORED.match(value)
        if match:
            return f"{match.group(1)}T{match.group(2)}Z"
    return value


def parse(value: str) -> datetime:
    """A stored or packet timestamp as an aware UTC instant, to the second."""
    shaped = rfc3339(value)
    return datetime.strptime(shaped, SHAPE).replace(tzinfo=UTC)


def stored(value: str | None) -> str | None:
    """A timestamp an agent sends back, in the shape storage compares against
    (``2026-11-21 18:19:13``). A packet hands out the one shape; a parameter
    that is compared with stored text as a string must not treat it as later
    than every stored time of the same day, which a bare comparison would
    (``T`` sorts after a space). Anything that is not a UTC timestamp is
    returned unchanged, for the comparison's own validation to judge."""
    if value is None:
        return None
    match = _STORED.match(value)
    return f"{match.group(1)} {match.group(2)}" if match else value


def seconds_until(expires_at: str | None, *, now: datetime) -> int | None:
    """Whole seconds from ``now`` to ``expires_at``; 0 once it has passed.

    ``now`` is truncated to the second, as every stored expiry is and as the
    lease and run-control clocks compare, so a positive answer is exactly the
    clock's "still active" verdict at the same read."""
    if expires_at is None:
        return None
    try:
        expiry = parse(expires_at)
    except ValueError:  # not a timestamp: no seconds to give
        return None
    remaining = expiry - now.astimezone(UTC).replace(microsecond=0)
    return max(0, int(remaining.total_seconds()))


def is_timestamp_key(key: str) -> bool:
    return key.endswith("_at") or key in TIMESTAMP_KEYS


def stamp(packet: Any, *, now: datetime) -> Any:
    """The packet with every timestamp in the one shape and every expiry
    followed by its seconds from ``now``, the packet's own clock read.

    Returns new containers and leaves ``packet`` as it was. An expiry whose
    owner already reports its seconds keeps the owner's value (a settled run
    control answers 0: its expiry no longer bounds anything)."""
    if isinstance(packet, list):
        return [stamp(item, now=now) for item in packet]
    if not isinstance(packet, Mapping):
        return packet
    out: dict[str, Any] = {}
    for key, value in packet.items():
        if is_timestamp_key(key) and isinstance(value, str):
            out[key] = rfc3339(value)
        else:
            out[key] = stamp(value, now=now)
        if key.endswith(EXPIRY_SUFFIX):
            seconds_key = key[: -len(EXPIRY_SUFFIX)] + SECONDS_SUFFIX
            if seconds_key not in packet:
                out[seconds_key] = (
                    seconds_until(value, now=now)
                    if isinstance(value, str) or value is None
                    else None
                )
    return out


def shape(packet: Any) -> Any:
    """The packet with every timestamp in the one shape, and nothing else.

    :func:`stamp` without the expiry seconds, for an answer whose clock read
    its reader does not have: the MCP client applies it to every REST answer
    it receives (K2.2), so an agent meets one timestamp shape whatever route
    answered. Returns new containers and leaves ``packet`` as it was."""
    if isinstance(packet, list):
        return [shape(item) for item in packet]
    if not isinstance(packet, Mapping):
        return packet
    return {
        key: rfc3339(value)
        if is_timestamp_key(key) and isinstance(value, str)
        else shape(value)
        for key, value in packet.items()
    }


def _serialize(value: str) -> str:
    shaped = rfc3339(value)
    return shaped if isinstance(shaped, str) else value


#: A response model's timestamp field: emitted in the one shape and declared
#: ``format: date-time``. ``Timestamp | None`` for a nullable one.
Timestamp = Annotated[
    str,
    PlainSerializer(_serialize, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": FORMAT}),
]
