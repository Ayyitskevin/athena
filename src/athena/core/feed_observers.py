"""What the event feed checks on entry, before it reads (decision D11).

A caller in a ``wait_for_events`` loop must learn of a fact the clock made and
nobody wrote down, such as a lease that lapsed with nobody settling it (C4).
The feed is the observer: on entry, before its read, it asks each registered
observer to record what this caller is accountable for and has not been told.
An observer runs its read with no transaction open and writes, if anything, in
its own immediate transaction, so the feed's read never holds the writer lock.

``core`` owns the feed and may not import ``aegis`` (the import contract), so
the composition root registers the observers, as it registers undo's inverses.
Registration is idempotent: every test builds its own app in one process.
"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3

#: One observer: given the request's connection and its resolved actor, record
#: what it observes. It must leave the connection with no transaction open and
#: must not raise for a failed write: the read goes out regardless.
Observer = Callable[[sqlite3.Connection, dict], object]

_OBSERVERS: dict[str, Observer] = {}


def register(name: str, observer: Observer) -> None:
    """Add an observer under ``name``. Re-registering the same one is a no-op;
    a different observer under a taken name is a bug, and raises."""
    existing = _OBSERVERS.get(name)
    if existing is not None and existing is not observer:
        raise ValueError(f"feed observer '{name}' is already registered")
    _OBSERVERS[name] = observer


def observe(conn: sqlite3.Connection, actor: dict) -> None:
    """Run every registered observer for this caller, in registration order."""
    for observer in tuple(_OBSERVERS.values()):
        observer(conn, actor)
