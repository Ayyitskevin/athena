"""run_id-aware reconstruction — an explicit run id beats the time-gap heuristic.

#96 reconstructed runs purely by time gap; #98 added a recorded run id. This wires
the two together: where events carry an X-Athena-Run id that id is authoritative — a
tagged run holds together across any pause, two different ids split even back-to-back,
and a tagged event never shares a run with an untagged one. Untagged events still fall
back to the gap. These tests pin that precedence on the data layer, that the run
carries its id through the API, and that the browser run card surfaces it.
"""
from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _user(conn, name="Bot"):
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (f"{name}@e.com", name))
    conn.commit()


def _event(conn, *, ts, run_id=None, target=1, verb="created"):
    conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail, created_at, run_id) "
        "VALUES (1, ?, 'issue', ?, '', ?, ?)",
        (verb, target, ts, run_id),
    )
    conn.commit()


# --- data layer: run id beats the gap --------------------------------------


def test_same_run_id_holds_across_a_long_gap(tmp_path):
    # A six-hour pause would split an untagged run; the shared id holds it as one.
    conn = _migrated_conn(tmp_path / "hold.db")
    _user(conn)
    _event(conn, ts="2026-06-27 09:00:00", run_id="r1", verb="created")
    _event(conn, ts="2026-06-27 15:00:00", run_id="r1", verb="commented")
    runs = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "r1"
    assert runs[0]["event_count"] == 2
    conn.close()


def test_different_run_ids_split_even_when_adjacent(tmp_path):
    conn = _migrated_conn(tmp_path / "split.db")
    _user(conn)
    _event(conn, ts="2026-06-27 10:00:00", run_id="r1")
    _event(conn, ts="2026-06-27 10:00:05", run_id="r2")  # 5s later, different run
    runs = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)
    assert [r["run_id"] for r in runs] == ["r2", "r1"]  # newest run first
    assert all(r["event_count"] == 1 for r in runs)
    conn.close()


def test_tagged_and_untagged_never_share_a_run(tmp_path):
    conn = _migrated_conn(tmp_path / "mixed.db")
    _user(conn)
    _event(conn, ts="2026-06-27 10:00:00", run_id="r1")
    _event(conn, ts="2026-06-27 10:00:05", run_id=None)  # untagged, adjacent
    runs = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)
    assert len(runs) == 2
    assert sorted((r["run_id"] or "") for r in runs) == ["", "r1"]
    conn.close()


def test_untagged_events_still_group_by_gap(tmp_path):
    # Regression: with no run ids, behaviour is exactly the old gap heuristic, and
    # the run's run_id summarizes to None.
    conn = _migrated_conn(tmp_path / "gap.db")
    _user(conn)
    _event(conn, ts="2026-06-27 09:00:00")
    _event(conn, ts="2026-06-27 09:00:30")  # close → same run
    _event(conn, ts="2026-06-27 15:00:00")  # big gap → new run
    runs = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800)
    assert [r["run_id"] for r in runs] == [None, None]
    assert runs[1]["event_count"] == 2  # the oldest run held the two close ones
    conn.close()


# --- API + web carry the id -------------------------------------------------


def test_runs_api_carries_run_id(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        iss = client.post(
            "/issues", json={"title": "x"}, headers={**H1, "X-Athena-Run": "deploy-7"}
        ).json()
        client.patch(
            f"/issues/{iss['id']}",
            json={"status": "done"},
            headers={**H1, "X-Athena-Run": "deploy-7"},
        )
        runs = client.get("/activity/runs?actor_id=1", headers=H1).json()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "deploy-7"
        assert runs[0]["event_count"] == 2


def test_runs_web_card_shows_run_id(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        client.post(
            "/issues", json={"title": "x"}, headers={**H1, "X-Athena-Run": "deploy-7"}
        )
        page = client.get("/aegis/activity/runs?actor=1")
        assert page.status_code == 200
        assert 'class="run-id"' in page.text
        assert "run deploy-7" in page.text
