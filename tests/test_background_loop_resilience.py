"""Direct resilience and cancellation coverage for Athena's background loops."""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest

from athena import config


LOOPS = [
    ("athena.core.webhooks", "delivery_loop", "run_delivery_pass"),
    ("athena.aegis.automation", "process_loop", "run_pass"),
]


def _drive_loop(
    *, module, loop_name, pass_name, pass_impl, monkeypatch, db_path, passes
) -> int:
    calls = 0
    signal: dict = {}

    def record(_db_path):
        nonlocal calls
        calls += 1
        if calls >= passes:
            signal["loop"].call_soon_threadsafe(signal["reached"].set)
        return pass_impl()

    monkeypatch.setattr(module, pass_name, record)

    async def drive() -> None:
        signal["loop"] = asyncio.get_running_loop()
        signal["reached"] = asyncio.Event()
        task = asyncio.create_task(getattr(module, loop_name)(db_path))
        try:
            await asyncio.wait_for(signal["reached"].wait(), timeout=30)
        except TimeoutError:
            task.cancel()
            raise AssertionError(
                f"{loop_name} ran {calls} passes in 30s; expected {passes}"
            ) from None

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    return calls


@pytest.fixture
def brief_background_intervals(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_DELIVERY_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(config, "AUTOMATION_INTERVAL_SECONDS", 0.001)


@pytest.mark.parametrize("module_name,loop_name,pass_name", LOOPS)
def test_background_loop_runs_repeatedly_and_propagates_cancellation(
    tmp_path,
    monkeypatch,
    brief_background_intervals,
    module_name,
    loop_name,
    pass_name,
):
    calls = _drive_loop(
        module=importlib.import_module(module_name),
        loop_name=loop_name,
        pass_name=pass_name,
        pass_impl=lambda: None,
        monkeypatch=monkeypatch,
        db_path=tmp_path / "loop.db",
        passes=2,
    )
    assert calls >= 2


@pytest.mark.parametrize("module_name,loop_name,pass_name", LOOPS)
def test_failed_pass_is_logged_and_does_not_stop_background_loop(
    tmp_path,
    monkeypatch,
    brief_background_intervals,
    caplog,
    module_name,
    loop_name,
    pass_name,
):
    def fail_pass():
        raise RuntimeError("one bad pass")

    with caplog.at_level(logging.WARNING):
        calls = _drive_loop(
            module=importlib.import_module(module_name),
            loop_name=loop_name,
            pass_name=pass_name,
            pass_impl=fail_pass,
            monkeypatch=monkeypatch,
            db_path=tmp_path / "loop.db",
            passes=2,
        )

    assert calls >= 2
    assert "loop continues" in caplog.text
