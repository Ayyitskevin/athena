"""Small in-process rate limits for bearer-token API traffic.

Athena is intentionally single-process/local-first today, so the first safety
step for runaway agents can stay process-local. Durable rate-limit accounting
would be a larger deployment feature; this module only bounds one app instance.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import Lock
import time
from typing import Callable

WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class TokenRateLimiter:
    """Fixed-window limiter keyed by api_tokens.id.

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
        self._windows: dict[int, tuple[float, int]] = {}
        self._lock = Lock()

    def check(self, token_id: int) -> RateLimitDecision:
        if self.limit_per_minute <= 0:
            return RateLimitDecision(
                allowed=True,
                limit=0,
                remaining=0,
                retry_after_seconds=0,
            )

        now = self._clock()
        with self._lock:
            self._prune(now)
            window_start, count = self._windows.get(token_id, (now, 0))
            if now >= window_start + WINDOW_SECONDS:
                window_start, count = now, 0

            retry_after = self._retry_after(now, window_start)
            if count >= self.limit_per_minute:
                self._windows[token_id] = (window_start, count)
                return RateLimitDecision(
                    allowed=False,
                    limit=self.limit_per_minute,
                    remaining=0,
                    retry_after_seconds=retry_after,
                )

            count += 1
            self._windows[token_id] = (window_start, count)
            return RateLimitDecision(
                allowed=True,
                limit=self.limit_per_minute,
                remaining=max(0, self.limit_per_minute - count),
                retry_after_seconds=retry_after,
            )

    def _retry_after(self, now: float, window_start: float) -> int:
        return max(1, math.ceil(window_start + WINDOW_SECONDS - now))

    def _prune(self, now: float) -> None:
        expired_before = now - (WINDOW_SECONDS * 2)
        stale = [
            token_id
            for token_id, (window_start, _) in self._windows.items()
            if window_start < expired_before
        ]
        for token_id in stale:
            del self._windows[token_id]
