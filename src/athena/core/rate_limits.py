"""Small in-process rate limits for API traffic.

Athena is intentionally single-process/local-first today, so the first safety
step for runaway agents (and credential-free hammering) can stay process-local.
Durable rate-limit accounting would be a larger deployment feature; this module
only bounds one app instance.

The one limiter is a fixed-window counter keyed by any hashable identity: a
bearer token's id for the per-token limit, or an anonymous caller's client IP for
optional-identity REST reads and signed-inbound attempts. Same mechanics,
different key; it is not a global browser-request ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import Lock
import time
from typing import Callable, Hashable

WINDOW_SECONDS = 60.0

# A sweep is O(keys), so doing one on every check() made per-request cost scale
# with the number of distinct callers seen — worst under exactly the traffic the
# limiter exists to absorb. Entries are retained for WINDOW_SECONDS * 2 anyway,
# so sweeping more than once per window evicts nothing new; throttling to one
# sweep per window leaves the memory bound identical and makes per-call work
# O(1) amortized.
_PRUNE_INTERVAL_SECONDS = WINDOW_SECONDS
# Escape hatch: a burst of distinct keys inside a single window would otherwise
# sit unswept until the interval elapses. Far above any real caller count, so it
# never fires in normal operation — it exists so "amortized" cannot become
# "unbounded" under a pathological burst.
_PRUNE_SIZE_ESCAPE = 10_000


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class FixedWindowRateLimiter:
    """Fixed-window limiter keyed by any hashable identity (a token id or a client IP).

    The state is operational back-pressure, not domain data. It resets on process
    restart, which is acceptable for local-alpha agent-loop safety and keeps the
    app free of another persistent coordination surface.
    """

    def __init__(
        self,
        limit_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit_per_minute = max(0, limit_per_minute)
        self._clock = clock
        self._windows: dict[Hashable, tuple[float, int]] = {}
        self._lock = Lock()
        # None means "never swept", so the first check() always sweeps and
        # establishes the cadence from the caller's clock rather than from
        # import time.
        self._last_prune: float | None = None

    def check(self, key: Hashable) -> RateLimitDecision:
        if self.limit_per_minute <= 0:
            return RateLimitDecision(
                allowed=True,
                limit=0,
                remaining=0,
                retry_after_seconds=0,
            )

        now = self._clock()
        with self._lock:
            self._maybe_prune(now)
            window_start, count = self._windows.get(key, (now, 0))
            if now >= window_start + WINDOW_SECONDS:
                window_start, count = now, 0

            retry_after = self._retry_after(now, window_start)
            if count >= self.limit_per_minute:
                self._windows[key] = (window_start, count)
                return RateLimitDecision(
                    allowed=False,
                    limit=self.limit_per_minute,
                    remaining=0,
                    retry_after_seconds=retry_after,
                )

            count += 1
            self._windows[key] = (window_start, count)
            return RateLimitDecision(
                allowed=True,
                limit=self.limit_per_minute,
                remaining=max(0, self.limit_per_minute - count),
                retry_after_seconds=retry_after,
            )

    def _retry_after(self, now: float, window_start: float) -> int:
        return max(1, math.ceil(window_start + WINDOW_SECONDS - now))

    def _maybe_prune(self, now: float) -> None:
        """Sweep at most once per window, or immediately if the map has blown up.

        Callers hold the lock. Kept separate from ``_prune`` so the sweep itself
        stays trivially testable without the throttle in the way.
        """
        if (
            self._last_prune is not None
            and now - self._last_prune < _PRUNE_INTERVAL_SECONDS
            and len(self._windows) < _PRUNE_SIZE_ESCAPE
        ):
            return
        self._last_prune = now
        self._prune(now)

    def _prune(self, now: float) -> None:
        expired_before = now - (WINDOW_SECONDS * 2)
        stale = [
            key
            for key, (window_start, _) in self._windows.items()
            if window_start < expired_before
        ]
        for key in stale:
            del self._windows[key]


# The per-token limiter is exactly this fixed-window limiter keyed by token id. The
# alias keeps that name (and its call sites/tests) meaningful now that the same class
# also backs the anonymous-IP limiter.
TokenRateLimiter = FixedWindowRateLimiter
