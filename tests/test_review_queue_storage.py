"""Canonical association provenance, immutable history, and honest portability."""

import shutil
import sqlite3

import pytest

from athena.aegis import issues, projects
from athena.aegis.review_queue import build_project_review_queue
from athena.core import activity, db, portability
from test_review_queue import board as _board, issue, queue, review, submit

from support import index_search_without_entry_keys

board = _board


def event(conn, item, *, reporter=3, imported=False, restricted=False, detail=None):
    return activity.record(
        conn,
        actor_id=reporter,
        verb="claim_completed",
        target_kind="issue",
        target_id=item["id"],
        detail="generation " + "a" * 32 if detail is None else detail,
        imported_at="2020-01-01" if imported else None,
        **({"issue_project_ids": [999999]} if restricted else {}),
        commit=True,
    )


@pytest.mark.parametrize(
    "table,mutation",
    [
        (table, mutation)
        for table in ("issue_review_candidates", "issue_candidate_reviews")
        for mutation in ("UPDATE", "DELETE")
    ],
)
def test_canonical_rows_are_immutable_and_durable(board, table, mutation):
    conn, _, project = board
    candidate = submit(conn, issue(conn, project))
    review(
        conn,
        issue(conn, project, assignee=3),
        candidate["candidate_id"],
        verdict="PASS",
    )
    sql = (
        f"UPDATE {table} SET id=id" if mutation == "UPDATE" else f"DELETE FROM {table}"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable|durable"):
        conn.execute(sql)
    conn.rollback()
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.parametrize("which", ["submission", "review"])
@pytest.mark.parametrize("mutation", ["UPDATE", "DELETE"])
def test_referenced_activity_cannot_be_reinterpreted(board, which, mutation):
    conn, _, project = board
    candidate = submit(conn, issue(conn, project))
    linked = review(
        conn,
        issue(conn, project, assignee=3),
        candidate["candidate_id"],
        verdict="PASS",
    )
    event_id = (candidate if which == "submission" else linked)["completion_event_id"]
    sql = (
        "UPDATE activity SET detail='reinterpreted' WHERE id=?"
        if mutation == "UPDATE"
        else "DELETE FROM activity WHERE id=?"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(sql, (event_id,))
    conn.rollback()
    assert queue(conn, project)["items"][0]["review_state"] == "reported_pass"


@pytest.mark.parametrize(
    "bad", ["imported", "restricted", "other_issue", "same_account", "same_issue"]
)
def test_review_insert_requires_matching_native_distinct_provenance(board, bad):
    conn, _, project = board
    build, check = issue(conn, project), issue(conn, project, assignee=3)
    candidate = submit(conn, build)
    target = build if bad in {"other_issue", "same_issue"} else check
    source = event(
        conn,
        target,
        reporter=2 if bad == "same_account" else 3,
        imported=bad == "imported",
        restricted=bad == "restricted",
    )
    review_issue = build if bad == "same_issue" else check
    with pytest.raises(sqlite3.IntegrityError, match="distinct native"):
        conn.execute(
            "INSERT INTO issue_candidate_reviews(candidate_id,review_issue_id,completion_event_id) VALUES (?,?,?)",
            (candidate["candidate_id"], review_issue["id"], source["id"]),
        )
    conn.rollback()
    assert (
        conn.execute("SELECT COUNT(*) FROM issue_candidate_reviews").fetchone()[0] == 0
    )


@pytest.mark.parametrize(
    "bad", ["imported", "restricted", "other_issue", "wrong_predecessor"]
)
def test_submission_insert_requires_native_source_and_continuity(board, bad):
    conn, _, project = board
    build = issue(conn, project)
    previous = submit(conn, build)
    source = event(
        conn,
        issue(conn, project) if bad == "other_issue" else build,
        imported=bad == "imported",
        restricted=bad == "restricted",
    )
    with pytest.raises(sqlite3.IntegrityError, match="native completion"):
        conn.execute(
            "INSERT INTO issue_review_candidates(issue_id,completion_event_id,kind,source,revision,previous_candidate_id) VALUES (?,?,'git','repo',?,?)",
            (
                build["id"],
                source["id"],
                "a" * 40,
                None if bad == "wrong_predecessor" else previous["candidate_id"],
            ),
        )
    conn.rollback()
    assert (
        conn.execute("SELECT COUNT(*) FROM issue_review_candidates").fetchone()[0] == 1
    )


@pytest.mark.parametrize(
    "detail", ["generation " + "a" * 32 + "; reported_completion_v1 {", "x" * 100_000]
)
def test_newest_malformed_native_review_is_unknown_not_old_pass(board, detail):
    conn, _, project = board
    candidate = submit(conn, issue(conn, project))
    check = issue(conn, project, assignee=3)
    review(conn, check, candidate["candidate_id"], verdict="PASS")
    source = event(conn, check, detail=detail)
    conn.execute(
        "INSERT INTO issue_candidate_reviews(candidate_id,review_issue_id,completion_event_id) VALUES (?,?,?)",
        (candidate["candidate_id"], check["id"], source["id"]),
    )
    conn.commit()
    row = queue(conn, project)["items"][0]
    assert row["review_state"] == "unknown"
    assert row["latest_review"]["completion_event_id"] == source["id"]
    assert row["latest_review"]["report_available"] is False


def test_selective_portability_keeps_report_history_but_no_operational_associations(
    board, tmp_path
):
    conn, _, project = board
    candidate = submit(conn, issue(conn, project))
    review(
        conn,
        issue(conn, project, assignee=3),
        candidate["candidate_id"],
        verdict="PASS",
    )
    bundle = portability.export_bundle(conn, "project", project["id"])
    assert (
        "issue_review_candidates" not in bundle
        and "issue_candidate_reviews" not in bundle
    )
    assert len([a for a in bundle["activity"] if a["verb"] == "claim_completed"]) == 2
    target = db.connect(tmp_path / "imported.db")
    try:
        db.migrate(target)
        for user in bundle["users"]:
            target.execute(
                "INSERT INTO users(email,name,role) VALUES (?,?,'member')",
                (user["email"], user["name"]),
            )
        target.commit()
        manifest = portability.build_import_manifest(target, bundle)
        assert manifest["status"] == "ready"
        assert portability.replay_import_manifest(target, bundle, manifest)["ok"]
        imported_project = target.execute(
            "SELECT id FROM projects WHERE key='RVW'"
        ).fetchone()[0]
        assert (
            build_project_review_queue(target, project_id=imported_project, actor=None)[
                "items"
            ]
            == []
        )
        assert (
            target.execute("SELECT COUNT(*) FROM issue_review_candidates").fetchone()[0]
            == 0
        )
        assert (
            target.execute("SELECT COUNT(*) FROM issue_candidate_reviews").fetchone()[0]
            == 0
        )
        assert (
            target.execute(
                "SELECT COUNT(*) FROM activity WHERE verb='claim_completed' AND imported_at IS NOT NULL"
            ).fetchone()[0]
            == 2
        )
    finally:
        target.close()


def test_additive_migration_preserves_populated_baseline_without_backfill(
    tmp_path, monkeypatch
):
    current_dir = db.MIGRATIONS_DIR
    baseline = tmp_path / "baseline-migrations"
    baseline.mkdir()
    for path in current_dir.glob("*.sql"):
        if path.name < "0082":
            shutil.copy2(path, baseline / path.name)
    conn = db.connect(tmp_path / "upgrade.db")
    try:
        monkeypatch.setattr(db, "MIGRATIONS_DIR", baseline)
        # The baseline has no search entry map (H2); index as that schema did.
        index_search_without_entry_keys(monkeypatch)
        db.migrate(conn)
        conn.execute(
            "INSERT INTO users(id,email,name,role) VALUES (1,'owner@example.invalid','Owner','admin')"
        )
        conn.commit()
        project = projects.create_project(
            conn, name="Retained", key="OLD", created_by=1, commit=True
        )
        item = issues.create_issue(
            conn,
            title="Retained issue",
            body="baseline",
            created_by=1,
            project_id=project["id"],
            commit=True,
        )
        source = event(conn, item, reporter=1)
        old_ledger = [
            tuple(row) for row in conn.execute("SELECT * FROM schema_migrations")
        ]
        migration = current_dir / "0082_issue_review_candidates.sql"
        shutil.copy2(migration, baseline / migration.name)
        assert db.migrate(conn) == [migration.name]
        assert db.migration_status(conn).current
        assert [tuple(row) for row in conn.execute("SELECT * FROM schema_migrations")][
            :-1
        ] == old_ledger
        assert issues.get_issue(conn, item["id"])["body"] == "baseline"
        assert (
            conn.execute(
                "SELECT id FROM activity WHERE id=?", (source["id"],)
            ).fetchone()[0]
            == source["id"]
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM issue_review_candidates").fetchone()[0]
            == 0
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            row["name"]
            for row in conn.execute("PRAGMA index_list(issue_review_candidates)")
        } >= {"idx_issue_review_candidates_latest"}
        assert {
            row["name"]
            for row in conn.execute("PRAGMA index_list(issue_candidate_reviews)")
        } >= {"idx_issue_candidate_reviews_latest"}
    finally:
        conn.close()


def test_whole_database_snapshot_preserves_candidate_and_review_history(
    board, tmp_path
):
    conn, _, project = board
    candidate = submit(conn, issue(conn, project))
    linked = review(
        conn,
        issue(conn, project, assignee=3),
        candidate["candidate_id"],
        verdict="HOLD",
    )
    saved = db.connect(tmp_path / "saved.db")
    try:
        conn.backup(saved)
        restored = queue(saved, project)["items"][0]
        assert restored["candidate"]["candidate_id"] == candidate["candidate_id"]
        assert restored["latest_review"]["review_id"] == linked["review_id"]
        assert restored["review_state"] == "reported_hold"
        assert db.migration_status(saved).current
    finally:
        saved.close()
