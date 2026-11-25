"""The background loops of a deployment run in one process (H13).

Webhook delivery and the automation engine each drain a queue the whole
deployment shares, so two processes running them deliver twice and fire a
rule twice. The process that runs them is the one holding an exclusive lock
on a file beside the database. Any other process serves requests, says so
once in its log, and reports it on ``/readyz``.

The lock is taken once, when the loops would start. A process that did not
get it does not take over when the holder exits: restart it to make it the
one that runs the loops.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import fcntl
import logging
import os
from pathlib import Path
from typing import Any

# What ``/readyz`` reports as ``background_loops``.
DISABLED = "disabled"  # both loops are turned off by their settings
STANDBY = "standby"  # this process is not running them
RUNNING = "running"  # this process holds the lock and runs each enabled loop

LOCK_SUFFIX = ".loops.lock"

_logger = logging.getLogger("athena")

Loop = Callable[[Any], Coroutine[Any, Any, None]]


def lock_path(db_path: str | Path) -> Path:
    """The lock file of the database at ``db_path``: its name, with a suffix."""
    return Path(f"{db_path}{LOCK_SUFFIX}")


def _take(path: Path) -> tuple[int | None, str]:
    """The descriptor that holds the exclusive lock on ``path``, or ``None``
    and why this process does not hold it. Never waits."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, BlockingIOError):
            return None, f"another process holds {path}"
        return None, f"{path} cannot be locked ({exc.strerror})"
    return descriptor, ""


SERVING_SUFFIX = ".serving.lock"


def serving_lock_path(db_path: str | Path) -> Path:
    """The file every process serving the database at ``db_path`` holds open."""
    return Path(f"{db_path}{SERVING_SUFFIX}")


def join_serving(db_path: str | Path) -> tuple[int | None, bool]:
    """Join the processes serving this database, and say whether this one is
    alone (H14).

    Every serving process holds a shared lock on the serving file for as long
    as it serves. A process that can first take the lock exclusively knows no
    other process is serving: whatever an earlier process left half done has
    no owner. It then keeps the lock shared. Returns the descriptor to close
    when serving ends, and whether this process was alone when it joined; a
    file that cannot be locked gives ``(None, False)``, which claims nothing.
    """
    descriptor = None
    try:
        descriptor = os.open(
            serving_lock_path(db_path),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            alone = True
        except BlockingIOError:
            alone = False
            # Waits only while a process that is starting holds it exclusively.
            fcntl.flock(descriptor, fcntl.LOCK_SH)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        return None, False
    return descriptor, alone


def keep_serving(descriptor: int) -> None:
    """Turn the exclusive lock ``join_serving`` took when alone into the shared
    one, once what only a lone process may do is done."""
    fcntl.flock(descriptor, fcntl.LOCK_SH)


class Loops:
    """One app's background loops: none until ``start``, and none at all in a
    process that does not hold the lock."""

    def __init__(self, db_path: str | Path, loops: dict[str, Loop | None]) -> None:
        # ``loops`` maps the name the log uses to the loop, or to ``None``
        # where its setting turns it off.
        self._db_path = db_path
        self._loops = loops
        self._tasks: tuple[asyncio.Task[None], ...] = ()
        self._lock: int | None = None
        self.state = STANDBY if any(loops.values()) else DISABLED

    async def start(self) -> None:
        """Take the lock and start each enabled loop. Called once per app: by
        its startup, or by the server that binds its listener."""
        enabled = [loop for loop in self._loops.values() if loop is not None]
        if enabled:
            self._lock, why_not = _take(lock_path(self._db_path))
            if self._lock is None:
                _logger.warning(
                    "background loops: not started, %s; this process serves "
                    "requests only",
                    why_not,
                )
                return
            self._tasks = tuple(
                asyncio.create_task(loop(self._db_path)) for loop in enabled
            )
            self.state = RUNNING
        _logger.info(
            "background loops: %s",
            ", ".join(
                f"{name} {'started' if loop is not None else 'disabled'}"
                for name, loop in self._loops.items()
            ),
        )

    async def stop(self) -> None:
        """Cancel the loops, wait for each to finish, then give up the lock.
        A loop that failed while stopping is raised after all have stopped."""
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._lock is not None:
            os.close(self._lock)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result
