"""0083 preserves native provenance through recovery and excludes it from imports."""

from contextlib import closing
import shutil
import sqlite3

import pytest

from athena.aegis import projects
from athena.core import activity, db, portability
from test_review_queue import board as _board, issue, submit
from test_review_requests_command import request, setup_request

board = _board


@pytest.mark.parametrize(
    "invalid",
    [
        "imported",
        "restricted",
        "wrong_issue",
        "wrong_verb",
        "nonempty_detail",
        "submitter",
        "same_issue",
        "missing_user",
    ],
)
def test_native_event_restrictions_and_foreign_keys(board, invalid):
    conn, _, project = board
    build, check, candidate = setup_request(conn, project)
    event = activity.record(
        conn,
        actor_id=1,
        target_kind="issue",
        target_id=build["id"]
        if invalid in {"wrong_issue", "same_issue"}
        else check["id"],
        verb="created" if invalid == "wrong_verb" else "review_requested",
        detail="Metadata goes in the association"
        if invalid == "nonempty_detail"
        else "",
        imported_at="2026-01-01" if invalid == "imported" else None,
        **({"issue_project_ids": [99999]} if invalid == "restricted" else {}),
    )
    sql = "INSERT INTO issue_review_requests(candidate_id,review_issue_id,reviewer_id,request_event_id) VALUES (?,?,?,?)"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            sql,
            (
                candidate,
                build["id"] if invalid == "same_issue" else check["id"],
                2
                if invalid == "submitter"
                else 9999
                if invalid == "missing_user"
                else 3,
                event["id"],
            ),
        )
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM issue_review_requests").fetchone()[0] == 0
    assert request(conn, check, candidate)["request_id"] == 1


@pytest.mark.parametrize("mutation", ["UPDATE", "DELETE", "REPLACE"])
@pytest.mark.parametrize("table", ["issue_review_requests", "activity"])
def test_rows_and_events_are_immutable(board, table, mutation):
    conn, _, project = board
    _, check, candidate = setup_request(conn, project)
    request(conn, check, candidate)
    stored = conn.execute("SELECT * FROM issue_review_requests").fetchone()
    row_id = (
        stored["id"] if table == "issue_review_requests" else stored["request_event_id"]
    )
    sql = {
        "DELETE": f"DELETE FROM {table} WHERE id=?",
        "UPDATE": f"UPDATE {table} SET id=id WHERE id=?",
        "REPLACE": f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} WHERE id=?",
    }[mutation]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (row_id,))
    conn.rollback()
    assert tuple(
        conn.execute("SELECT * FROM issue_review_requests").fetchone()
    ) == tuple(stored)
    assert (
        conn.execute(
            "SELECT detail FROM activity WHERE id=?", (stored["request_event_id"],)
        ).fetchone()[0]
        == ""
    )


def test_event_and_request_rollback_together(board):
    conn, _, project = board
    _, check, candidate = setup_request(conn, project)
    # Force the association insert to fail after activity.record has appended its row and chain entry.
    conn.execute(
        "CREATE TRIGGER refuse_request BEFORE INSERT ON issue_review_requests BEGIN SELECT RAISE(ABORT, 'refuse'); END"
    )

    def counts():
        return [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "activity",
                "activity_chain",
                "notifications",
                "run_bindings",
                "issue_review_requests",
            )
        ]

    before = counts()
    with pytest.raises(sqlite3.IntegrityError, match="refuse"):
        request(conn, check, candidate)
    assert counts() == before
    assert not conn.in_transaction


def test_selective_portability_carries_only_generic_imported_activity(board, tmp_path):
    conn, _, project = board
    _, check, candidate = setup_request(conn, project)
    request(conn, check, candidate)
    bundle = portability.export_bundle(conn, "project", project["id"])
    assert "issue_review_requests" not in bundle
    with closing(db.connect(tmp_path / "imported.db")) as copied:
        db.migrate(copied)
        for user in bundle["users"]:
            copied.execute(
                "INSERT INTO users(email,name,role) VALUES (?,?,'member')",
                (user["email"], user["name"]),
            )
        copied.commit()
        manifest = portability.build_import_manifest(copied, bundle)
        assert manifest["status"] == "ready"
        assert portability.replay_import_manifest(copied, bundle, manifest)["ok"]
        assert (
            copied.execute("SELECT COUNT(*) FROM issue_review_requests").fetchone()[0]
            == 0
        )
        imported = copied.execute(
            "SELECT detail,imported_at FROM activity WHERE verb='review_requested'"
        ).fetchall()
        assert (
            len(imported) == 1
            and imported[0]["detail"] == ""
            and imported[0]["imported_at"] is not None
        )


def test_populated_0082_upgrade_and_restored_snapshot(
    tmp_path, monkeypatch, migration_inventory_through
):
    current = migration_inventory_through("0083_issue_review_requests.sql")
    baseline = tmp_path / "0082"
    baseline.mkdir()
    for migration in current.glob("*.sql"):
        if migration.name < "0083":
            shutil.copy2(migration, baseline / migration.name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", baseline)
    with (
        closing(db.connect(tmp_path / "upgrade.db")) as conn,
        closing(db.connect(tmp_path / "before.db")) as before,
        closing(db.connect(tmp_path / "after.db")) as after,
    ):
        db.migrate(conn)
        for number, role in [(1, "admin"), (2, "member"), (3, "member")]:
            conn.execute(
                "INSERT INTO users(id,email,name,role) VALUES (?,?,?,?)",
                (number, f"actor{number}@example.invalid", f"Actor {number}", role),
            )
        conn.commit()
        project = projects.create_project(
            conn, name="Retained", key="OLD", created_by=1
        )
        build, check = issue(conn, project), issue(conn, project, assignee=3)
        target = submit(conn, build)["candidate_id"]
        conn.backup(before)
        previous = db.migration_status(conn)
        monkeypatch.setattr(db, "MIGRATIONS_DIR", current)
        assert db.migrate(conn) == ["0083_issue_review_requests.sql"]
        assert db.migration_status(conn).current
        assert (
            conn.execute("SELECT COUNT(*) FROM issue_review_requests").fetchone()[0]
            == 0
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            r["name"] for r in conn.execute("PRAGMA index_list(issue_review_requests)")
        } >= {
            "idx_review_requests_issue",
            "idx_review_requests_candidate",
            "idx_review_requests_reviewer",
        }
        receipt = request(conn, check, target)
        conn.backup(after)
        assert db.migration_status(after).current
        assert request(after, check, target) == receipt
        monkeypatch.setattr(db, "MIGRATIONS_DIR", baseline)
        with pytest.raises(
            db.MigrationIntegrityError,
            match="future migration: 0083_issue_review_requests.sql",
        ):
            db.migration_status(conn)
        assert db.migration_status(before) == previous
        assert (
            before.execute(
                "SELECT id FROM issue_review_candidates WHERE issue_id=?",
                (build["id"],),
            ).fetchone()[0]
            == target
        )
        assert (
            before.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name='issue_review_requests'"
            ).fetchone()[0]
            == 0
        )
