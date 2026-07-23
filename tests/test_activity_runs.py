"""Run reconstruction — reading the append-only trail as discrete work sessions.

A "run" is a derived lens, not a stored thing: one actor's maximal stretch of
events with no gap longer than gap_seconds. This is the first step toward replaying
"what did this agent do" as sessions, built entirely from the audit log we already
keep. These tests pin the contract: the gap splits runs (and the boundary is
deterministic), a run is one actor's work regardless of who else acted in between,
events within a run read oldest-first, runs come newest-first, and the API requires
an actor and authentication.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _user(conn, name, *, is_agent=0):
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES (?, ?, ?)",
        (f"{name.lower()}@e.com", name, is_agent),
    )
    conn.commit()


def _event(conn, *, actor, ts, target=1, verb="created", kind="issue"):
    """Append an activity row with an explicit timestamp, so gap behaviour is
    deterministic rather than wall-clock dependent."""
    conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, '', ?)",
        (actor, verb, kind, target, ts),
    )
    conn.commit()


# --- data layer -------------------------------------------------------------


def test_runs_split_on_gap_and_order(tmp_path):
    conn = _migrated_conn(tmp_path / "split.db")
    _user(conn, "Bot", is_agent=1)
    # Run A: three events within a minute.
    _event(conn, actor=1, ts="2026-06-27 10:00:00", verb="created")
    _event(conn, actor=1, ts="2026-06-27 10:00:30", verb="changed_status")
    _event(conn, actor=1, ts="2026-06-27 10:01:00", verb="commented")
    # Four hours quiet, then Run B: two events.
    _event(conn, actor=1, ts="2026-06-27 14:00:00", verb="created", target=2)
    _event(conn, actor=1, ts="2026-06-27 14:00:10", verb="assigned", target=2)

    runs = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)
    assert len(runs) == 2
    # Newest run first.
    assert runs[0]["started_at"] == "2026-06-27 14:00:00"
    assert runs[0]["event_count"] == 2
    assert runs[0]["first_id"] < runs[0]["last_id"]
    # Within a run, events read oldest-first.
    assert [e["verb"] for e in runs[1]["events"]] == [
        "created",
        "changed_status",
        "commented",
    ]
    assert runs[1]["started_at"] == "2026-06-27 10:00:00"
    assert runs[1]["ended_at"] == "2026-06-27 10:01:00"
    conn.close()


def test_runs_collapse_when_close(tmp_path):
    conn = _migrated_conn(tmp_path / "close.db")
    _user(conn, "Bot", is_agent=1)
    for i in range(4):
        _event(conn, actor=1, ts=f"2026-06-27 09:0{i}:00")
    runs = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)
    assert len(runs) == 1
    assert runs[0]["event_count"] == 4
    conn.close()


def test_run_gap_boundary_is_deterministic(tmp_path):
    # WHY: a run boundary must be the gap, exactly — events EXACTLY gap_seconds apart
    # are one run (not >), one second more splits. No wall-clock, no fuzz.
    conn = _migrated_conn(tmp_path / "boundary.db")
    _user(conn, "Bot", is_agent=1)
    _event(conn, actor=1, ts="2026-06-27 10:00:00")
    _event(conn, actor=1, ts="2026-06-27 10:30:00")  # exactly 1800s later
    assert len(activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)) == 1
    assert len(activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1799)) == 2
    conn.close()


def test_runs_are_actor_scoped(tmp_path):
    # WHY: a run is ONE actor's work. Another actor acting in the middle must neither
    # appear in the run nor break it — list_activity already scopes by actor.
    conn = _migrated_conn(tmp_path / "scoped.db")
    _user(conn, "Bot", is_agent=1)
    _user(conn, "Human")
    _event(conn, actor=1, ts="2026-06-27 10:00:00", verb="created")
    _event(conn, actor=2, ts="2026-06-27 10:00:05", verb="commented")  # interleaved
    _event(conn, actor=1, ts="2026-06-27 10:00:10", verb="changed_status")

    runs = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)
    assert len(runs) == 1
    assert runs[0]["event_count"] == 2
    assert {e["actor_name"] for e in runs[0]["events"]} == {"Bot"}
    conn.close()


# --- API --------------------------------------------------------------------


def test_runs_api_returns_actor_runs(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        client.post(
            "/users",
            json={"email": "b@e.com", "name": "Bot", "is_agent": True},
            headers=H1,
        )
        # The agent does a couple of things back-to-back → one run.
        iss = client.post(
            "/issues", json={"title": "x"}, headers={"X-Athena-Actor": "2"}
        ).json()
        client.patch(
            f"/issues/{iss['id']}",
            json={"status": "done"},
            headers={"X-Athena-Actor": "2"},
        )

        runs = client.get("/activity/runs?actor_id=2", headers=H1).json()
        assert len(runs) == 1
        assert runs[0]["actor_name"] == "Bot"
        assert runs[0]["event_count"] == 2
        assert [e["verb"] for e in runs[0]["events"]] == ["created", "changed_status"]


def test_runs_api_requires_actor_and_auth(tmp_path):
    with TestClient(create_app(tmp_path / "guard.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        # actor_id is required.
        assert client.get("/activity/runs", headers=H1).status_code == 422
        # Like the rest of the feed, runs are an authenticated read.
        assert client.get("/activity/runs?actor_id=1").status_code == 401
