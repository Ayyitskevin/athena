"""Bounded, restart-safe UTC schedule triggers for the automation engine."""

from datetime import UTC, datetime, timedelta
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena.aegis import automation, automation_commands, issue_activity, issues
from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _database(tmp_path, *, issue_count: int = 1):
    db_file = tmp_path / "scheduled.db"
    with TestClient(create_app(db_file)) as client:
        client.post(
            "/users",
            json={"email": "admin@example.com", "name": "Admin", "password": "pw"},
        )
        issue_ids = [
            client.post("/issues", json={"title": f"Issue {index}"}, headers=H1).json()[
                "id"
            ]
            for index in range(issue_count)
        ]
    return db_file, issue_ids


def _schedule(
    conn,
    *,
    at: str,
    every: int | None = None,
    conditions: dict | None = None,
    body: str = "scheduled nudge",
):
    return automation.create_rule(
        conn,
        name="scheduled rule",
        trigger_verb=automation.SCHEDULE_TRIGGER_VERB,
        trigger_type="schedule",
        schedule_at=at,
        schedule_every_seconds=every,
        action_type="comment",
        action_params={"body": body},
        conditions=conditions,
        created_by=1,
    )


def _action_schedule(
    conn,
    *,
    at: str,
    action_type: str,
    action_params: dict,
):
    return automation.create_rule(
        conn,
        name=f"scheduled {action_type}",
        trigger_verb=automation.SCHEDULE_TRIGGER_VERB,
        trigger_type="schedule",
        schedule_at=at,
        action_type=action_type,
        action_params=action_params,
        created_by=1,
    )


def _create_contributor(db_file) -> int:
    with TestClient(create_app(db_file)) as client:
        response = client.post(
            "/users",
            json={
                "email": "contributor@example.com",
                "name": "Contributor",
                "password": "pw",
            },
            headers=H1,
        )
        assert response.status_code == 201
        return response.json()["id"]


def _now(value: str) -> datetime:
    parsed = automation._parse_utc_timestamp(value)
    assert parsed is not None
    return parsed


def test_schedule_validation_accepts_only_canonical_utc_and_bounded_intervals():
    valid = {
        "trigger_verb": "scheduled",
        "trigger_type": "schedule",
        "schedule_at": "2030-01-02T03:04:05Z",
        "schedule_every_seconds": 60,
        "action_type": "comment",
        "action_params": {"body": "nudge"},
        "conditions": {"inactive_for_seconds": 3600},
    }
    assert automation.validate_rule(**valid) is None
    for invalid in (
        "2030-01-02 03:04:05",
        "2030-01-02T03:04:05",
        "2030-01-02T03:04:05+00:00",
        "2030-01-02T03:04:05.123Z",
        "2030-13-02T03:04:05Z",
    ):
        assert "canonical UTC" in automation.validate_rule(
            **(valid | {"schedule_at": invalid})
        )
    assert "schedule_every_seconds" in automation.validate_rule(
        **(valid | {"schedule_every_seconds": 59})
    )
    assert "inactive_for_seconds" in automation.validate_rule(
        **(valid | {"conditions": {"inactive_for_seconds": True}})
    )
    for field, value in (
        ("project_id", []),
        ("assignee_id", True),
        ("sprint_id", "12"),
        ("status", 1),
        ("priority", {}),
    ):
        error = automation.validate_rule(**(valid | {"conditions": {field: value}}))
        assert error is not None and field in error

    assert "schedule fields" in automation.validate_rule(
        **(
            valid
            | {
                "trigger_type": "event",
                "trigger_verb": "created",
            }
        )
    )


def test_exact_boundary_claim_is_idempotent_and_authored_by_automation(tmp_path):
    db_file, issue_ids = _database(tmp_path)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-01-01T12:00:00Z")
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []

    def execute(_conn, _rule, event):
        calls.append(event["target_id"])

    exact = _now("2030-01-01T12:00:00Z")
    assert (
        automation.process_schedules(
            conn, executor=execute, actor_id=actor_id, now=exact - timedelta(seconds=1)
        )
        == 0
    )
    assert (
        automation.process_schedules(
            conn, executor=execute, actor_id=actor_id, now=exact
        )
        == 1
    )
    assert (
        automation.process_schedules(
            conn, executor=execute, actor_id=actor_id, now=exact
        )
        == 0
    )
    assert calls == issue_ids

    firing = conn.execute("SELECT * FROM automation_schedule_firings").fetchone()
    occurrence = conn.execute(
        "SELECT * FROM automation_schedule_occurrences"
    ).fetchone()
    trigger = activity.get_activity(conn, occurrence["trigger_event_id"])
    assert firing["state"] == "completed" and firing["scheduled_for"] == (
        "2030-01-01T12:00:00Z"
    )
    assert occurrence["state"] == "completed" and occurrence["attempt_count"] == 1
    assert trigger["actor_id"] == actor_id and trigger["verb"] == "scheduled"
    assert trigger["run_id"] == automation.schedule_trigger_run_id(
        rule["id"], firing["scheduled_for"], issue_ids[0]
    )
    assert trigger["parent_run_id"] is None
    assert trigger["forked_from_event_id"] is None
    assert automation.get_rule(conn, rule["id"])["schedule_status"] == "completed"


def test_crash_after_action_commit_resumes_without_duplicate_and_keeps_lineage(
    tmp_path,
):
    db_file, issue_ids = _database(tmp_path)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-02-01T00:00:00Z", body="only once")
    actor_id = automation.system_actor_id(conn)
    now = _now("2030-02-01T00:00:00Z")
    crashed = False

    def crash_after_commit(c, current_rule, event):
        nonlocal crashed
        automation.execute_action(c, current_rule, event, actor_id=actor_id)
        if not crashed:
            crashed = True
            raise RuntimeError("crash after action commit")

    assert (
        automation.process_schedules(
            conn, executor=crash_after_commit, actor_id=actor_id, now=now
        )
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
    pending = conn.execute("SELECT * FROM automation_schedule_occurrences").fetchone()
    assert pending["state"] == "pending" and pending["attempt_count"] == 1

    def resume(c, current_rule, event):
        automation.execute_action(c, current_rule, event, actor_id=actor_id)

    assert (
        automation.process_schedules(conn, executor=resume, actor_id=actor_id, now=now)
        == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1

    occurrence = conn.execute(
        "SELECT * FROM automation_schedule_occurrences"
    ).fetchone()
    trigger = activity.get_activity(conn, occurrence["trigger_event_id"])
    action_event = conn.execute(
        "SELECT * FROM activity WHERE run_id = ?",
        (automation.automation_run_id(rule["id"], trigger["id"]),),
    ).fetchone()
    assert occurrence["state"] == "completed" and occurrence["attempt_count"] == 2
    assert action_event["actor_id"] == actor_id
    assert action_event["parent_run_id"] == trigger["run_id"]
    assert action_event["forked_from_event_id"] == trigger["id"]
    assert action_event["target_id"] == issue_ids[0]


def test_recurring_downtime_collapses_to_latest_grid_slot(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-03-01T10:00:00Z", every=3600)
    actor_id = automation.system_actor_id(conn)
    calls: list[str] = []
    now = _now("2030-03-01T15:30:00Z")
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, _r, event: calls.append(event["run_id"]),
            actor_id=actor_id,
            now=now,
        )
        == 1
    )
    assert (
        automation.process_schedules(
            conn, executor=lambda *_: None, actor_id=actor_id, now=now
        )
        == 0
    )
    current = automation.get_rule(conn, rule["id"])
    firing = conn.execute("SELECT * FROM automation_schedule_firings").fetchone()
    assert firing["scheduled_for"] == "2030-03-01T15:00:00Z"
    assert firing["missed_slots"] == 5
    assert current["schedule_missed_count"] == 5
    assert current["next_scheduled_at"] == "2030-03-01T16:00:00Z"
    assert len(calls) == 1


def test_disabled_rule_waits_and_reenable_resumes_bounded_work(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-04-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    automation.set_enabled(conn, rule["id"], False)
    now = _now("2030-04-01T00:01:00Z")
    assert (
        automation.process_schedules(
            conn, executor=lambda *_: None, actor_id=actor_id, now=now
        )
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM automation_schedule_firings").fetchone()[0]
        == 0
    )
    assert automation.get_rule(conn, rule["id"])["schedule_status"] == "disabled"

    automation.set_enabled(conn, rule["id"], True)
    assert (
        automation.process_schedules(
            conn, executor=lambda *_: None, actor_id=actor_id, now=now, max_actions=0
        )
        == 0
    )
    assert (
        conn.execute("SELECT state FROM automation_schedule_occurrences").fetchone()[
            "state"
        ]
        == "pending"
    )

    automation.set_enabled(conn, rule["id"], False)
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: (_ for _ in ()).throw(
                AssertionError("paused work ran")
            ),
            actor_id=actor_id,
            now=now,
        )
        == 0
    )
    automation.set_enabled(conn, rule["id"], True)
    assert (
        automation.process_schedules(
            conn, executor=lambda *_: None, actor_id=actor_id, now=now
        )
        == 1
    )


def test_expired_one_shot_is_visible_and_fails_closed(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2020-01-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: (_ for _ in ()).throw(AssertionError("must not run")),
            actor_id=actor_id,
            now=_now("2020-01-02T00:00:01Z"),
        )
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM automation_schedule_firings").fetchone()[0]
        == 0
    )
    assert automation.get_rule(conn, rule["id"])["schedule_status"] == "overdue"


def test_target_overflow_fails_whole_slot_closed_and_is_visible(tmp_path):
    db_file, _ = _database(tmp_path, issue_count=3)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-05-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: (_ for _ in ()).throw(AssertionError("partial sweep")),
            actor_id=actor_id,
            now=_now("2030-05-01T00:00:00Z"),
            max_targets=2,
        )
        == 0
    )
    firing = conn.execute("SELECT * FROM automation_schedule_firings").fetchone()
    current = automation.get_rule(conn, rule["id"])
    assert firing["state"] == "failed" and firing["trigger_count"] == 0
    assert firing["overflow_count"] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM automation_schedule_occurrences").fetchone()[
            0
        ]
        == 0
    )
    assert current["last_schedule_target_count"] == 3
    assert current["last_schedule_overflow_count"] == 1
    assert current["failure_count"] == 1 and current["schedule_status"] == "failing"


def test_action_budget_resumes_targets_in_stable_issue_order(tmp_path):
    db_file, issue_ids = _database(tmp_path, issue_count=3)
    conn = db.connect(db_file)
    _schedule(conn, at="2030-06-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []
    now = _now("2030-06-01T00:00:00Z")
    for expected_count in (1, 2, 3):
        assert (
            automation.process_schedules(
                conn,
                executor=lambda _c, _r, event: calls.append(event["target_id"]),
                actor_id=actor_id,
                now=now,
                max_actions=1,
            )
            == 1
        )
        assert calls == issue_ids[:expected_count]
    assert (
        automation.process_schedules(
            conn, executor=lambda *_: None, actor_id=actor_id, now=now, max_actions=1
        )
        == 0
    )
    states = conn.execute(
        "SELECT state, attempt_count FROM automation_schedule_occurrences ORDER BY issue_id"
    ).fetchall()
    assert [(row["state"], row["attempt_count"]) for row in states] == [
        ("completed", 1),
        ("completed", 1),
        ("completed", 1),
    ]


def test_failing_target_retries_three_times_then_unblocks_firing(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-07-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    now = _now("2030-07-01T00:00:00Z")

    def boom(*_args):
        raise ValueError("broken action")

    for _ in range(3):
        assert (
            automation.process_schedules(
                conn, executor=boom, actor_id=actor_id, now=now
            )
            == 0
        )
    occurrence = conn.execute(
        "SELECT * FROM automation_schedule_occurrences"
    ).fetchone()
    firing = conn.execute("SELECT * FROM automation_schedule_firings").fetchone()
    assert occurrence["state"] == "failed" and occurrence["attempt_count"] == 3
    assert firing["state"] == "failed"
    assert automation.get_rule(conn, rule["id"])["failure_count"] == 3
    assert (
        automation.process_schedules(conn, executor=boom, actor_id=actor_id, now=now)
        == 0
    )
    assert (
        conn.execute(
            "SELECT attempt_count FROM automation_schedule_occurrences"
        ).fetchone()[0]
        == 3
    )


def test_malformed_schedule_is_visible_and_does_not_block_healthy_rule(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    malformed = _schedule(conn, at="2030-08-01T00:00:00Z")
    healthy = _schedule(conn, at="2030-08-01T00:00:00Z")
    conn.execute(
        "UPDATE automation_rules SET schedule_at = 'not-a-time' WHERE id = ?",
        (malformed["id"],),
    )
    conn.commit()
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, rule, _e: calls.append(rule["id"]),
            actor_id=actor_id,
            now=_now("2030-08-01T00:00:00Z"),
        )
        == 1
    )
    assert calls == [healthy["id"]]
    broken = automation.get_rule(conn, malformed["id"])
    assert broken["schedule_status"] == "malformed"
    assert "canonical UTC" in broken["configuration_error"]


def test_inactivity_condition_uses_slot_time_and_durable_snapshot(tmp_path):
    db_file, issue_ids = _database(tmp_path, issue_count=2)
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE activity SET created_at = '2029-12-31 22:00:00' "
        "WHERE target_kind = 'issue' AND target_id = ?",
        (issue_ids[0],),
    )
    conn.execute(
        "UPDATE activity SET created_at = '2030-01-01 00:00:00' "
        "WHERE target_kind = 'issue' AND target_id = ?",
        (issue_ids[1],),
    )
    conn.commit()
    _schedule(
        conn,
        at="2030-01-01T00:00:00Z",
        conditions={"inactive_for_seconds": 3600},
    )
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, _r, event: calls.append(event["target_id"]),
            actor_id=actor_id,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
        == 1
    )
    assert calls == [issue_ids[0]]


def test_inactivity_condition_ignores_imported_activity(tmp_path):
    # WHY: imported rows are foreign history (0041). A forge delivery or an import
    # bundle touching an issue must not reset the inactivity clock on work it
    # never did — the schedule-side twin of the event scan's native_only guard.
    db_file, issue_ids = _database(tmp_path, issue_count=2)
    conn = db.connect(db_file)
    # Issue A: its recent activity is IMPORTED — it does not count, so the issue
    # still reads as inactive (its native trail is older than the cutoff).
    conn.execute(
        "UPDATE activity SET created_at = '2030-01-01 00:00:00', "
        "imported_at = '2030-01-01 00:00:00' "
        "WHERE target_kind = 'issue' AND target_id = ?",
        (issue_ids[0],),
    )
    # Issue B: the same recent activity, NATIVE — it counts, and suppresses.
    conn.execute(
        "UPDATE activity SET created_at = '2030-01-01 00:00:00' "
        "WHERE target_kind = 'issue' AND target_id = ?",
        (issue_ids[1],),
    )
    conn.commit()
    _schedule(
        conn,
        at="2030-01-01T00:00:00Z",
        conditions={"inactive_for_seconds": 3600},
    )
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, _r, event: calls.append(event["target_id"]),
            actor_id=actor_id,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
        == 1
    )
    assert calls == [issue_ids[0]]


def test_0059_upgrades_existing_event_rules_without_moving_event_cursor(
    tmp_path, monkeypatch
):
    source_migrations = db.MIGRATIONS_DIR
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    schedule_migration = source_migrations / "0059_scheduled_automation.sql"
    for migration in source_migrations.glob("*.sql"):
        if migration.name < schedule_migration.name:
            shutil.copy2(migration, staged_migrations / migration.name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged_migrations)

    conn = db.connect(tmp_path / "upgrade.db")
    applied_before = db.migrate(conn)
    assert applied_before[-1].startswith("0058_")
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES (1, 'admin@example.com', 'Admin')"
    )
    conn.execute(
        "INSERT INTO automation_rules "
        "(name, trigger_verb, target_kind, conditions, action_type, action_params, "
        "created_by) VALUES ('existing', 'created', 'issue', '{}', 'comment', "
        '\'{"body":"hello"}\', 1)'
    )
    conn.commit()
    cursor_before = automation.get_cursor(conn)

    shutil.copy2(schedule_migration, staged_migrations / schedule_migration.name)
    assert db.migrate(conn) == [schedule_migration.name]
    existing = automation.get_rule(conn, 1)
    assert existing["trigger_type"] == "event"
    assert existing["schedule_at"] is None
    assert existing["next_scheduled_at"] is None
    assert existing["schedule_status"] == "event"
    assert automation.get_cursor(conn) == cursor_before
    assert {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }.issuperset(
        {
            "automation_schedule_state",
            "automation_schedule_firings",
            "automation_schedule_occurrences",
        }
    )
    assert automation._get_schedule_scan_cursor(conn) == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE automation_rules SET trigger_type = 'cron' WHERE id = 1")


def test_run_pass_executes_schedule_inside_existing_automation_loop(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    due = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    _schedule(conn, at=automation._format_utc_timestamp(due), body="one loop")
    conn.close()

    assert automation.run_pass(db_file) == 1
    verify = db.connect(db_file)
    try:
        assert verify.execute("SELECT body FROM comments").fetchone()["body"] == (
            "one loop"
        )
    finally:
        verify.close()


def test_deleting_rule_cancels_pending_receipts_but_retains_history(tmp_path):
    db_file, issue_ids = _database(tmp_path, issue_count=2)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-09-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: None,
            actor_id=actor_id,
            now=_now("2030-09-01T00:00:00Z"),
            max_actions=0,
        )
        == 0
    )
    trigger_ids = [
        row["trigger_event_id"]
        for row in conn.execute(
            "SELECT trigger_event_id FROM automation_schedule_occurrences "
            "ORDER BY issue_id"
        )
    ]
    assert automation_commands.delete_rule(conn, actor_id=1, rule_id=rule["id"])
    assert automation.get_rule(conn, rule["id"]) is None
    firing = conn.execute("SELECT state FROM automation_schedule_firings").fetchone()
    occurrences = conn.execute(
        "SELECT issue_id, state FROM automation_schedule_occurrences ORDER BY issue_id"
    ).fetchall()
    assert firing["state"] == "cancelled"
    assert [(row["issue_id"], row["state"]) for row in occurrences] == [
        (issue_id, "cancelled") for issue_id in issue_ids
    ]
    assert all(activity.get_activity(conn, event_id) for event_id in trigger_ids)


def test_equality_conditions_select_targets_and_empty_snapshot_completes(tmp_path):
    db_file, issue_ids = _database(tmp_path, issue_count=2)
    conn = db.connect(db_file)
    matching = _schedule(
        conn,
        at="2030-10-01T00:00:00Z",
        conditions={"status": "open"},
    )
    empty = _schedule(
        conn,
        at="2030-10-01T00:00:00Z",
        conditions={"status": "done"},
    )
    actor_id = automation.system_actor_id(conn)
    calls: list[tuple[int, int]] = []
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, rule, event: calls.append(
                (rule["id"], event["target_id"])
            ),
            actor_id=actor_id,
            now=_now("2030-10-01T00:00:00Z"),
        )
        == 2
    )
    assert calls == [(matching["id"], issue_id) for issue_id in issue_ids]
    empty_firing = conn.execute(
        "SELECT state, trigger_count FROM automation_schedule_firings "
        "WHERE rule_id = ?",
        (empty["id"],),
    ).fetchone()
    assert dict(empty_firing) == {"state": "completed", "trigger_count": 0}


def test_due_rule_budget_leaves_later_rule_visible_for_next_pass(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    first = _schedule(conn, at="2030-11-01T00:00:00Z")
    second = _schedule(conn, at="2030-11-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []
    now = _now("2030-11-01T00:00:00Z")
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, rule, _e: calls.append(rule["id"]),
            actor_id=actor_id,
            now=now,
            max_rules=1,
        )
        == 1
    )
    assert calls == [first["id"]]
    assert automation.get_rule(conn, second["id"])["next_scheduled_at"] == (
        "2030-11-01T00:00:00Z"
    )
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, rule, _e: calls.append(rule["id"]),
            actor_id=actor_id,
            now=now,
            max_rules=1,
        )
        == 1
    )
    assert calls == [first["id"], second["id"]]


def test_schedule_corrupted_after_claim_fails_closed_with_visible_retry(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    rule = _schedule(conn, at="2030-12-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    now = _now("2030-12-01T00:00:00Z")
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: None,
            actor_id=actor_id,
            now=now,
            max_actions=0,
        )
        == 0
    )
    conn.execute(
        "UPDATE automation_rules SET schedule_at = 'corrupt' WHERE id = ?",
        (rule["id"],),
    )
    conn.commit()
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: (_ for _ in ()).throw(
                AssertionError("malformed work executed")
            ),
            actor_id=actor_id,
            now=now,
        )
        == 0
    )
    occurrence = conn.execute(
        "SELECT state, attempt_count, last_error FROM automation_schedule_occurrences"
    ).fetchone()
    assert occurrence["state"] == "pending" and occurrence["attempt_count"] == 1
    assert "malformed schedule" in occurrence["last_error"]
    assert automation.get_rule(conn, rule["id"])["schedule_status"] == "malformed"


def test_schedule_runner_rejects_naive_or_non_utc_clock(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    actor_id = automation.system_actor_id(conn)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        automation.process_schedules(
            conn,
            executor=lambda *_: None,
            actor_id=actor_id,
            now=datetime(2030, 1, 1),
        )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        automation.process_schedules(
            conn,
            executor=lambda *_: None,
            actor_id=actor_id,
            now=datetime.fromisoformat("2030-01-01T01:00:00+01:00"),
        )


def test_live_runner_retries_semantic_action_rejections_and_keeps_them_visible(
    tmp_path,
):
    db_file, issue_ids = _database(tmp_path)
    conn = db.connect(db_file)
    before = issues.get_issue(conn, issue_ids[0])["status"]
    due = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    rule = automation.create_rule(
        conn,
        name="invalid-runtime-status",
        trigger_verb=automation.SCHEDULE_TRIGGER_VERB,
        action_type="set_status",
        action_params={"status": "not-a-real-status"},
        created_by=1,
        trigger_type="schedule",
        schedule_at=due,
    )
    conn.close()

    for _ in range(automation.MAX_SCHEDULE_ATTEMPTS):
        automation.run_pass(db_file)

    conn = db.connect(db_file)
    try:
        current = automation.get_rule(conn, rule["id"])
        occurrence = conn.execute(
            "SELECT * FROM automation_schedule_occurrences"
        ).fetchone()
        firing = conn.execute("SELECT * FROM automation_schedule_firings").fetchone()
        assert current is not None
        assert issues.get_issue(conn, issue_ids[0])["status"] == before
        assert occurrence["state"] == "failed"
        assert occurrence["attempt_count"] == automation.MAX_SCHEDULE_ATTEMPTS
        assert firing["state"] == "failed"
        assert current["failure_count"] == automation.MAX_SCHEDULE_ATTEMPTS
        assert current["schedule_status"] == "failing"
        assert "no such status" in current["last_error"]
    finally:
        conn.close()


def test_unbindable_condition_is_malformed_without_blocking_healthy_work(tmp_path):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    malformed = _schedule(
        conn, at="2031-01-01T00:00:00Z", conditions={"project_id": []}
    )
    healthy = _schedule(conn, at="2031-01-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, rule, _e: calls.append(rule["id"]),
            actor_id=actor_id,
            now=_now("2031-01-01T00:00:00Z"),
        )
        == 1
    )
    assert calls == [healthy["id"]]
    broken = automation.get_rule(conn, malformed["id"])
    assert broken["schedule_status"] == "malformed"
    assert "project_id" in broken["configuration_error"]


def test_bounded_due_scan_rotates_past_more_than_limit_malformed_rules(
    tmp_path,
):
    db_file, _ = _database(tmp_path)
    conn = db.connect(db_file)
    for index in range(automation.MAX_SCHEDULE_RULE_SCAN + 1):
        _schedule(
            conn,
            at="2031-02-01T00:00:00Z",
            conditions={"project_id": []},
            body=f"malformed {index}",
        )
    healthy = _schedule(conn, at="2031-02-01T00:00:00Z")
    actor_id = automation.system_actor_id(conn)
    calls: list[int] = []
    now = _now("2031-02-01T00:00:00Z")
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, rule, _e: calls.append(rule["id"]),
            actor_id=actor_id,
            now=now,
        )
        == 0
    )
    assert calls == []
    assert (
        automation.process_schedules(
            conn,
            executor=lambda _c, rule, _e: calls.append(rule["id"]),
            actor_id=actor_id,
            now=now,
        )
        == 1
    )
    assert calls == [healthy["id"]]
    assert automation.get_rule(conn, healthy["id"])["schedule_status"] == ("completed")


@pytest.mark.parametrize(
    ("action_type", "recorder_name", "expected_verb"),
    [
        ("add_label", "record_label_added", "labeled"),
        ("add_contributor", "record_contributor_added", "added_contributor"),
    ],
)
def test_relation_actions_roll_back_relation_audit_and_watch_when_recorder_raises(
    tmp_path,
    monkeypatch,
    action_type,
    recorder_name,
    expected_verb,
):
    db_file, issue_ids = _database(tmp_path)
    contributor_id = (
        _create_contributor(db_file) if action_type == "add_contributor" else None
    )
    conn = db.connect(db_file)
    now = _now("2031-03-01T00:00:00Z")
    action_params = (
        {"label": "Atomic"}
        if action_type == "add_label"
        else {"user_id": contributor_id}
    )
    rule = _action_schedule(
        conn,
        at="2031-03-01T00:00:00Z",
        action_type=action_type,
        action_params=action_params,
    )
    actor_id = automation.system_actor_id(conn)
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: None,
            actor_id=actor_id,
            now=now,
            max_actions=0,
        )
        == 0
    )
    occurrence = conn.execute(
        "SELECT * FROM automation_schedule_occurrences"
    ).fetchone()
    trigger = activity.get_activity(conn, occurrence["trigger_event_id"])
    assert trigger is not None
    action_run_id = automation.automation_run_id(rule["id"], trigger["id"])
    baseline_activity = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    baseline_notifications = conn.execute(
        "SELECT COUNT(*) FROM notifications"
    ).fetchone()[0]
    baseline_watches = conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0]

    original_recorder = getattr(issue_activity, recorder_name)

    def record_then_raise(current_conn, **kwargs):
        original_recorder(current_conn, **kwargs)
        raise RuntimeError(f"{expected_verb} recorder failed")

    monkeypatch.setattr(issue_activity, recorder_name, record_then_raise)
    with pytest.raises(RuntimeError, match=f"{expected_verb} recorder failed"):
        automation.execute_action(
            conn,
            rule,
            trigger,
            actor_id=actor_id,
            fail_closed=True,
        )

    if action_type == "add_label":
        relation_count = conn.execute(
            "SELECT COUNT(*) FROM issue_labels WHERE issue_id = ?",
            (issue_ids[0],),
        ).fetchone()[0]
    else:
        relation_count = conn.execute(
            "SELECT COUNT(*) FROM issue_contributors "
            "WHERE issue_id = ? AND user_id = ?",
            (issue_ids[0], contributor_id),
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM watches "
                "WHERE user_id = ? AND target_kind = 'issue' AND target_id = ?",
                (contributor_id, issue_ids[0]),
            ).fetchone()[0]
            == 0
        )

    assert relation_count == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM activity WHERE run_id = ? AND verb = ?",
            (action_run_id, expected_verb),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM run_bindings WHERE run_id = ?",
            (action_run_id,),
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == (
        baseline_activity
    )
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == (
        baseline_notifications
    )
    assert conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == (
        baseline_watches
    )
    assert conn.in_transaction is False
    with db.transaction(conn, immediate=True):
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert conn.in_transaction is False
    conn.close()


@pytest.mark.parametrize(
    ("action_type", "expected_verb"),
    [
        ("add_label", "labeled"),
        ("add_contributor", "added_contributor"),
    ],
)
def test_relation_schedule_action_restart_deduplicates_before_receipt_advances(
    tmp_path,
    action_type,
    expected_verb,
):
    db_file, issue_ids = _database(tmp_path)
    contributor_id = (
        _create_contributor(db_file) if action_type == "add_contributor" else None
    )
    conn = db.connect(db_file)
    now = _now("2031-04-01T00:00:00Z")
    action_params = (
        {"label": "Restart safe"}
        if action_type == "add_label"
        else {"user_id": contributor_id}
    )
    rule = _action_schedule(
        conn,
        at="2031-04-01T00:00:00Z",
        action_type=action_type,
        action_params=action_params,
    )
    actor_id = automation.system_actor_id(conn)
    assert (
        automation.process_schedules(
            conn,
            executor=lambda *_: None,
            actor_id=actor_id,
            now=now,
            max_actions=0,
        )
        == 0
    )
    occurrence = conn.execute(
        "SELECT * FROM automation_schedule_occurrences"
    ).fetchone()
    trigger = activity.get_activity(conn, occurrence["trigger_event_id"])
    assert trigger is not None
    assert occurrence["state"] == "pending" and occurrence["attempt_count"] == 0
    assert (
        automation.execute_action(
            conn,
            rule,
            trigger,
            actor_id=actor_id,
            fail_closed=True,
        )
        is True
    )
    action_run_id = automation.automation_run_id(rule["id"], trigger["id"])
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM activity WHERE run_id = ?", (action_run_id,)
        ).fetchone()[0]
        == 1
    )
    pending = conn.execute(
        "SELECT state, attempt_count FROM automation_schedule_occurrences"
    ).fetchone()
    assert dict(pending) == {"state": "pending", "attempt_count": 0}
    conn.close()

    restarted = db.connect(db_file)
    try:
        resumed_rule = automation.get_rule(restarted, rule["id"])
        assert resumed_rule is not None

        def strict_execute(current_conn, current_rule, event):
            automation.execute_action(
                current_conn,
                current_rule,
                event,
                actor_id=actor_id,
                fail_closed=True,
            )

        assert (
            automation.process_schedules(
                restarted,
                executor=strict_execute,
                actor_id=actor_id,
                now=now,
            )
            == 1
        )

        if action_type == "add_label":
            relation_count = restarted.execute(
                "SELECT COUNT(*) FROM issue_labels WHERE issue_id = ?",
                (issue_ids[0],),
            ).fetchone()[0]
        else:
            relation_count = restarted.execute(
                "SELECT COUNT(*) FROM issue_contributors "
                "WHERE issue_id = ? AND user_id = ?",
                (issue_ids[0], contributor_id),
            ).fetchone()[0]
        assert relation_count == 1

        action_events = restarted.execute(
            "SELECT * FROM activity WHERE run_id = ?", (action_run_id,)
        ).fetchall()
        assert len(action_events) == 1
        action_event = action_events[0]
        assert action_event["verb"] == expected_verb
        assert action_event["actor_id"] == actor_id
        assert action_event["target_id"] == issue_ids[0]
        assert action_event["parent_run_id"] == trigger["run_id"]
        assert action_event["forked_from_event_id"] == trigger["id"]
        assert trigger["run_id"] == automation.schedule_trigger_run_id(
            rule["id"],
            "2031-04-01T00:00:00Z",
            issue_ids[0],
        )
        final_occurrence = restarted.execute(
            "SELECT state, attempt_count FROM automation_schedule_occurrences"
        ).fetchone()
        assert dict(final_occurrence) == {"state": "completed", "attempt_count": 1}
        assert (
            restarted.execute(
                "SELECT state FROM automation_schedule_firings"
            ).fetchone()["state"]
            == "completed"
        )
    finally:
        restarted.close()
