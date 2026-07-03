"""Run replay artifacts freeze one tagged run for handoff and audit."""

import json

from fastapi.testclient import TestClient

from athena import ops
from athena.core import activity, db, run_context, run_replay
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name, role) VALUES ('a@e.com', 'A', 'admin')")
    conn.commit()
    return conn


def _rec(conn, *, run_id, parent=None, forked_from=None, target_id=1, verb="created"):
    run_token = run_context.set_run_id(run_id)
    parent_token = run_context.set_parent_run_id(parent)
    fork_token = run_context.set_forked_from_event_id(forked_from)
    try:
        return activity.record(
            conn,
            actor_id=1,
            verb=verb,
            target_kind="issue",
            target_id=target_id,
            detail=f"{verb} issue {target_id}",
        )
    finally:
        run_context.reset_forked_from_event_id(fork_token)
        run_context.reset_parent_run_id(parent_token)
        run_context.reset_run_id(run_token)


def test_run_replay_artifact_exports_ordered_events_and_lineage(tmp_path):
    conn = _conn(tmp_path / "artifact.db")
    root = _rec(conn, run_id="goal", target_id=1)
    _rec(conn, run_id="goal", target_id=2, verb="triaged")
    child_first = _rec(
        conn,
        run_id="goal:alt",
        parent="goal",
        forked_from=root["id"],
        target_id=3,
    )
    child_second = _rec(
        conn,
        run_id="goal:alt",
        parent="goal",
        forked_from=root["id"],
        target_id=4,
        verb="commented",
    )

    artifact = run_replay.build_run_replay_artifact(conn, "goal:alt")

    assert artifact["schema"] == run_replay.SCHEMA
    assert artifact["run_id"] == "goal:alt"
    assert artifact["event_count"] == 2
    assert artifact["first_event_id"] == child_first["id"]
    assert artifact["last_event_id"] == child_second["id"]
    assert [event["id"] for event in artifact["events"]] == [
        child_first["id"],
        child_second["id"],
    ]
    assert all(event["forked_from_event_id"] == root["id"] for event in artifact["events"])
    assert [node["run_id"] for node in artifact["lineage"]["ancestors"]] == ["goal"]
    assert artifact["lineage"]["run"]["event_count"] == 2
    assert artifact["lineage"]["run"]["events"] == []
    assert "detail" in artifact["determinism_contract"]["safe_fields"]
    conn.close()


def test_run_replay_endpoint_returns_visible_artifact(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        issue = client.post(
            "/issues",
            json={"title": "parent"},
            headers={**H1, "X-Athena-Run": "goal"},
        ).json()
        client.patch(
            f"/issues/{issue['id']}",
            json={"status": "in_progress"},
            headers={**H1, "X-Athena-Run": "child", "X-Athena-Parent-Run": "goal"},
        )

        response = client.get("/activity/runs/goal/replay", headers=H1)

        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == run_replay.SCHEMA
        assert body["event_count"] == 1
        assert body["events"][0]["run_id"] == "goal"
        assert [node["run_id"] for node in body["lineage"]["descendants"]] == ["child"]
        assert client.get("/activity/runs/goal/replay").status_code == 401
        assert client.get("/activity/runs/missing/replay", headers=H1).status_code == 404


def test_run_replay_artifact_caps_a_huge_run(tmp_path, monkeypatch):
    # WHY: a run is tagged by a client header with no server-side event-count limit, so
    # replay must not materialize an unbounded event list / JSON body. Beyond the cap the
    # artifact holds the first N events and flags itself partial.
    monkeypatch.setattr(run_replay, "_MAX_REPLAY_EVENTS", 3)
    conn = _conn(tmp_path / "cap.db")
    for i in range(5):
        _rec(conn, run_id="big", target_id=i + 1)
    art = run_replay.build_run_replay_artifact(conn, "big")
    assert art["partial"] is True
    assert art["event_count"] == 3 and len(art["events"]) == 3
    assert art["lineage"]["run"]["partial"] is True

    _rec(conn, run_id="small", target_id=99)
    small = run_replay.build_run_replay_artifact(conn, "small")
    assert small["partial"] is False  # a run under the cap is whole
    conn.close()


def test_run_replay_endpoint_gates_hidden_runs(tmp_path):
    # WHY: the "hidden runs are 404s, not leaks" property (build_run_replay_artifact
    # threads actor= into both list_events and run_lineage) was previously unverified —
    # every existing test used an ungated call or a single all-seeing admin.
    h_creator = {"X-Athena-Actor": "2"}
    h_outsider = {"X-Athena-Actor": "3"}
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "Admin", "password": "pw"}, headers=H1)
        client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H1)
        client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H1)
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=h_creator).json()["id"]
        client.post(
            "/issues", json={"title": "secret", "project_id": pid},
            headers={**h_creator, "X-Athena-Run": "p1"},
        )
        client.put(f"/projects/{pid}/visibility", json={"visibility": "private"}, headers=h_creator)

        # The outsider can't see the only event, so the run's replay is a 404 — no leak.
        assert client.get("/activity/runs/p1/replay", headers=h_outsider).status_code == 404
        # The creator (a member) and the admin (god-view) both get the artifact.
        assert client.get("/activity/runs/p1/replay", headers=h_creator).status_code == 200
        assert client.get("/activity/runs/p1/replay", headers=H1).status_code == 200


def test_export_run_cli_writes_json_and_refuses_overwrite(tmp_path):
    db_file = tmp_path / "cli.db"
    conn = _conn(db_file)
    _rec(conn, run_id="goal", target_id=1)
    conn.close()
    artifact_path = tmp_path / "run-goal.json"

    assert ops.export_run_main([str(db_file), "goal", str(artifact_path)]) == 0
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["schema"] == run_replay.SCHEMA
    assert artifact["run_id"] == "goal"
    assert artifact["event_count"] == 1

    assert ops.export_run_main([str(db_file), "goal", str(artifact_path)]) == 1
    assert (
        ops.export_run_main(
            [str(db_file), "goal", str(artifact_path), "--overwrite"]
        )
        == 0
    )
    assert ops.export_run_main([str(db_file), "missing", str(tmp_path / "missing.json")]) == 1
