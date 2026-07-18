"""A run id belongs to the FIRST actor that writes under it.

Run ids are client-chosen header strings, so until this slice ANY actor could
write events into another actor's run — poisoning its replay, lineage, and the
Mission Control story built on top. These pin the P2 binding rule: the first
tagged write claims the run id (inside the same transaction as the event), a
tagged write by anyone else is a 403 that lands NOTHING (no row, no event), the
owner keeps writing freely, untagged writes never bind, and the migration
backfills historical runs to their earliest actor.
"""
import pytest
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="binding.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann"})
    client.post(
        "/users",
        json={"email": "b@e.com", "name": "Bee", "role": "member"},
        headers=H1,
    )


def _bindings(db_file):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT run_id, actor_id FROM run_bindings ORDER BY run_id"
    ).fetchall()
    conn.close()
    return {row["run_id"]: row["actor_id"] for row in rows}


def test_first_write_binds_and_the_owner_keeps_writing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        tagged = {**H1, "X-Athena-Run": "ann-run"}
        first = c.post("/issues", json={"title": "one"}, headers=tagged)
        assert first.status_code == 201
        second = c.patch(
            f"/issues/{first.json()['id']}", json={"status": "done"}, headers=tagged
        )
        assert second.status_code == 200
    assert _bindings(db_file) == {"ann-run": 1}


def test_second_actor_is_refused_and_nothing_lands(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        assert (
            c.post(
                "/issues",
                json={"title": "ann's"},
                headers={**H1, "X-Athena-Run": "shared"},
            ).status_code
            == 201
        )
        forged = c.post(
            "/issues",
            json={"title": "bee's forgery"},
            headers={"X-Athena-Actor": "2", "X-Athena-Run": "shared"},
        )
        assert forged.status_code == 403
        assert forged.json()["detail"] == "run 'shared' is bound to another actor"
        # The refused write rolled back WHOLE: no issue row, no event, and the
        # run's trail still has exactly one author.
        assert [i["title"] for i in c.get("/issues", headers=H1).json()] == ["ann's"]
        events = c.get("/activity", params={"run_id": "shared"}, headers=H1).json()
        assert {e["actor_id"] for e in events} == {1}
    assert _bindings(db_file) == {"shared": 1}


def test_untagged_writes_never_bind(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        assert c.post("/issues", json={"title": "plain"}, headers=H1).status_code == 201
    assert _bindings(db_file) == {}


def test_mcp_client_sees_a_clean_403(tmp_path):
    # The transport agents actually use: a second agent configured (or tricked)
    # into continuing someone else's run gets a clean AthenaError, not a 500.
    from athena.mcp.client import AthenaClient, AthenaError

    app, _ = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        raw_a = tc.post(
            "/tokens", json={"name": "a", "scopes": ["issue:write"]}, headers=H1
        ).json()["token"]
        raw_b = tc.post(
            "/tokens",
            json={"name": "b", "scopes": ["issue:write"]},
            headers={"X-Athena-Actor": "2"},
        ).json()["token"]

        a_http = TestClient(app)
        a_http.headers.update({"Authorization": f"Bearer {raw_a}"})
        AthenaClient(client=a_http, run_id="mission-7").create_issue(title="a's")

        b_http = TestClient(app)
        b_http.headers.update({"Authorization": f"Bearer {raw_b}"})
        intruder = AthenaClient(client=b_http, run_id="mission-7")
        with pytest.raises(AthenaError) as excinfo:
            intruder.create_issue(title="b's")
        assert excinfo.value.status_code == 403
        assert "bound to another actor" in str(excinfo.value)
    finally:
        tc.__exit__(None, None, None)


def test_migration_backfills_each_run_to_its_earliest_actor(tmp_path):
    db_file = tmp_path / "backfill.db"
    conn = db.connect(db_file)
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE activity (id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, "
        "verb TEXT NOT NULL, target_kind TEXT NOT NULL, target_id INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, run_id TEXT)"
    )
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'Ann')")
    conn.execute("INSERT INTO users (email, name) VALUES ('b@e.com', 'Bee')")
    conn.executemany(
        "INSERT INTO activity (id, actor_id, verb, target_kind, target_id, "
        "created_at, run_id) VALUES (?, ?, 'created', 'issue', 1, ?, ?)",
        [
            # 'contested' was historically shared: Bee wrote FIRST (lower id).
            (1, 2, "2026-07-01 10:00:00", "contested"),
            (2, 1, "2026-07-01 10:05:00", "contested"),
            (3, 1, "2026-07-01 11:00:00", "ann-only"),
            (4, 1, "2026-07-01 12:00:00", None),  # untagged: never bound
        ],
    )
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if path.name != "0048_run_bindings.sql":
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (path.name,)
            )
    conn.commit()

    applied = db.migrate(conn)
    rows = conn.execute(
        "SELECT run_id, actor_id, bound_at FROM run_bindings ORDER BY run_id"
    ).fetchall()
    conn.close()

    assert applied == ["0048_run_bindings.sql"]
    assert {(r["run_id"], r["actor_id"]) for r in rows} == {
        ("contested", 2),
        ("ann-only", 1),
    }
    by_run = {r["run_id"]: r["bound_at"] for r in rows}
    assert by_run["contested"] == "2026-07-01 10:00:00"  # the EARLIEST event's time
