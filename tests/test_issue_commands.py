"""Shared issue commands keep every transport aligned and every write atomic.

These tests pin the architectural reason for the command layer: the browser and
REST API must not grow separate interpretations of one write, and Athena must
never persist agent-visible state without its projections and load-bearing audit
event. Failure injection proves rollback, rather than merely checking happy-path
HTTP responses.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena.aegis import (
    automation,
    dependencies,
    issue_activity,
    issue_commands,
    issues,
)
from athena.core import activity, db, notifications, search, users
from athena.main import create_app


def _command_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
        ("owner@example.com", "Owner", "admin"),
    )
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
        ("mentioned@example.com", "Mentioned", "member"),
    )
    conn.commit()
    return conn, users.get_user(conn, 1)


def _install_deferred_commit_trap(conn):
    conn.execute(
        "CREATE TEMP TABLE deferred_parent (id INTEGER PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TEMP TABLE deferred_child ("
        "parent_id INTEGER REFERENCES deferred_parent(id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    )
    conn.commit()


def _assert_no_issue_footprint(conn):
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    assert not conn.in_transaction


def test_create_rolls_back_when_search_projection_fails(tmp_path, monkeypatch):
    # WHY: returning 500 after leaving a real issue outside search/replay would
    # make the database and agent-facing projections disagree about what exists.
    conn, actor = _command_conn(tmp_path / "search-failure.db")

    def fail_index(*args, **kwargs):
        raise RuntimeError("injected search failure")

    monkeypatch.setattr(search, "index_document", fail_index)
    with pytest.raises(RuntimeError, match="injected search failure"):
        issue_commands.create_issue(
            conn,
            actor=actor,
            title="must roll back",
            body="references [[issue:999]]",
        )

    _assert_no_issue_footprint(conn)
    conn.close()


def test_create_rolls_back_when_required_audit_event_fails(tmp_path, monkeypatch):
    # WHY: Athena promises that every human/agent write is attributable. The row,
    # FTS entry, link projection, and auto-watch cannot survive without "created".
    conn, actor = _command_conn(tmp_path / "audit-failure.db")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(issue_activity, "record_created", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        issue_commands.create_issue(
            conn,
            actor=actor,
            title="must roll back",
            body="references [[issue:999]]",
        )

    _assert_no_issue_footprint(conn)
    conn.close()


def test_create_rolls_back_after_activity_watches_and_mentions_exist(
    tmp_path, monkeypatch
):
    # WHY: exercise the latest failure point in create. By the injected error,
    # issue/link/FTS/activity, the creator watch, and user-2 mention notification
    # have all been inserted; the outer command still has to erase every one.
    conn, actor = _command_conn(tmp_path / "late-failure.db")
    real_process_mentions = notifications.process_mentions

    def fail_after_mentions(*args, **kwargs):
        real_process_mentions(*args, **kwargs)
        raise RuntimeError("injected late mention failure")

    monkeypatch.setattr(notifications, "process_mentions", fail_after_mentions)
    with pytest.raises(RuntimeError, match="injected late mention failure"):
        issue_commands.create_issue(
            conn,
            actor=actor,
            title="must all roll back",
            body="notify [[user:2]] and link [[issue:999]]",
        )

    _assert_no_issue_footprint(conn)
    conn.close()


def test_status_rolls_back_when_its_audit_event_fails(tmp_path, monkeypatch):
    # WHY: a status transition is exactly the kind of agent action operators need
    # to replay. The state must stay at its old value when that fact cannot append.
    conn, actor = _command_conn(tmp_path / "status-failure.db")
    issue = issue_commands.create_issue(conn, actor=actor, title="still open")
    baseline_events = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    def fail_audit(*args, **kwargs):
        raise RuntimeError("injected status audit failure")

    monkeypatch.setattr(issue_activity, "record_status_change", fail_audit)
    with pytest.raises(RuntimeError, match="injected status audit failure"):
        issue_commands.update_issue(
            conn, actor=actor, issue_id=issue["id"], status="done"
        )

    assert issues.get_issue(conn, issue["id"])["status"] == "open"
    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == baseline_events
    assert not conn.in_transaction
    conn.close()


@pytest.mark.parametrize(
    ("initial_assignee", "target_assignee"),
    [(None, 2), (2, None)],
    ids=["assign", "unassign"],
)
def test_assignee_rolls_back_after_activity_and_notifications_exist(
    tmp_path, monkeypatch, initial_assignee, target_assignee
):
    # WHY: assignment is one audited responsibility change, not three loosely
    # related commits. Fail after the real activity writer has inserted its row
    # and notification fan-out; the issue row and new-assignee watch must still
    # roll back with them. Parameterizing pins both recorder branches.
    conn, actor = _command_conn(tmp_path / f"{target_assignee}-late-failure.db")
    issue = issue_commands.create_issue(conn, actor=actor, title="keep ownership honest")
    if initial_assignee is not None:
        issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=issue["id"],
            assignee_id=initial_assignee,
        )

    baseline = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("activity", "watches", "notifications")
    }
    real_record = activity.record

    def fail_after_record(*args, **kwargs):
        real_record(*args, **kwargs)
        raise RuntimeError("injected late assignment failure")

    monkeypatch.setattr(activity, "record", fail_after_record)
    with pytest.raises(RuntimeError, match="injected late assignment failure"):
        issue_commands.update_issue(
            conn,
            actor=actor,
            issue_id=issue["id"],
            assignee_id=target_assignee,
        )

    assert issues.get_issue(conn, issue["id"])["assignee_id"] == initial_assignee
    for table, count in baseline.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
    assert not conn.in_transaction
    conn.close()


def test_set_assignee_default_commit_failure_rolls_back_and_cleans_connection(
    tmp_path,
):
    # WHY: standalone data callers let this helper own COMMIT. A deferred
    # constraint can fail only during finalization, and must not leave either
    # the assignment or the caller's invalid row dirty on the connection.
    conn, actor = _command_conn(tmp_path / "assignee-finalize-failure.db")
    issue = issue_commands.create_issue(conn, actor=actor, title="stay unassigned")
    _install_deferred_commit_trap(conn)
    baseline = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("activity", "watches", "notifications")
    }
    conn.execute("INSERT INTO deferred_child (parent_id) VALUES (999)")

    with pytest.raises(sqlite3.IntegrityError):
        issues.set_assignee(conn, issue["id"], 2)

    assert not conn.in_transaction
    assert issues.get_issue(conn, issue["id"])["assignee_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM deferred_child").fetchone()[0] == 0
    for table, count in baseline.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
    conn.close()


def test_assignee_recorder_default_commit_failure_rolls_back_every_effect(
    tmp_path,
):
    # WHY: the standalone recorder owns one final commit for an assignment row,
    # new-assignee watch, activity event, and notification. If that commit fails,
    # its rollback must clear every effect and leave the connection reusable.
    conn, actor = _command_conn(tmp_path / "assignee-recorder-finalize-failure.db")
    issue = issue_commands.create_issue(conn, actor=actor, title="stay atomic")
    _install_deferred_commit_trap(conn)
    baseline = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("activity", "watches", "notifications")
    }
    issues.set_assignee(conn, issue["id"], 2, commit=False)
    conn.execute("INSERT INTO deferred_child (parent_id) VALUES (999)")

    with pytest.raises(sqlite3.IntegrityError):
        issue_activity.record_assignee_change(
            conn,
            actor_id=actor["id"],
            issue_id=issue["id"],
            before=None,
            after=2,
        )

    assert not conn.in_transaction
    assert issues.get_issue(conn, issue["id"])["assignee_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM deferred_child").fetchone()[0] == 0
    for table, count in baseline.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
    conn.close()


def test_command_uses_a_savepoint_without_committing_outer_work(tmp_path):
    # WHY: import/replay and future compound commands need to compose an issue
    # command into a larger transaction. Releasing the inner savepoint must not
    # durably commit either the caller's work or the issue command early.
    db_file = tmp_path / "nested.db"
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
        ("owner@example.com", "Owner", "admin"),
    )
    actor = users.get_user(conn, 1)
    issue_commands.create_issue(conn, actor=actor, title="inside outer transaction")
    assert conn.in_transaction

    observer = db.connect(db_file)
    assert observer.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert observer.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    observer.close()

    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    conn.close()


def test_failed_inner_command_preserves_callers_outer_work(tmp_path, monkeypatch):
    # WHY: rolling back a failed command's savepoint must not discard unrelated
    # writes the composing caller made before entering it.
    db_file = tmp_path / "nested-failure.db"
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
        ("owner@example.com", "Owner", "admin"),
    )
    actor = users.get_user(conn, 1)

    def fail_index(*args, **kwargs):
        raise RuntimeError("inner projection failed")

    monkeypatch.setattr(search, "index_document", fail_index)
    with pytest.raises(RuntimeError, match="inner projection failed"):
        issue_commands.create_issue(conn, actor=actor, title="roll back only me")

    assert conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    conn.commit()

    observer = db.connect(db_file)
    assert observer.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert observer.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    observer.close()
    conn.close()


def test_top_level_commit_failure_rolls_back_and_cleans_connection():
    # WHY: deferred constraints can fail only at COMMIT. Finalization is part of
    # the atomic boundary too; a failed commit must not leave dirty visible rows.
    conn = db.connect(":memory:")
    conn.executescript(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE child ("
        "  parent_id INTEGER REFERENCES parent(id) "
        "  DEFERRABLE INITIALLY DEFERRED"
        ");"
    )
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(conn):
            conn.execute("INSERT INTO child (parent_id) VALUES (999)")

    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
    conn.close()


def _bootstrap_and_login(client):
    created = client.post(
        "/users",
        json={
            "email": "owner@example.com",
            "name": "Owner",
            "password": "secret",
        },
    )
    assert created.status_code == 201
    logged_in = client.post(
        "/login",
        data={"email": "owner@example.com", "password": "secret"},
        follow_redirects=False,
    )
    assert logged_in.status_code == 303
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_rest_and_web_preserve_the_same_markdown_body_on_create_and_edit(tmp_path):
    # WHY: Markdown trailing spaces/newlines carry meaning. The old browser path
    # stripped them while REST preserved them, proving two command paths had drifted.
    db_file = tmp_path / "body-parity.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        original = "intro  \n  indented line\n"
        api_issue = client.post(
            "/issues",
            json={"title": "API", "body": original},
            headers={"X-Athena-Actor": "1"},
        ).json()
        web_created = client.post(
            "/aegis/issues",
            data={"title": "Web", "body": original, "priority": "medium"},
            follow_redirects=False,
        )
        assert web_created.status_code == 200
        web_id = int(web_created.headers["HX-Redirect"].rsplit("/", 1)[-1])

        conn = db.connect(db_file)
        assert issues.get_issue(conn, api_issue["id"])["body"] == original
        assert issues.get_issue(conn, web_id)["body"] == original
        conn.close()

        edited = "updated  \nsecond line\n"
        assert client.patch(
            f"/issues/{api_issue['id']}",
            json={"body": edited},
            headers={"X-Athena-Actor": "1"},
        ).status_code == 200
        assert client.post(
            f"/aegis/issues/{web_id}/edit",
            data={"title": "Web", "body": edited},
            follow_redirects=False,
        ).status_code == 303

        conn = db.connect(db_file)
        assert issues.get_issue(conn, api_issue["id"])["body"] == edited
        assert issues.get_issue(conn, web_id)["body"] == edited
        for issue_id in (api_issue["id"], web_id):
            verbs = [
                row["verb"]
                for row in conn.execute(
                    "SELECT verb FROM activity "
                    "WHERE target_kind = 'issue' AND target_id = ? ORDER BY id",
                    (issue_id,),
                )
            ]
            assert verbs == ["created", "issue_edited"]
        conn.close()


@pytest.mark.parametrize("surface", ["rest", "web", "bulk", "automation"])
def test_assignment_surfaces_share_audit_watch_notification_and_noop_semantics(
    tmp_path, surface
):
    # WHY: four adapters used to hand-roll assignment. They must now produce the
    # same durable facts, and resubmitting the same owner must stay a true no-op.
    db_file = tmp_path / f"assignee-{surface}.db"
    headers = {"X-Athena-Actor": "1"}
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        assert client.post(
            "/users",
            json={
                "email": "assignee@example.com",
                "name": "Assignee",
                "password": "secret",
                "role": "member",
            },
            headers=headers,
        ).status_code == 201
        issue = client.post(
            "/issues", json={"title": surface}, headers=headers
        ).json()

        expected_actor = 1
        for attempt in range(2):
            if surface == "rest":
                response = client.put(
                    f"/issues/{issue['id']}/assignee",
                    json={"assignee_id": 2},
                    headers=headers,
                )
                assert response.status_code == 200
            elif surface == "web":
                response = client.post(
                    f"/aegis/issues/{issue['id']}/assignee",
                    data={"assignee_id": "2"},
                    follow_redirects=False,
                )
                assert response.status_code == 303
            elif surface == "bulk":
                response = client.post(
                    "/issues/bulk",
                    json={"ids": [issue["id"]], "assignee_id": 2},
                    headers=headers,
                )
                assert response.status_code == 200
                assert response.json()["updated"] == 1
            else:
                conn = db.connect(db_file)
                expected_actor = automation.system_actor_id(conn)
                changed = automation.execute_action(
                    conn,
                    {"action_type": "assign", "action_params": {"user_id": 2}},
                    {"target_id": issue["id"]},
                    actor_id=expected_actor,
                )
                conn.close()
                assert changed is (attempt == 0)

    conn = db.connect(db_file)
    assigned = issues.get_issue(conn, issue["id"])
    assert assigned["assignee_id"] == 2
    assert assigned["assignee_name"] == "Assignee"
    events = conn.execute(
        "SELECT actor_id, detail FROM activity "
        "WHERE target_kind = 'issue' AND target_id = ? AND verb = 'assigned'",
        (issue["id"],),
    ).fetchall()
    assert len(events) == 1
    assert events[0]["actor_id"] == expected_actor
    assert events[0]["detail"] == "Assignee"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM watches "
            "WHERE user_id = 2 AND target_kind = 'issue' AND target_id = ?",
            (issue["id"],),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM notifications n "
            "JOIN activity a ON a.id = n.event_id "
            "WHERE n.user_id = 2 AND a.target_kind = 'issue' "
            "AND a.target_id = ? AND a.verb = 'assigned'",
            (issue["id"],),
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_update_authorizes_target_before_disclosing_invalid_input(tmp_path):
    # WHY: a malformed PATCH must not become an existence/authorization oracle.
    # This ordering also keeps REST aligned with the browser's pre-authorized form.
    db_file = tmp_path / "update-order.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        owner = {"X-Athena-Actor": "1"}
        assert client.post(
            "/users",
            json={
                "email": "outsider@example.com",
                "name": "Outsider",
                "password": "secret",
                "role": "member",
            },
            headers=owner,
        ).status_code == 201
        issue = client.post(
            "/issues", json={"title": "Owned"}, headers=owner
        ).json()
        outsider = {"X-Athena-Actor": "2"}

        for malformed in ({}, {"title": "   "}):
            assert client.patch(
                "/issues/9999", json=malformed, headers=owner
            ).status_code == 404
            assert client.patch(
                f"/issues/{issue['id']}", json=malformed, headers=outsider
            ).status_code == 403


def test_assignee_authorizes_source_before_validating_target(tmp_path):
    # WHY: an unknown target user must not turn assignment into an issue
    # existence/authorization oracle. Type checks belong after the same source
    # gate too, including direct callers that bypass Pydantic.
    conn, actor = _command_conn(tmp_path / "assignee-order.db")
    issue = issue_commands.create_issue(conn, actor=actor, title="private target")
    outsider = users.get_user(conn, 2)

    with pytest.raises(issue_commands.IssueCommandError) as missing:
        issue_commands.update_issue(
            conn, actor=actor, issue_id=9999, assignee_id=9999
        )
    assert (missing.value.kind, missing.value.detail) == (
        "not_found",
        "no such issue",
    )

    with pytest.raises(issue_commands.IssueCommandError) as forbidden:
        issue_commands.update_issue(
            conn, actor=outsider, issue_id=issue["id"], assignee_id=True
        )
    assert forbidden.value.kind == "forbidden"

    with pytest.raises(issue_commands.IssueCommandError) as bad_type:
        issue_commands.update_issue(
            conn, actor=actor, issue_id=issue["id"], assignee_id=True
        )
    assert (bad_type.value.kind, bad_type.value.detail) == (
        "invalid",
        "assignee_id must be an integer or null",
    )

    with pytest.raises(issue_commands.IssueCommandError) as unknown:
        issue_commands.update_issue(
            conn, actor=actor, issue_id=issue["id"], assignee_id=9999
        )
    assert (unknown.value.kind, unknown.value.detail) == (
        "invalid",
        "no such user",
    )
    assert not conn.in_transaction
    conn.close()


def test_combined_patch_commits_each_audit_fact_once(tmp_path):
    # WHY: one PATCH can carry content + lifecycle fields. The command must append
    # the three distinct facts once each, in the same transaction as the final row.
    db_file = tmp_path / "combined.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        headers = {"X-Athena-Actor": "1"}
        issue = client.post(
            "/issues", json={"title": "Before", "body": "old"}, headers=headers
        ).json()
        updated = client.patch(
            f"/issues/{issue['id']}",
            json={
                "title": "After",
                "body": "new",
                "status": "done",
                "priority": "urgent",
            },
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["title"] == "After"

        conn = db.connect(db_file)
        verbs = [
            row["verb"]
            for row in conn.execute(
                "SELECT verb FROM activity "
                "WHERE target_kind = 'issue' AND target_id = ? ORDER BY id",
                (issue["id"],),
            )
        ]
        assert verbs == [
            "created",
            "changed_status",
            "changed_priority",
            "issue_edited",
        ]
        conn.close()


def test_combined_command_orders_assignment_after_lifecycle_before_edit(tmp_path):
    # WHY: one command emits distinct facts in a stable order. Assignment follows
    # status/priority so bulk preserves its historical event sequence, while the
    # content edit remains the final summary fact.
    conn, actor = _command_conn(tmp_path / "combined-assignee.db")
    issue = issue_commands.create_issue(
        conn, actor=actor, title="Before", body="old"
    )
    issue_commands.update_issue(
        conn,
        actor=actor,
        issue_id=issue["id"],
        title="After",
        body="new",
        status="done",
        priority="urgent",
        assignee_id=2,
    )
    assert [
        row["verb"]
        for row in conn.execute(
            "SELECT verb FROM activity "
            "WHERE target_kind = 'issue' AND target_id = ? ORDER BY id",
            (issue["id"],),
        )
    ] == [
        "created",
        "changed_status",
        "changed_priority",
        "assigned",
        "issue_edited",
    ]
    conn.close()


def test_bulk_core_and_assignee_roll_back_as_one_item(tmp_path, monkeypatch):
    # WHY: routing assignee through a separate command would still let a bulk
    # status/priority commit survive an assignment-audit failure. Exercise the
    # real recorder before raising so row, all three events, watch, and inbox
    # fan-out have been attempted inside the one per-item transaction.
    db_file = tmp_path / "bulk-assignee-atomic.db"
    headers = {"X-Athena-Actor": "1"}
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        assert client.post(
            "/users",
            json={
                "email": "assignee@example.com",
                "name": "Assignee",
                "role": "member",
            },
            headers=headers,
        ).status_code == 201
        issue = client.post(
            "/issues", json={"title": "bulk atomic"}, headers=headers
        ).json()

        real_recorder = issue_activity.record_assignee_change

        def fail_after_assignment(*args, **kwargs):
            real_recorder(*args, **kwargs)
            raise RuntimeError("injected bulk assignment failure")

        monkeypatch.setattr(
            issue_activity, "record_assignee_change", fail_after_assignment
        )
        with pytest.raises(RuntimeError, match="injected bulk assignment failure"):
            client.post(
                "/issues/bulk",
                json={
                    "ids": [issue["id"]],
                    "status": "done",
                    "priority": "urgent",
                    "assignee_id": 2,
                },
                headers=headers,
            )

    conn = db.connect(db_file)
    unchanged = issues.get_issue(conn, issue["id"])
    assert (
        unchanged["status"],
        unchanged["priority"],
        unchanged["assignee_id"],
    ) == ("open", "medium", None)
    assert [
        row["verb"]
        for row in conn.execute(
            "SELECT verb FROM activity "
            "WHERE target_kind = 'issue' AND target_id = ? ORDER BY id",
            (issue["id"],),
        )
    ] == ["created"]
    assert not notifications.is_watching(conn, 2, "issue", issue["id"])
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    conn.close()


def test_custom_done_status_survives_web_blocker_confirmation(tmp_path):
    # WHY: done-ness is a project status CATEGORY, not the literal word "done".
    # The confirmation form must resubmit the requested custom status unchanged.
    db_file = tmp_path / "custom-done.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        headers = {"X-Athena-Actor": "1"}
        project = client.post(
            "/projects",
            json={"name": "Ops", "key": "OPS"},
            headers=headers,
        ).json()
        assert client.post(
            f"/projects/{project['id']}/statuses",
            json={"name": "shipped", "category": "done"},
            headers=headers,
        ).status_code == 201
        target = client.post(
            "/issues",
            json={"title": "Target", "project_id": project["id"]},
            headers=headers,
        ).json()
        blocker = client.post(
            "/issues",
            json={"title": "Blocker", "project_id": project["id"]},
            headers=headers,
        ).json()
        conn = db.connect(db_file)
        assert dependencies.add_link(
            conn,
            from_id=blocker["id"],
            to_id=target["id"],
            relation="blocks",
            created_by=1,
        ) is None
        conn.close()

        warning = client.post(
            f"/aegis/issues/{target['id']}/status",
            data={"status": "shipped"},
            follow_redirects=False,
        )
        assert warning.status_code == 200
        assert 'name="status" value="shipped"' in warning.text
        assert client.get(f"/issues/{target['id']}").json()["status"] == "open"

        confirmed = client.post(
            f"/aegis/issues/{target['id']}/status",
            data={"status": "shipped", "confirm": "1"},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        assert client.get(f"/issues/{target['id']}").json()["status"] == "shipped"


def test_automation_status_rolls_back_when_audit_fails(tmp_path, monkeypatch):
    # WHY: automation used to commit status, fail its audit, swallow the error,
    # and advance the cursor. It now uses the same atomic status command under an
    # explicit system-actor policy.
    db_file = tmp_path / "automation-atomic.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        issue = client.post(
            "/issues",
            json={"title": "Automate me"},
            headers={"X-Athena-Actor": "1"},
        ).json()

        conn = db.connect(db_file)
        actor_id = automation.system_actor_id(conn)
        baseline_events = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

        def fail_audit(*args, **kwargs):
            raise RuntimeError("automation audit failed")

        monkeypatch.setattr(issue_activity, "record_status_change", fail_audit)
        with pytest.raises(RuntimeError, match="automation audit failed"):
            automation.execute_action(
                conn,
                {"action_type": "set_status", "action_params": {"status": "done"}},
                {"target_id": issue["id"]},
                actor_id=actor_id,
            )

        assert issues.get_issue(conn, issue["id"])["status"] == "open"
        assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == baseline_events
        assert not conn.in_transaction
        conn.close()


def test_automation_assignee_rolls_back_when_audit_fails(tmp_path, monkeypatch):
    # WHY: the system policy must use the same assignment transaction as human
    # adapters. A late audit failure cannot leave an automated owner, watch, or
    # notification behind even though the executor itself is best-effort.
    db_file = tmp_path / "automation-assignee-atomic.db"
    headers = {"X-Athena-Actor": "1"}
    with TestClient(create_app(db_file)) as client:
        _bootstrap_and_login(client)
        assert client.post(
            "/users",
            json={
                "email": "assignee@example.com",
                "name": "Assignee",
                "role": "member",
            },
            headers=headers,
        ).status_code == 201
        issue = client.post(
            "/issues", json={"title": "Automate ownership"}, headers=headers
        ).json()

        conn = db.connect(db_file)
        actor_id = automation.system_actor_id(conn)
        baseline = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("activity", "watches", "notifications")
        }
        real_recorder = issue_activity.record_assignee_change

        def fail_after_assignment(*args, **kwargs):
            real_recorder(*args, **kwargs)
            raise RuntimeError("automation assignment audit failed")

        monkeypatch.setattr(
            issue_activity, "record_assignee_change", fail_after_assignment
        )
        with pytest.raises(RuntimeError, match="automation assignment audit failed"):
            automation.execute_action(
                conn,
                {"action_type": "assign", "action_params": {"user_id": 2}},
                {"target_id": issue["id"]},
                actor_id=actor_id,
            )

        assert issues.get_issue(conn, issue["id"])["assignee_id"] is None
        for table, count in baseline.items():
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert not conn.in_transaction
        conn.close()
