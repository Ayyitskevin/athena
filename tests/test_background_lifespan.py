"""Real lifespan coverage for background-loop startup and shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from athena import config
from athena.aegis import automation
from athena.core import db, webhooks
from athena.main import create_app


@dataclass
class _LoopProbe:
    cleanup_error: str | None = None
    started: threading.Event = field(default_factory=threading.Event)
    cleaned: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[object] | None = None
    connection: sqlite3.Connection | None = None

    async def run(self, db_path: str | Path) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.task = task
        self.connection = db.connect(db_path)
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.connection.close()
            # A suspension point makes the assertion prove lifespan awaited the
            # cleanup coroutine, rather than merely requesting cancellation.
            await asyncio.sleep(0)
            self.cleaned.set()
            if self.cleanup_error is not None:
                raise RuntimeError(self.cleanup_error)

    def assert_fully_stopped(self) -> None:
        assert self.cleaned.is_set()
        assert self.task is not None
        assert self.task.done()
        assert self.connection is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            self.connection.execute("SELECT 1")


def _enable_probed_loops(
    monkeypatch: pytest.MonkeyPatch,
    webhook_probe: _LoopProbe,
    automation_probe: _LoopProbe,
) -> None:
    monkeypatch.setattr(config, "WEBHOOK_DELIVERY_ENABLED", True)
    monkeypatch.setattr(config, "AUTOMATION_ENABLED", True)
    monkeypatch.setattr(webhooks, "delivery_loop", webhook_probe.run)
    monkeypatch.setattr(automation, "process_loop", automation_probe.run)


def test_lifespan_starts_and_fully_awaits_enabled_background_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    webhook_probe = _LoopProbe()
    automation_probe = _LoopProbe()
    _enable_probed_loops(monkeypatch, webhook_probe, automation_probe)

    with TestClient(create_app(tmp_path / "athena.db")):
        assert webhook_probe.started.wait(timeout=2)
        assert automation_probe.started.wait(timeout=2)
        assert not webhook_probe.cleaned.is_set()
        assert not automation_probe.cleaned.is_set()

    webhook_probe.assert_fully_stopped()
    automation_probe.assert_fully_stopped()


def test_lifespan_finishes_every_cleanup_before_propagating_one_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    webhook_probe = _LoopProbe(cleanup_error="webhook cleanup failed")
    automation_probe = _LoopProbe()
    _enable_probed_loops(monkeypatch, webhook_probe, automation_probe)

    with pytest.raises(RuntimeError, match="webhook cleanup failed"):
        with TestClient(create_app(tmp_path / "athena.db")):
            assert webhook_probe.started.wait(timeout=2)
            assert automation_probe.started.wait(timeout=2)

    webhook_probe.assert_fully_stopped()
    automation_probe.assert_fully_stopped()
