"""A bounded wait on the events feed: park one request instead of running a sleep loop.

`GET /events?after=N` answers at once. With `wait=S` the same request may instead
park for up to `S` seconds and return as soon as a visible event lands. The rules
that make that affordable on a one-process, one-SQLite server:

* **Waiting costs no thread, no transaction and no lock.** The route is a coroutine:
  between polls it only sleeps on the event loop. Each poll borrows a worker thread
  for one bounded SELECT (the very same gated query the immediate read runs), then
  returns it. The per-request connection is held across the wait, but with no
  statement in flight and nothing `in_transaction`, so WAL checkpoints and writers
  are unaffected, and `RequestConnection`'s clean-handoff invariant holds.
* **Capped twice.** At most ``EVENT_WAIT_MAX_WAITERS`` requests may wait at once per
  process, and none for longer than ``EVENT_WAIT_MAX_SECONDS``. Both sit well below
  the server's connection ceiling (``limit_concurrency=100`` in ``ops.serve``) and
  the MCP client's 30 s request timeout, so ordinary requests always have room and
  a parked call cannot outlive its caller's patience.
* **Degrades, never errors.** With no free slot the answer is today's immediate
  answer, labelled ``capacity`` so a client can back off. A client that went away
  frees its slot at the next tick instead of holding it to the deadline.
* **Visibility is the immediate read's.** The poll *is* `activity.list_events` with
  the caller's actor, so a waiter can only wake on a batch it may read. An event it
  may not see produces no rows and therefore no early return: timing leaks nothing.

The wake bound is one poll interval plus one query. The supported deployment shape
is one process (``docs/OPERATIONS.md``), so the slot cap is per process; a
deliberate multi-worker experiment multiplies it accordingly.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Literal

#: Longest wait a request may ask for, in whole seconds. Validated like `limit`:
#: above it is a 422 naming the bound, not a silent clamp.
EVENT_WAIT_MAX_SECONDS = 25
#: Requests that may be parked at once, per process. Keep this well under the
#: server's connection ceiling: parked requests count against it.
EVENT_WAIT_MAX_WAITERS = 32
#: How often a parked request re-runs its gated read. The wake bound.
EVENT_WAIT_POLL_SECONDS = 0.5

WaitOutcome = Literal["event", "timeout", "capacity", "disconnected"]

Poll = Callable[[], Awaitable[list[dict]]]
Disconnected = Callable[[], Awaitable[bool]]


class WaiterSlots:
    """A counted set of parking places. Acquire fails past capacity; it never blocks.

    All route use happens on the one event loop, but the lock makes the count safe
    from any thread so a test or a tool can read it without a race.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(0, int(capacity))
        self._active = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self._capacity:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1


#: The process-wide slots the events route parks in. Tests swap it.
SLOTS = WaiterSlots(EVENT_WAIT_MAX_WAITERS)


async def _never_disconnected() -> bool:
    return False


async def wait_for_events(
    poll: Poll,
    *,
    timeout: float,
    slots: WaiterSlots,
    interval: float = EVENT_WAIT_POLL_SECONDS,
    is_disconnected: Disconnected | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[list[dict], WaitOutcome, float]:
    """Poll until `poll` returns rows, the deadline passes, or the client is gone.

    Returns ``(rows, outcome, elapsed_seconds)``. With no free slot it runs `poll`
    once and reports ``capacity`` with zero elapsed: exactly the immediate answer.
    The slot is released on every exit, including a raising poll.
    """
    disconnected = is_disconnected or _never_disconnected
    if not slots.try_acquire():
        return await poll(), "capacity", 0.0
    started = clock()
    try:
        while True:
            rows = await poll()
            if rows:
                return rows, "event", clock() - started
            remaining = timeout - (clock() - started)
            if remaining <= 0:
                return [], "timeout", clock() - started
            if await disconnected():
                return [], "disconnected", clock() - started
            await sleep(min(interval, remaining))
    finally:
        slots.release()
