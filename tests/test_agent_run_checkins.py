"""Cooperative agent-run check-in command and persistence regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import sqlite3
import threading

import pytest

from athena import config
from athena.core import (
    agent_run_checkins,
    agent_run_commands,
    db,
    run_context,
    tokens,
    users,
)


def _open(db_path):
    conn = db.connect(db_path)
    db.migrate(conn)
    return conn


def _actor(
    conn,
    *,
    email="agent@example.com",
    name="Agent",
    is_agent=True,
    scopes=(tokens.ISSUE_WRITE_SCOPE,),
):
    user = users.create_user(
        conn,
        email=email,
        name=name,
        is_agent=is_agent,
    )
    created = tokens.create_token(
        conn,
        user_id=user["id"],
        name=f"{name} token",
        scopes=scopes,
    )
    actor = tokens.resolve_token(conn, created["token"])
    assert actor is not None
    return user, created, actor


def _assert_command_error(kind, callback):
    with pytest.raises(agent_run_commands.AgentRunCommandError) as caught:
        callback()
    assert caught.value.kind == kind


def test_migration_has_composite_identity_and_enforces_references(tmp_path):
    conn = _open(tmp_path / "schema.db")
    try:
        columns = conn.execute("PRAGMA table_info(agent_run_checkins)").fetchall()
        primary_key = {
            row["name"]: row["pk"] for row in columns if row["pk"]
        }
        assert primary_key == {"agent_id": 1, "run_id": 2}
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_agent_run_checkins_recent'"
        ).fetchone()

        agent, token, _ = _actor(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_run_checkins "
                "(agent_id, run_id, last_token_id) VALUES (?, ?, ?)",
                (agent["id"], "   ", token["id"]),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_run_checkins "
                "(agent_id, run_id, last_token_id) VALUES (?, ?, ?)",
                (agent["id"], "x" * 201, token["id"]),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_run_checkins "
                "(agent_id, run_id, last_token_id) VALUES (?, ?, ?)",
                (agent["id"], "run", token["id"] + 1000),
            )
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        42,
        "",
        "   ",
        "x" * 201,
        "line\nbreak",
        "run\n",
        "\trun",
        "nul\0byte",
        "del\x7fbyte",
        "lone-\ud800-surrogate",
        "bidi-\u202espoof",
        "zero-\u200bwidth",
        "line-\u2028separator",
    ],
)
def test_strict_run_id_rejects_malformed_values_without_changing_headers(raw):
    with pytest.raises(ValueError):
        run_context.strict_run_id(raw)

    # The older correlation-header boundary remains deliberately forgiving.
    assert run_context.normalize("x" * 201) == "x" * 200


def test_strict_run_id_accepts_trimmed_unicode():
    assert run_context.strict_run_id("  任務-🚀  ") == "任務-🚀"
    assert run_context.strict_run_id("  cafe\u0301  ") == "café"


def test_heartbeat_requires_agent_bearer_with_live_write_scope(tmp_path):
    conn = _open(tmp_path / "policy.db")
    try:
        agent, _, write_actor = _actor(conn)
        _, _, human_actor = _actor(
            conn,
            email="human@example.com",
            name="Human",
            is_agent=False,
        )
        _, _, read_actor = _actor(
            conn,
            email="reader@example.com",
            name="Reader",
            scopes=(tokens.READ_SCOPE,),
        )

        _assert_command_error(
            "unauthorized",
            lambda: agent_run_commands.heartbeat(conn, actor=None, run_id="run"),
        )
        _assert_command_error(
            "forbidden",
            lambda: agent_run_commands.heartbeat(
                conn, actor=human_actor, run_id="run"
            ),
        )
        _assert_command_error(
            "forbidden",
            lambda: agent_run_commands.heartbeat(
                conn, actor=read_actor, run_id="run"
            ),
        )
        header_actor = {**write_actor}
        header_actor.pop("_token_id")
        _assert_command_error(
            "forbidden",
            lambda: agent_run_commands.heartbeat(
                conn, actor=header_actor, run_id="run"
            ),
        )
        _assert_command_error(
            "invalid",
            lambda: agent_run_commands.heartbeat(
                conn, actor=write_actor, run_id="line\nbreak"
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM agent_run_checkins"
        ).fetchone()["n"] == 0

        for index, scope in enumerate(
            (tokens.ISSUE_WRITE_SCOPE, tokens.DOCS_WRITE_SCOPE, tokens.ADMIN_SCOPE)
        ):
            if index == 0:
                actor = write_actor
            else:
                _, _, actor = _actor(
                    conn,
                    email=f"writer-{index}@example.com",
                    name=f"Writer {index}",
                    scopes=(scope,),
                )
            result = agent_run_commands.heartbeat(
                conn, actor=actor, run_id=f"scope-{scope}"
            )
            assert result["agent_id"] == actor["id"]
        assert agent["id"] == write_actor["id"]
    finally:
        conn.close()


def test_heartbeat_rechecks_database_authority_inside_transaction(tmp_path):
    conn = _open(tmp_path / "authority.db")
    try:
        agent, token, actor = _actor(conn)

        conn.execute(
            "UPDATE api_tokens SET scopes = ? WHERE id = ?",
            (tokens.READ_SCOPE, token["id"]),
        )
        conn.commit()
        forged_scope_actor = {
            **actor,
            "_token_scopes": (tokens.ISSUE_WRITE_SCOPE,),
        }
        _assert_command_error(
            "forbidden",
            lambda: agent_run_commands.heartbeat(
                conn, actor=forged_scope_actor, run_id="forged-scope"
            ),
        )

        conn.execute(
            "UPDATE api_tokens SET scopes = ? WHERE id = ?",
            (tokens.ISSUE_WRITE_SCOPE, token["id"]),
        )
        conn.commit()
        assert tokens.revoke_token(
            conn, user_id=agent["id"], token_id=token["id"]
        )
        _assert_command_error(
            "forbidden",
            lambda: agent_run_commands.heartbeat(
                conn, actor=actor, run_id="revoked"
            ),
        )

        _, _, current_actor = _actor(
            conn,
            email="unmarked@example.com",
            name="Unmarked",
        )
        assert users.set_agent(conn, current_actor["id"], False)
        _assert_command_error(
            "forbidden",
            lambda: agent_run_commands.heartbeat(
                conn, actor=current_actor, run_id="unmarked"
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM agent_run_checkins"
        ).fetchone()["n"] == 0
    finally:
        conn.close()


def test_heartbeat_upsert_is_safe_and_keeps_first_seen(tmp_path):
    conn = _open(tmp_path / "upsert.db")
    try:
        agent, first_token, actor = _actor(conn)
        first = agent_run_commands.heartbeat(conn, actor=actor, run_id="  run-1  ")
        assert set(first) == {
            "agent_id",
            "run_id",
            "first_seen_at",
            "last_seen_at",
            "age_seconds",
            "reporting_state",
        }
        assert first["run_id"] == "run-1"
        assert first["reporting_state"] == agent_run_checkins.REPORTING_RECENTLY
        assert first["age_seconds"] >= 0

        conn.execute(
            "UPDATE agent_run_checkins SET first_seen_at = ?, last_seen_at = ? "
            "WHERE agent_id = ? AND run_id = ?",
            ("2000-01-01 00:00:00", "2000-01-01 00:00:00", agent["id"], "run-1"),
        )
        conn.commit()
        second_token = tokens.create_token(
            conn,
            user_id=agent["id"],
            name="rotated",
            scopes=(tokens.DOCS_WRITE_SCOPE,),
        )
        second_actor = tokens.resolve_token(conn, second_token["token"])
        assert second_actor is not None

        refreshed = agent_run_commands.heartbeat(
            conn, actor=second_actor, run_id="run-1"
        )
        assert refreshed["first_seen_at"] == "2000-01-01 00:00:00"
        assert refreshed["last_seen_at"] != "2000-01-01 00:00:00"
        raw = conn.execute(
            "SELECT COUNT(*) AS n, MIN(last_token_id) AS token_id "
            "FROM agent_run_checkins WHERE agent_id = ? AND run_id = ?",
            (agent["id"], "run-1"),
        ).fetchone()
        assert raw["n"] == 1
        assert raw["token_id"] == second_token["id"]
        assert first_token["id"] != second_token["id"]
        assert "last_token_id" not in refreshed
    finally:
        conn.close()


def test_checkin_capacity_blocks_only_new_ids(tmp_path, monkeypatch):
    conn = _open(tmp_path / "capacity.db")
    try:
        monkeypatch.setattr(config, "AGENT_RUN_MAX_CHECKINS_PER_AGENT", 2)
        agent, _, actor = _actor(conn)
        agent_run_commands.heartbeat(conn, actor=actor, run_id="run-1")
        agent_run_commands.heartbeat(conn, actor=actor, run_id="run-2")

        # WHY: hitting the durable-row ceiling must not stop a healthy worker from
        # refreshing a row it already owns, even when an operator lowers the cap
        # below the current row count.
        monkeypatch.setattr(config, "AGENT_RUN_MAX_CHECKINS_PER_AGENT", 1)
        refreshed = agent_run_commands.heartbeat(conn, actor=actor, run_id="run-1")
        assert refreshed["run_id"] == "run-1"
        _assert_command_error(
            "capacity",
            lambda: agent_run_commands.heartbeat(
                conn, actor=actor, run_id="run-3"
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM agent_run_checkins WHERE agent_id = ?",
            (agent["id"],),
        ).fetchone()["n"] == 2
    finally:
        conn.close()


def test_checkin_capacity_serializes_last_slot_across_tokens(tmp_path, monkeypatch):
    db_path = tmp_path / "capacity-race.db"
    setup = _open(db_path)
    try:
        monkeypatch.setattr(config, "AGENT_RUN_MAX_CHECKINS_PER_AGENT", 2)
        agent, _, actor_one = _actor(setup)
        agent_run_commands.heartbeat(setup, actor=actor_one, run_id="seed")
        token_two = tokens.create_token(
            setup,
            user_id=agent["id"],
            name="second worker",
            scopes=(tokens.ISSUE_WRITE_SCOPE,),
        )
        actor_two = tokens.resolve_token(setup, token_two["token"])
        assert actor_two is not None
    finally:
        setup.close()

    barrier = threading.Barrier(2)

    def compete(actor, run_id):
        conn = db.connect(db_path)
        try:
            barrier.wait(timeout=5)
            try:
                agent_run_commands.heartbeat(conn, actor=actor, run_id=run_id)
                return "accepted"
            except agent_run_commands.AgentRunCommandError as exc:
                return exc.kind
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(compete, actor_one, "candidate-one"),
            pool.submit(compete, actor_two, "candidate-two"),
        )
        outcomes = [future.result(timeout=10) for future in futures]

    check = db.connect(db_path)
    try:
        # WHY: the cap belongs to the agent, not a token, and BEGIN IMMEDIATE makes
        # the count-and-insert decision atomic across workers.
        assert sorted(outcomes) == ["accepted", "capacity"]
        assert check.execute(
            "SELECT COUNT(*) AS n FROM agent_run_checkins WHERE agent_id = ?",
            (agent["id"],),
        ).fetchone()["n"] == 2

        _, _, other_actor = _actor(
            check,
            email="other@example.com",
            name="Other Agent",
        )
        assert agent_run_commands.heartbeat(
            check, actor=other_actor, run_id="independent"
        )["run_id"] == "independent"
    finally:
        check.close()


def test_recent_projection_has_exact_90_second_boundary_and_safe_identity(
    tmp_path, monkeypatch
):
    conn = _open(tmp_path / "recent.db")
    try:
        monkeypatch.setattr(config, "AGENT_RUN_STALE_SECONDS", 90)
        agent, _, actor = _actor(
            conn,
            email="fresh@example.com",
            name="Fresh Agent",
        )
        for run_id in ("age-90", "age-91", "future"):
            agent_run_commands.heartbeat(conn, actor=actor, run_id=run_id)
        times = {
            "age-90": "2026-01-01 00:00:00",
            "age-91": "2025-12-31 23:59:59",
            "future": "2026-01-01 00:02:00",
        }
        for run_id, seen_at in times.items():
            conn.execute(
                "UPDATE agent_run_checkins SET first_seen_at = ?, last_seen_at = ? "
                "WHERE agent_id = ? AND run_id = ?",
                (seen_at, seen_at, agent["id"], run_id),
            )
        conn.commit()
        now = datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)

        age_90 = agent_run_checkins.get_checkin(
            conn, agent_id=agent["id"], run_id="age-90", now=now
        )
        age_91 = agent_run_checkins.get_checkin(
            conn, agent_id=agent["id"], run_id="age-91", now=now
        )
        future = agent_run_checkins.get_checkin(
            conn, agent_id=agent["id"], run_id="future", now=now
        )
        assert age_90["age_seconds"] == 90
        assert age_90["reporting_state"] == "reporting_recently"
        assert age_91["age_seconds"] == 91
        assert age_91["reporting_state"] == "stale"
        assert future["age_seconds"] == 0
        assert future["reporting_state"] == "reporting_recently"

        rows = agent_run_checkins.list_recent_checkins(
            conn, agent_id=agent["id"], limit=2, now=now
        )
        assert [row["run_id"] for row in rows] == ["future", "age-90"]
        assert all(row["agent_name"] == "Fresh Agent" for row in rows)
        assert all(row["agent_email"] == "fresh@example.com" for row in rows)
        assert all("last_token_id" not in row for row in rows)
        with pytest.raises(ValueError):
            agent_run_checkins.list_recent_checkins(conn, limit=0)
    finally:
        conn.close()


def test_concurrent_first_heartbeats_converge_without_activity(tmp_path):
    db_path = tmp_path / "concurrent.db"
    setup = _open(db_path)
    try:
        agent, _, actor = _actor(setup)
        activity_before = setup.execute(
            "SELECT COUNT(*) AS n FROM activity"
        ).fetchone()["n"]
    finally:
        setup.close()

    barrier = threading.Barrier(2)

    def report():
        conn = db.connect(db_path)
        try:
            barrier.wait(timeout=5)
            return agent_run_commands.heartbeat(
                conn, actor=actor, run_id="same-run"
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in (pool.submit(report), pool.submit(report))]

    check = db.connect(db_path)
    try:
        assert len(results) == 2
        assert {result["run_id"] for result in results} == {"same-run"}
        assert check.execute(
            "SELECT COUNT(*) AS n FROM agent_run_checkins "
            "WHERE agent_id = ? AND run_id = ?",
            (agent["id"], "same-run"),
        ).fetchone()["n"] == 1
        assert check.execute(
            "SELECT COUNT(*) AS n FROM activity"
        ).fetchone()["n"] == activity_before
    finally:
        check.close()
