"""The reviewer demo is real product data, disposable, and overwrite-safe."""

from pathlib import Path

import pytest

from athena.core import activity, db
from athena.demo import DEMO_RUN_ID, DemoSetupError, main, seed_demo


def test_seed_demo_builds_a_cross_linked_agent_workspace(tmp_path):
    db_path = tmp_path / "review.db"
    seeded = seed_demo(db_path)

    assert Path(seeded["db_path"]) == db_path.resolve()
    assert Path(seeded["attach_dir"]).is_dir()
    assert seeded["counts"]["users"] == 3
    assert seeded["counts"]["projects"] == 1
    assert seeded["counts"]["issues"] == 3
    assert seeded["counts"]["spaces"] == 1
    assert seeded["counts"]["pages"] == 2

    conn = db.connect(db_path)
    try:
        agents = conn.execute(
            "SELECT name FROM users WHERE is_agent = 1 ORDER BY name"
        ).fetchall()
        assert [row["name"] for row in agents] == ["Sol Builder", "Terra Reviewer"]
        issues = conn.execute(
            "SELECT status, assignee_id FROM issues ORDER BY id"
        ).fetchall()
        assert {row["status"] for row in issues} == {"open", "in_progress", "done"}
        assert sum(row["assignee_id"] is not None for row in issues) == 2

        run_events = activity.list_activity(conn, run_id=DEMO_RUN_ID)
        assert run_events
        assert {event["actor_name"] for event in run_events} == {"Sol Builder"}
        assert any(event["verb"] == "issue_edited" for event in run_events)

        linked = conn.execute(
            "SELECT COUNT(*) AS n FROM links WHERE target_kind = 'issue'"
        ).fetchone()["n"]
        assert linked >= 3
    finally:
        conn.close()


def test_seed_demo_refuses_existing_database_and_attachment_paths(tmp_path):
    db_path = tmp_path / "existing.db"
    db_path.write_bytes(b"do not replace")
    with pytest.raises(DemoSetupError, match="database already exists"):
        seed_demo(db_path)
    assert db_path.read_bytes() == b"do not replace"

    fresh_db = tmp_path / "fresh.db"
    attach_dir = tmp_path / "existing-attachments"
    attach_dir.mkdir()
    with pytest.raises(DemoSetupError, match="attachment path already exists"):
        seed_demo(fresh_db, attach_dir=attach_dir)
    assert not fresh_db.exists()


def test_demo_cli_seed_only_is_clear_and_idempotently_safe(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    assert main(["--db", str(db_path), "--seed-only"]) == 0
    output = capsys.readouterr()
    assert "Athena demo workspace created" in output.out
    assert "operator@athena.local" in output.out
    assert "athena-demo" in output.out

    # A retry explains the conflict and leaves the seeded database untouched.
    before = db_path.read_bytes()
    assert main(["--db", str(db_path), "--seed-only"]) == 1
    output = capsys.readouterr()
    assert "database already exists" in output.err
    assert db_path.read_bytes() == before
