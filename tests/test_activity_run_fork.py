"""Run forking: branch a tagged run from a specific parent event.

The append-only activity log remains the source of truth. A fork is not a stored run
row; it begins when a client writes child-run events with the contract headers
returned by GET /activity/runs/{run_id}/fork.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db, run_context
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _user(conn, name="Human"):
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (f"{name}@e.com", name))
    conn.commit()


def test_normalize_fork_event_id():
    assert run_context.normalize_event_id(None) is None
    assert run_context.normalize_event_id("   ") is None
    assert run_context.normalize_event_id("17") == 17
    assert run_context.normalize_event_id(18) == 18
    assert run_context.normalize_event_id("0") is None
    assert run_context.normalize_event_id("-2") is None
    assert run_context.normalize_event_id("not-an-id") is None


def test_record_stamps_fork_point_within_scope(tmp_path):
    conn = _migrated_conn(tmp_path / "stamp.db")
    _user(conn)
    run_token = run_context.set_run_id("child")
    parent_token = run_context.set_parent_run_id("parent")
    fork_token = run_context.set_forked_from_event_id("12")
    try:
        tagged = activity.record(
            conn, actor_id=1, verb="created", target_kind="issue", target_id=1
        )
    finally:
        run_context.reset_forked_from_event_id(fork_token)
        run_context.reset_parent_run_id(parent_token)
        run_context.reset_run_id(run_token)

    assert tagged["run_id"] == "child"
    assert tagged["parent_run_id"] == "parent"
    assert tagged["forked_from_event_id"] == 12

    untagged = activity.record(
        conn, actor_id=1, verb="commented", target_kind="issue", target_id=1
    )
    assert untagged["forked_from_event_id"] is None
    conn.close()


def test_run_fork_contract_stamps_child_run_and_lineage(tmp_path):
    # WHY: forking must be a deterministic log contract, not hidden mutable run state.
    # The endpoint validates the visible parent event, returns headers, and later
    # writes with those headers appear in both /events and run lineage.
    with TestClient(create_app(tmp_path / "fork.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        parent_headers = {**H1, "X-Athena-Run": "goal"}
        first = client.post(
            "/issues", json={"title": "shared prefix"}, headers=parent_headers
        ).json()
        client.post("/issues", json={"title": "parent after fork"}, headers=parent_headers)

        parent_events = client.get("/events?run_id=goal", headers=H1).json()["events"]
        fork_from = parent_events[0]

        contract_response = client.get(
            f"/activity/runs/goal/fork?from_event_id={fork_from['id']}"
            "&fork_run_id=goal:alt",
            headers=H1,
        )
        assert contract_response.status_code == 200
        contract = contract_response.json()

        assert contract["parent_run_id"] == "goal"
        assert contract["fork_run_id"] == "goal:alt"
        assert contract["fork_from_event_id"] == fork_from["id"]
        assert contract["fork_from_event"]["target_id"] == first["id"]
        assert contract["shared_prefix_partial"] is False
        assert [event["id"] for event in contract["shared_prefix_events"]] == [
            fork_from["id"]
        ]
        assert contract["headers"] == {
            "X-Athena-Run": "goal:alt",
            "X-Athena-Parent-Run": "goal",
            "X-Athena-Fork-From-Event": str(fork_from["id"]),
        }

        child_headers = {**H1, **contract["headers"]}
        client.post("/issues", json={"title": "child branch"}, headers=child_headers)

        child_events = client.get("/events?run_id=goal:alt", headers=H1).json()["events"]
        assert len(child_events) == 1
        assert child_events[0]["parent_run_id"] == "goal"
        assert child_events[0]["forked_from_event_id"] == fork_from["id"]

        lineage = client.get("/activity/runs/goal/lineage", headers=H1).json()
        child_node = lineage["descendants"][0]
        assert child_node["run_id"] == "goal:alt"
        assert child_node["parent_run_id"] == "goal"
        assert child_node["forked_from_event_id"] == fork_from["id"]


def test_run_fork_contract_rejects_unknown_or_invalid_fork_point(tmp_path):
    with TestClient(create_app(tmp_path / "reject.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        client.post("/issues", json={"title": "parent"}, headers={**H1, "X-Athena-Run": "p"})
        client.post("/issues", json={"title": "other"}, headers={**H1, "X-Athena-Run": "q"})
        other_event = client.get("/events?run_id=q", headers=H1).json()["events"][0]

        missing = client.get(
            f"/activity/runs/p/fork?from_event_id={other_event['id']}&fork_run_id=p:alt",
            headers=H1,
        )
        assert missing.status_code == 404

        same_id = client.get(
            f"/activity/runs/p/fork?from_event_id={other_event['id']}&fork_run_id=p",
            headers=H1,
        )
        assert same_id.status_code == 422
