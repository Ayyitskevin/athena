"""One clock, replaceable in tests (G6).

WHY: the app read the time in three places (``core/_util.utc_now``,
``packet_time.clock`` and bare ``datetime.now`` calls) and SQLite read it in a
fourth (``datetime('now')``), so a test could lapse a lease only by rewriting
the lease's stored expiry: it tested a row nobody wrote, never time passing.
``athena.core.clock`` is now the one place the lease lifecycle reads the time.
These tests hold that:

* in an app it is the system clock, and nothing in ``src/athena`` (no setting,
  route or tool) can replace it;
* a test that replaces it gets it back to the system clock when it ends, and a
  test that leaves it replaced fails instead of handing it to the next test;
* moving it lapses a lease with no write to the lease row, a claim and a
  renewal take their expiry from it, and the lease SQL binds it rather than
  SQLite's own clock;
* the lapse observer records ``lease_lapsed`` exactly as it did when a test
  rewrote the expiry instead.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    bearer,
    bootstrap,
    events,
    lapse_lease,
    make_app,
    onboard_agent,
    restore_system_clock,
)

from athena.aegis import leases
from athena.core import _util, db, packet_time
from athena.core import clock as app_clock

# Paths from the modules themselves, so no constant names a directory.
CLOCK_FILE = Path(app_clock.__file__).resolve()
SRC = CLOCK_FILE.parents[1]  # the athena package
ROOT = SRC.parents[1]
TESTS = Path(__file__).resolve().parent
STORED = "%Y-%m-%d %H:%M:%S"


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime(STORED)


# --- the production clock -------------------------------------------------------


def test_the_app_clock_is_the_system_clock():
    assert app_clock.replaced() is False
    before = datetime.now(UTC)
    readings = [app_clock.now(), _util.utc_now(), packet_time.clock()]
    after = datetime.now(UTC)
    for reading in readings:
        assert reading.utcoffset() == timedelta(0), reading
        assert before <= reading <= after


def test_an_app_connection_keeps_sqlites_own_clock(tmp_path, monkeypatch):
    """With the system clock nothing is installed on a connection: SQLite's
    ``datetime()`` is SQLite's, and production SQL runs as it always did."""

    def bridged(*args):
        raise AssertionError("the bridge was installed on an app connection")

    monkeypatch.setattr(app_clock, "sqlite_datetime", bridged)
    conn = db.connect(tmp_path / "plain.db")
    try:
        before = _stamp(datetime.now(UTC))
        (now,) = conn.execute("SELECT datetime('now')").fetchone()
        after = _stamp(datetime.now(UTC))
    finally:
        conn.close()
    assert before <= now <= after


def _assigned_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def _clock_aliases(tree: ast.AST) -> set[str]:
    """The names a module binds to ``athena.core.clock``."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "athena.core":
            aliases |= {a.asname or a.name for a in node.names if a.name == "clock"}
        elif isinstance(node, ast.Import):
            aliases |= {
                a.asname
                for a in node.names
                if a.name == "athena.core.clock" and a.asname
            }
    return aliases


def test_nothing_in_the_app_can_replace_the_clock():
    """Replacing the clock is for tests only: no module of the app (and so no
    setting, route or tool) reaches the seam, and the clock module never
    assigns it after defining it."""
    seam = {"_source", "_system"}
    reaching = []
    for path in sorted(SRC.rglob("*.py")):
        if path == CLOCK_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _clock_aliases(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "athena.core.clock"
                and {a.name for a in node.names} & seam
            ):
                reaching.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in seam
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                reaching.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"setattr", "getattr"}
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in aliases
            ):
                reaching.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert reaching == []
    clock_module = ast.parse(CLOCK_FILE.read_text(encoding="utf-8"))
    module_level = [
        node
        for node in clock_module.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and "_source" in _assigned_names(node)
    ]
    assert len(module_level) == 1, "the seam is defined once, at module level"
    for function in ast.walk(clock_module):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert "_source" not in _assigned_names(function), function.name


# --- the test clock ---------------------------------------------------------------


def test_the_clock_fixture_moves_only_by_hand(clock):
    start = clock.now()
    assert app_clock.replaced() is True
    assert start.microsecond == 0
    assert abs(start - datetime.now(UTC)) < timedelta(seconds=5)
    assert [app_clock.now(), _util.utc_now(), packet_time.clock()] == [start] * 3
    clock.advance(minutes=5)
    assert app_clock.now() == _util.utc_now() == start + timedelta(minutes=5)
    with pytest.raises(ValueError):
        clock.advance(seconds=-1)
    assert app_clock.now() == start + timedelta(minutes=5)


def test_a_replaced_clock_is_sqlites_datetime_on_a_new_connection(clock, tmp_path):
    clock.advance(days=400, seconds=0.25)
    moved = clock.now()
    conn = db.connect(tmp_path / "bridged.db")
    try:
        conn.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, at TEXT DEFAULT (datetime('now')))"
        )
        conn.execute("INSERT INTO t DEFAULT VALUES")
        row = conn.execute(
            "SELECT datetime('now'), datetime('NOW'), datetime(), "
            "datetime('now', '-1 minute'), "
            "datetime('2020-01-01 10:00:00', '+1 day'), "
            "(SELECT at FROM t)"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == (
        _stamp(moved),
        _stamp(moved),
        _stamp(moved),
        _stamp(moved - timedelta(minutes=1)),
        "2020-01-02 10:00:00",
        _stamp(moved),
    )


def test_a_test_that_leaves_the_clock_replaced_fails_and_the_next_one_is_clean(
    tmp_path,
):
    """Run in a separate pytest with this suite's conftest: a test that uses the
    fixture and moves the clock hands the next test the system clock; a test
    that replaces the clock by hand and leaves it fails, and still hands the
    next test the system clock."""
    (tmp_path / "test_leak.py").write_text(
        textwrap.dedent(
            """
            from datetime import UTC, datetime, timedelta

            from athena.core import clock as app_clock


            def test_1_moves_the_clock_through_the_fixture(clock):
                clock.advance(days=30)
                assert app_clock.now() > datetime.now(UTC) + timedelta(days=29)


            def test_2_starts_on_the_system_clock():
                assert app_clock.replaced() is False


            def test_3_leaves_a_frozen_clock_behind():
                app_clock._source = lambda: datetime(2040, 1, 1, tzinfo=UTC)


            def test_4_starts_on_the_system_clock():
                assert app_clock.replaced() is False
                assert abs(app_clock.now() - datetime.now(UTC)) < timedelta(seconds=5)
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(SRC.parent), str(TESTS)]),
    }
    ran = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "conftest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-rA",
            str(tmp_path / "test_leak.py"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = ran.stdout
    assert ran.returncode == 1, out + ran.stderr
    assert "PASSED test_leak.py::test_1_moves_the_clock_through_the_fixture" in out
    # The fixture put the system clock back itself: the guard had nothing to do.
    assert "ERROR test_leak.py::test_1_moves_the_clock_through_the_fixture" not in out
    assert "PASSED test_leak.py::test_2_starts_on_the_system_clock" in out
    assert "ERROR test_leak.py::test_3_leaves_a_frozen_clock_behind" in out
    assert "left the app's clock replaced" in out
    assert "PASSED test_leak.py::test_4_starts_on_the_system_clock" in out


def test_restoring_the_system_clock_says_whether_it_had_to():
    try:
        app_clock._source = lambda: datetime(2040, 1, 1, tzinfo=UTC)
        assert app_clock.replaced() is True
        assert restore_system_clock() is True
        assert app_clock.replaced() is False
    finally:
        restore_system_clock()
    assert restore_system_clock() is False


# --- the lease lifecycle reads it --------------------------------------------------


def _delegated_claim(client, seat, **body) -> tuple[int, dict]:
    """The admin (user 1) files an issue, assigns it to itself and delegates it
    to ``seat``, which claims it. Returns the issue id and the claim answer."""
    issue = client.post("/issues", json={"title": "Abandoned work"}, headers=ADMIN)
    assert issue.status_code == 201, issue.text
    issue_id = issue.json()["id"]
    assigned = client.put(
        f"/issues/{issue_id}/assignee", json={"assignee_id": 1}, headers=ADMIN
    )
    assert assigned.status_code == 200, assigned.text
    delegated = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": seat["user"]["id"]},
        headers=ADMIN,
    )
    assert delegated.status_code == 201, delegated.text
    tag = client.get(f"/issues/{issue_id}", headers=bearer(seat)).headers["ETag"]
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        json=body or None,
        headers={**bearer(seat), "If-Match": tag},
    )
    assert claimed.status_code == 201, claimed.text
    return issue_id, claimed.json()


def _lease_row(db_file, issue_id) -> tuple:
    conn = db.connect(db_file)
    try:
        return tuple(
            conn.execute(
                "SELECT * FROM issue_leases WHERE issue_id = ?", (issue_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def test_moving_the_clock_lapses_a_lease_and_nothing_writes_the_row(
    clock, tmp_path, monkeypatch
):
    app, db_file = make_app(tmp_path)
    statements: list[str] = []
    original = db.connect

    def traced(path):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    with TestClient(app) as client:
        bootstrap(client)
        seat = onboard_agent(client)
        clock.advance(days=1)  # far from SQLite's own clock
        claimed_at = clock.now()
        issue_id, lease = _delegated_claim(client, seat, lease_seconds=60)
        assert lease["active"] is True
        assert lease["expires_at"] == packet_time.rfc3339(
            _stamp(claimed_at + timedelta(seconds=60))
        )
        before = _lease_row(db_file, issue_id)

        monkeypatch.setattr(db, "connect", traced)
        clock.advance(seconds=59)
        live = client.get(f"/issues/{issue_id}/lease", headers=ADMIN).json()
        lapse_lease(clock, db_file, issue_id)
        lapsed = client.get(f"/issues/{issue_id}/lease", headers=ADMIN).json()
        desk = client.get("/desk", headers=bearer(seat)).json()
        monkeypatch.setattr(db, "connect", original)

    assert live["active"] is True and live["expires_in_seconds"] == 1
    assert lapsed["active"] is False and lapsed["expires_in_seconds"] == 0
    assert [row["issue_id"] for row in desk["work"]["leases"]["lapsed"]] == [issue_id]
    assert desk["work"]["leases"]["items"] == []
    assert _lease_row(db_file, issue_id) == before
    writes = [
        sql
        for sql in statements
        if "issue_leases" in sql
        and sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE", "REPLACE"))
    ]
    assert writes == []


def test_a_claim_and_a_renewal_take_their_window_from_the_app_clock(clock, tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat = onboard_agent(client)
        clock.advance(days=2)
        first_at = clock.now()
        issue_id, first = _delegated_claim(client, seat)
        clock.advance(minutes=10)
        renewed_at = clock.now()
        tag = client.get(f"/issues/{issue_id}", headers=bearer(seat)).headers["ETag"]
        renewed = client.post(
            f"/issues/{issue_id}/claim",
            json={"generation": first["generation"]},
            headers={**bearer(seat), "If-Match": tag},
        )
        assert renewed.status_code in (200, 201), renewed.text
    window = timedelta(seconds=leases.DEFAULT_LEASE_SECONDS)
    assert (first["claimed_at"], first["expires_at"]) == (
        packet_time.rfc3339(_stamp(first_at)),
        packet_time.rfc3339(_stamp(first_at + window)),
    )
    renewal = renewed.json()
    assert renewal["generation"] == first["generation"]
    assert (renewal["claimed_at"], renewal["expires_at"]) == (
        packet_time.rfc3339(_stamp(renewed_at)),
        packet_time.rfc3339(_stamp(renewed_at + window)),
    )


def test_the_lease_sql_binds_the_app_clock_not_sqlites(clock, tmp_path):
    """On a connection that keeps SQLite's own clock, the lease reads and writes
    still answer by the app's: every comparison and stamp is bound from it."""
    db_file = tmp_path / "unbridged.db"
    setup = db.connect(db_file)
    try:
        db.migrate(setup)
        setup.execute(
            "INSERT INTO users (id, email, name) VALUES (1, 'h@e.com', 'Holder')"
        )
        setup.execute("INSERT INTO issues (id, title, created_by) VALUES (1, 'w', 1)")
        setup.commit()
    finally:
        setup.close()
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        clock.advance(days=3)
        granted_at = clock.now()
        lease = leases.upsert_lease(conn, 1, 1, 60, commit=True)
        assert (lease["claimed_at"], lease["expires_at"]) == (
            _stamp(granted_at),
            _stamp(granted_at + timedelta(seconds=60)),
        )
        assert lease["active"] is True
        clock.advance(seconds=60)
        assert leases.get_lease(conn, 1)["active"] is False
        assert leases.list_active_leases(conn) == []
        assert leases.leases_held_by(conn, holder_id=1)[0]["active"] is False
        assert leases.count_active_leases_held_by(conn, holder_id=1) == 0
    finally:
        conn.close()


# --- the lapse observer --------------------------------------------------------------


def _observed_lapse(client, db_file, email, lapse) -> tuple[dict, dict, list[int]]:
    """A seat claims, ``lapse(issue_id)`` lapses it, the admin reads its feed
    once. Returns the claim, the one ``lease_lapsed`` event and who was told."""
    seat = onboard_agent(client, email=email, name=email.split("@")[0])
    issue_id, lease = _delegated_claim(client, seat)
    lapse(issue_id)
    feed = client.get("/events", params={"after": 0, "limit": 200}, headers=ADMIN)
    assert feed.status_code == 200, feed.text
    [event] = [
        row
        for row in feed.json()["events"]
        if row["verb"] == "lease_lapsed" and row["target_id"] == issue_id
    ]
    conn = db.connect(db_file)
    try:
        notified = [
            row["user_id"]
            for row in conn.execute(
                "SELECT user_id FROM notifications WHERE event_id = ? ORDER BY user_id",
                (event["id"],),
            )
        ]
    finally:
        conn.close()
    return lease, event, notified


def test_the_lapse_observer_records_lease_lapsed_exactly_as_before(clock, tmp_path):
    """The same lapse, observed twice: once with its stored expiry rewritten,
    as the tests used to lapse a lease, and once by the clock passing it. The
    event and its notifications are the same but for the lease's own values."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)

        def rewrite(issue_id: int) -> None:
            conn = db.connect(db_file)
            try:
                conn.execute(
                    "UPDATE issue_leases SET expires_at = '2000-01-01 00:00:00' "
                    "WHERE issue_id = ?",
                    (issue_id,),
                )
                conn.commit()
            finally:
                conn.close()

        old_lease, old, old_notified = _observed_lapse(
            client, db_file, "ada@e.com", rewrite
        )
        new_lease, new, new_notified = _observed_lapse(
            client,
            db_file,
            "bea@e.com",
            lambda issue_id: lapse_lease(clock, db_file, issue_id),
        )
        recorded = events(db_file, "lease_lapsed")
        holder = new_lease["holder_id"]

    assert new["detail"] == (
        f"generation {new_lease['generation']}; holder_id {holder}; "
        f"expired_at {new_lease['expires_at'].replace('T', ' ').removesuffix('Z')}; "
        f"holder {new_lease['holder_name']}"
    )
    assert old["detail"] == (
        f"generation {old_lease['generation']}; holder_id {old_lease['holder_id']}; "
        f"expired_at 2000-01-01 00:00:00; holder {old_lease['holder_name']}"
    )
    varying = {"id", "target_id", "detail", "created_at", "generation", "chain_hash"}
    assert {k: v for k, v in new.items() if k not in varying} == {
        k: v for k, v in old.items() if k not in varying
    }
    assert new["actor_id"] == 1 and new["imported_at"] is None
    # The accountable admin and the holder, who watches the work it was handed.
    assert old_notified == [1, old_lease["holder_id"]]
    assert new_notified == [1, holder]
    assert [row["id"] for row in recorded] == [new["id"], old["id"]]


def test_the_readers_that_take_now_default_to_the_app_clock(clock, tmp_path):
    """Each read that takes ``now`` and decides by a lease or a time window
    reads the app's clock when it is not given one. The clock is first moved a
    day past SQLite's and the system's, so a reader on either would disagree."""
    from athena.aegis import fence_overlaps, office, review_queue, run_state
    from athena.core import users

    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        project = client.post(
            "/projects", json={"name": "Work", "key": "WRK"}, headers=ADMIN
        )
        assert project.status_code == 201, project.text
        seat = onboard_agent(client)
        clock.advance(days=1)
        issue_id, lease = _delegated_claim(
            client, seat, lease_seconds=60, paths=["src/a.py"]
        )
        commented = client.post(
            f"/issues/{issue_id}/comments",
            json={"body": "worked"},
            headers=bearer(seat, run="run-1"),
        )
        assert commented.status_code == 201, commented.text
    seat_id = seat["user"]["id"]
    conn = db.connect(db_file)
    try:
        holder = users.get_user(conn, seat_id)
        admin = users.get_user(conn, 1)
        # A second lease whose fence overlaps the seat's, expiring with it: only
        # a write around the claim command can make an overlap.
        other = conn.execute(
            "INSERT INTO issues (title, created_by) VALUES ('other', 1)"
        ).lastrowid
        conn.execute(
            "INSERT INTO issue_leases (issue_id, holder_id, claimed_at, expires_at, "
            "generation, declared_paths) VALUES (?, 1, ?, ?, ?, '[\"src\"]')",
            (
                other,
                _stamp(clock.now()),
                packet_time.stored(lease["expires_at"]),
                "f" * 32,
            ),
        )
        conn.commit()

        live = {
            "office": office.build_office(conn, actor=holder)["active_lease_count"],
            "run": run_state.state_for_run(conn, agent_id=seat_id, run_id="run-1")[
                "state"
            ],
            "overlaps": fence_overlaps.find_overlaps(conn, actor=admin)["total"],
            "observed": review_queue.build_project_review_queue(
                conn, project_id=project.json()["id"], actor=admin
            )["observed_at"],
        }
        clock.advance(minutes=31)
        later = {
            "office": office.build_office(conn, actor=holder)["active_lease_count"],
            "run": run_state.state_for_run(conn, agent_id=seat_id, run_id="run-1")[
                "state"
            ],
            "overlaps": fence_overlaps.find_overlaps(conn, actor=admin)["total"],
            "observed": review_queue.build_project_review_queue(
                conn, project_id=project.json()["id"], actor=admin
            )["observed_at"],
        }
    finally:
        conn.close()
    assert live["office"] == 1 and later["office"] == 0
    assert live["run"] == "active" and later["run"] != "active"
    assert live["overlaps"] == 1 and later["overlaps"] == 0
    assert later["observed"] == clock.now().isoformat()


def test_the_lapse_command_reads_the_app_clock(clock, tmp_path):
    """The command re-checks under its writer lock with the app's clock: a lease
    a second before its expiry is no lapse, and the same lease at its expiry is
    one."""
    from athena.aegis import lease_lapse_commands
    from athena.core import users

    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat = onboard_agent(client)
        issue_id, lease = _delegated_claim(client, seat, lease_seconds=60)
    conn = db.connect(db_file)
    try:
        admin = users.get_user(conn, 1)
        clock.advance(seconds=59)
        before = lease_lapse_commands.record_lease_lapse(
            conn, actor=admin, issue_id=issue_id, generation=lease["generation"]
        )
        clock.advance(seconds=1)  # the expiry itself: a lease is live only before it
        at_expiry = lease_lapse_commands.record_lease_lapse(
            conn, actor=admin, issue_id=issue_id, generation=lease["generation"]
        )
    finally:
        conn.close()
    assert before is None
    assert at_expiry is not None and at_expiry["verb"] == "lease_lapsed"
