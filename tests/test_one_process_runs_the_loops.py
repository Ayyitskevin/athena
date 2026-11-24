"""The background loops run in one process, and only once it is bound (H13).

WHY (OPS-5): a second ``athena-serve`` on a port already in use ran the
webhook and automation loops before it failed to bind, so 30 events gave 47
deliveries; and a second process on another port ran them for as long as it
lived, with no warning.

Now a server starts the loops itself after its listener is bound, and only
the process holding an exclusive lock on ``<database>.loops.lock`` runs
them. A process without the lock serves requests, logs one line saying so,
and ``/readyz`` reports which it is.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shlex
import socket
import stat
import threading

import pytest
from fastapi.testclient import TestClient
import uvicorn

from athena import background, config, ops
from athena.aegis import automation
from athena.core import db, webhooks
from athena.main import create_app


class _Probe:
    """A loop that records that it ran and waits to be cancelled."""

    def __init__(self) -> None:
        self.starts = 0
        self.started = threading.Event()
        self.stopped = threading.Event()

    async def run(self, db_path) -> None:
        self.starts += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


@pytest.fixture
def probes(monkeypatch):
    delivery, rules = _Probe(), _Probe()
    monkeypatch.setattr(config, "WEBHOOK_DELIVERY_ENABLED", True)
    monkeypatch.setattr(config, "AUTOMATION_ENABLED", True)
    monkeypatch.setattr(webhooks, "delivery_loop", delivery.run)
    monkeypatch.setattr(automation, "process_loop", rules.run)
    return delivery, rules


def _ran(*probes: _Probe) -> bool:
    return any(probe.started.wait(timeout=0.3) for probe in probes)


def _lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("background loops:")
    ]


# --- one process ------------------------------------------------------------------


def test_the_process_that_holds_the_lock_runs_the_loops_and_says_so(
    tmp_path, probes, caplog
):
    database = tmp_path / "athena.db"
    lock = Path(f"{database}.loops.lock")
    caplog.set_level(logging.INFO, logger="athena")

    with TestClient(create_app(database)) as client:
        assert all(probe.started.wait(timeout=2) for probe in probes)
        assert client.get("/readyz").json()["background_loops"] == "running"
        # The lock is a private, empty file beside the database.
        assert background.lock_path(database) == lock
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert lock.read_bytes() == b""

    assert all(probe.stopped.is_set() for probe in probes)
    assert _lines(caplog) == [
        "background loops: webhook delivery started, automation started"
    ]


def test_a_second_process_serves_without_the_loops_and_says_so_once(
    tmp_path, probes, monkeypatch, caplog
):
    database = tmp_path / "athena.db"
    caplog.set_level(logging.INFO, logger="athena")
    with TestClient(create_app(database)) as first:
        assert all(probe.started.wait(timeout=2) for probe in probes)
        second_delivery, second_rules = _Probe(), _Probe()
        monkeypatch.setattr(webhooks, "delivery_loop", second_delivery.run)
        monkeypatch.setattr(automation, "process_loop", second_rules.run)
        caplog.clear()

        with TestClient(create_app(database)) as second:
            # It serves: the database answers, and it reports what it is.
            ready = second.get("/readyz")
            assert ready.status_code == 200
            assert ready.json() == {
                "status": "ok",
                "database": "ok",
                "background_loops": "standby",
            }
            assert not _ran(second_delivery, second_rules)
            assert first.get("/readyz").json()["background_loops"] == "running"

        assert _lines(caplog) == [
            f"background loops: not started, another process holds "
            f"{database}.loops.lock; this process serves requests only"
        ]
        (said,) = [
            record
            for record in caplog.records
            if record.getMessage().startswith("background loops:")
        ]
        assert said.levelno == logging.WARNING
        # Stopping the second took nothing from the first.
        assert not any(probe.stopped.is_set() for probe in probes)
        assert first.get("/readyz").json()["background_loops"] == "running"


def test_the_lock_is_given_up_when_its_holder_stops(tmp_path, probes):
    database = tmp_path / "athena.db"
    with TestClient(create_app(database)) as first:
        assert first.get("/readyz").json()["background_loops"] == "running"

    with TestClient(create_app(database)) as later:
        assert later.get("/readyz").json()["background_loops"] == "running"
    assert [probe.starts for probe in probes] == [2, 2]


@pytest.mark.parametrize("in_the_way", ["directory", "symlink"])
def test_a_lock_that_cannot_be_opened_leaves_the_process_serving(
    tmp_path, probes, caplog, in_the_way
):
    database = tmp_path / "athena.db"
    lock = Path(f"{database}.loops.lock")
    elsewhere = tmp_path / "elsewhere"
    if in_the_way == "directory":
        lock.mkdir()
    else:
        # A link is not followed: the lock is the file beside the database.
        lock.symlink_to(elsewhere)

    with TestClient(create_app(database)) as client:
        assert client.get("/readyz").json()["background_loops"] == "standby"
        assert not _ran(*probes)

    (line,) = _lines(caplog)
    assert line.startswith(
        f"background loops: not started, {database}.loops.lock cannot be locked ("
    )
    assert "another process" not in line
    assert not elsewhere.exists()


def test_only_an_enabled_loop_runs_and_the_line_names_both(
    tmp_path, probes, monkeypatch, caplog
):
    delivery, rules = probes
    monkeypatch.setattr(config, "AUTOMATION_ENABLED", False)
    caplog.set_level(logging.INFO, logger="athena")

    with TestClient(create_app(tmp_path / "athena.db")) as client:
        assert delivery.started.wait(timeout=2)
        assert client.get("/readyz").json()["background_loops"] == "running"
        assert not _ran(rules)

    assert _lines(caplog) == [
        "background loops: webhook delivery started, automation disabled"
    ]


def test_with_both_loops_off_no_lock_is_taken(tmp_path, monkeypatch, caplog):
    # The suite's own setting: every other test's app is this one.
    assert not config.WEBHOOK_DELIVERY_ENABLED and not config.AUTOMATION_ENABLED
    database = tmp_path / "athena.db"
    caplog.set_level(logging.INFO, logger="athena")

    with TestClient(create_app(database)) as client:
        assert client.get("/readyz").json()["background_loops"] == "disabled"
        # A second such process is not a standby: there is nothing to stand by for.
        with TestClient(create_app(database)) as second:
            assert second.get("/readyz").json()["background_loops"] == "disabled"

    assert not Path(f"{database}.loops.lock").exists()
    assert (
        _lines(caplog)
        == ["background loops: webhook delivery disabled, automation disabled"] * 2
    )


# --- after the bind ---------------------------------------------------------------


def test_a_server_that_binds_starts_the_loops_itself(tmp_path, probes):
    app = create_app(tmp_path / "athena.db", loops_after_bind=True)

    with TestClient(app) as client:
        # Startup has run and nothing is running yet.
        assert not _ran(*probes)
        assert client.get("/readyz").json()["background_loops"] == "standby"
        assert not Path(f"{tmp_path / 'athena.db'}.loops.lock").exists()

        client.portal.call(app.state.background.start)
        assert all(probe.started.wait(timeout=2) for probe in probes)
        assert client.get("/readyz").json()["background_loops"] == "running"

    assert all(probe.stopped.is_set() for probe in probes)


def test_a_server_that_never_bound_stops_cleanly_having_run_nothing(tmp_path, probes):
    with TestClient(create_app(tmp_path / "athena.db", loops_after_bind=True)):
        pass

    assert not any(probe.started.is_set() for probe in probes)


def _serve(app, port: int) -> None:
    ops._serve_bound(app, host="127.0.0.1", port=port, lifespan="on")


def test_a_start_that_fails_on_the_port_runs_no_loop(tmp_path, probes, caplog):
    """The baseline's duplicate start: the port is taken."""
    database = tmp_path / "athena.db"
    caplog.set_level(logging.INFO, logger="athena")
    app = create_app(database, loops_after_bind=True)

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(SystemExit) as stopped:
            _serve(app, taken.getsockname()[1])

    # uvicorn's own exit status for a start that failed, as before.
    assert stopped.value.code == 3
    assert not any(probe.started.is_set() for probe in probes)
    assert not Path(f"{database}.loops.lock").exists()
    # Its startup did run (the schema is current) and its shutdown too.
    assert app.state.db_path == database
    assert _lines(caplog) == []


def test_a_start_that_binds_runs_the_loops(tmp_path, probes, monkeypatch):
    database = tmp_path / "athena.db"
    app = create_app(database, loops_after_bind=True)
    seen: list[tuple[bool, str]] = []

    async def serve_nothing(server) -> None:
        # In place of serving requests until a signal: what is true once bound.
        seen.append((server.started, app.state.background.state))

    monkeypatch.setattr(uvicorn.Server, "main_loop", serve_nothing)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]

    _serve(app, port)

    assert seen == [(True, "running")]
    assert [probe.starts for probe in probes] == [1, 1]
    assert all(probe.stopped.is_set() for probe in probes)


def test_ctrl_c_ends_a_started_server_without_an_error(tmp_path, monkeypatch):
    def run(server, sockets=None) -> None:
        server.started = True
        raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn.Server, "run", run)

    assert _serve(create_app(tmp_path / "athena.db"), 8000) is None


def test_a_server_that_stopped_before_it_started_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(uvicorn.Server, "run", lambda server, sockets=None: None)

    with pytest.raises(SystemExit) as stopped:
        _serve(create_app(tmp_path / "athena.db"), 8000)
    assert stopped.value.code == 3


# --- the doctor knows the file ----------------------------------------------------


def test_the_doctor_counts_the_lock_file_as_athenas_own(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    os.chmod(data, 0o755)
    database = data / "athena.db"
    conn = db.connect(database)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    os.chmod(database, 0o600)
    Path(f"{database}.loops.lock").touch(mode=0o600)

    assert ops.doctor_main([str(database)]) == 0

    # Nothing but Athena's entries: the directory gets its command.
    assert f"run `chmod 700 {shlex.quote(str(data))}`" in capsys.readouterr().err
