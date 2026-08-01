"""The reviewer demo is real product data, disposable, and overwrite-safe."""

from pathlib import Path

import pytest
import uvicorn

from athena import config
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
    assert seeded["counts"]["rooms"] == 7
    assert seeded["counts"]["room_events"] == 4

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
        room_events = conn.execute(
            "SELECT re.event_kind, a.delivery_eligible "
            "FROM room_events re JOIN activity a ON a.id = re.activity_id "
            "ORDER BY re.activity_id"
        ).fetchall()
        assert [row["event_kind"] for row in room_events] == [
            "message",
            "decision",
            "check_in",
            "handoff",
        ]
        assert all(row["delivery_eligible"] == 0 for row in room_events)
    finally:
        conn.close()


def test_seed_demo_records_the_creation_audit_events(tmp_path):
    # The demo is the review-facing tour of a "load-bearing, not decorative"
    # audit log: every seeded container must appear WITH its lifecycle event.
    # Seeding through the bare data layer once shipped a project with no
    # created_project event — a tour stop that materialized from nowhere.
    db_path = tmp_path / "audit.db"
    seeded = seed_demo(db_path)

    conn = db.connect(db_path)
    try:
        events = activity.list_activity(conn, limit=500)
    finally:
        conn.close()
    by_verb: dict[str, list] = {}
    for event in events:
        by_verb.setdefault(event["verb"], []).append(event)

    assert len(by_verb["created_project"]) == 1
    assert by_verb["created_project"][0]["target_id"] == seeded["ids"]["project"]
    assert len(by_verb["space_created"]) == 1
    assert by_verb["space_created"][0]["target_id"] == seeded["ids"]["space"]
    assert len(by_verb["page_created"]) == 2
    assert {e["target_id"] for e in by_verb["page_created"]} == {
        seeded["ids"]["guide"],
        seeded["ids"]["protocol"],
    }


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


def test_demo_server_keeps_loopback_host_and_disables_proxy_header_trust(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)

    assert main(["--db", str(tmp_path / "served.db"), "--port", "8765"]) == 0

    assert captured["app"] is not None
    assert captured["kwargs"] == {
        "host": "127.0.0.1",
        "port": 8765,
        "workers": 1,
        "reload": False,
        "lifespan": "on",
        "proxy_headers": False,
        "server_header": False,
    }
    assert config.TRUST_ACTOR_HEADER is False


def test_seed_demo_mints_a_scoped_agent_token_for_mcp(tmp_path):
    # The five-minute tour must include the differentiator: connecting an agent
    # over MCP. Seeding mints Sol a least-privilege token through the audited
    # command — raw secret returned once, hash in the DB, mint on the trail.
    db_path = tmp_path / "token.db"
    seeded = seed_demo(db_path)

    assert seeded["agent_email"] == "sol@athena.local"
    assert seeded["agent_token"].startswith("ath_")
    assert seeded["agent_scopes"] == [
        "read",
        "issue:write",
        "docs:write",
        "rooms:write",
    ]

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT user_id, scopes, token_hash FROM api_tokens"
        ).fetchone()
        assert row["user_id"] == seeded["ids"]["sol"]
        assert row["scopes"] == "read issue:write docs:write rooms:write"
        assert row["token_hash"] != seeded["agent_token"]  # hash, never the raw
        minted = [
            e
            for e in activity.list_activity(conn, limit=200)
            if e["verb"] == "minted_token"
        ]
        assert len(minted) == 1 and minted[0]["actor_id"] == seeded["ids"]["sol"]
    finally:
        conn.close()
