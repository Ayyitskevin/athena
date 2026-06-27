"""Run lineage — linking a child run to the run that spawned it.

run_id (#98) says which run an event belongs to; this adds its PARENT. When one run
delegates work and a second run carries it out, the child tags its requests with
X-Athena-Parent-Run = the parent's id, and every event it records is stamped with it.
A run thus becomes a node in a tree, reconstructed by reading (run_id, parent_run_id)
off the trail — no stored "run" row. These tests pin: record() stamps the ambient
parent, an HTTP child run carries its parent through to its events and reconstructed
run, lineage walks downward by parent_run_id on both feeds, and a top-level/untagged
run has no parent (backward-compatible).
"""
from fastapi.testclient import TestClient

from athena.core import activity, db, run_context
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _two_users(client):
    client.post("/users", json={"email": "a@e.com", "name": "Orchestrator"})
    client.post("/users", json={"email": "b@e.com", "name": "SubAgent"}, headers=H1)


def _delegated_epic(client) -> int:
    """Parent run A1: the orchestrator opens an epic and DELEGATES it to the
    sub-agent by assigning it — both part of A1's work. Returns the issue id, now
    workable by actor 2 (the assignee). This is the delegation the lineage models:
    A1 hands work to a child run."""
    iss = client.post(
        "/issues", json={"title": "epic"}, headers={**H1, "X-Athena-Run": "A1"}
    ).json()
    client.put(
        f"/issues/{iss['id']}/assignee",
        json={"assignee_id": 2},
        headers={**H1, "X-Athena-Run": "A1"},
    )
    return iss["id"]


# --- data layer: record() stamps the ambient parent -------------------------


def test_record_stamps_parent_run_id_within_scope(tmp_path):
    conn = _migrated_conn(tmp_path / "stamp.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    rt = run_context.set_run_id("child")
    pt = run_context.set_parent_run_id("parent")
    try:
        nested = activity.record(
            conn, actor_id=1, verb="created", target_kind="issue", target_id=1
        )
    finally:
        run_context.reset_parent_run_id(pt)
        run_context.reset_run_id(rt)
    assert nested["run_id"] == "child"
    assert nested["parent_run_id"] == "parent"
    # Top-level once reset — no parent bleeds through.
    top = activity.record(
        conn, actor_id=1, verb="commented", target_kind="issue", target_id=1
    )
    assert top["parent_run_id"] is None
    conn.close()


# --- HTTP: a child run carries its parent + lineage walk ---------------------


def test_child_run_tags_parent_and_walks_lineage(tmp_path):
    with TestClient(create_app(tmp_path / "lineage.db")) as client:
        _two_users(client)
        iid = _delegated_epic(client)
        # Child run B1 (parent A1): the sub-agent works it, two distinct moves.
        child = {**H2, "X-Athena-Run": "B1", "X-Athena-Parent-Run": "A1"}
        client.patch(f"/issues/{iid}", json={"status": "in_progress"}, headers=child)
        client.patch(f"/issues/{iid}", json={"status": "done"}, headers=child)

        # Walk lineage downward: the children of A1 are exactly B1's events.
        children = client.get("/activity?parent_run_id=A1", headers=H1).json()
        assert children
        assert all(e["parent_run_id"] == "A1" for e in children)
        assert all(e["run_id"] == "B1" for e in children)

        # The parent run's own events (created + assigned) are top-level (no parent).
        a1 = client.get("/activity?run_id=A1", headers=H1).json()
        assert a1 and all(e["parent_run_id"] is None for e in a1)


def test_reconstructed_child_run_reports_its_parent(tmp_path):
    with TestClient(create_app(tmp_path / "runs.db")) as client:
        _two_users(client)
        iid = _delegated_epic(client)
        child = {**H2, "X-Athena-Run": "B1", "X-Athena-Parent-Run": "A1"}
        client.patch(f"/issues/{iid}", json={"status": "done"}, headers=child)

        # The sub-agent's run knows its parent; the orchestrator's run is a root.
        b = client.get("/activity/runs?actor_id=2", headers=H1).json()
        assert len(b) == 1 and b[0]["run_id"] == "B1" and b[0]["parent_run_id"] == "A1"
        a = client.get("/activity/runs?actor_id=1", headers=H1).json()
        assert a[0]["run_id"] == "A1" and a[0]["parent_run_id"] is None


def test_events_filter_by_parent_run(tmp_path):
    with TestClient(create_app(tmp_path / "events.db")) as client:
        _two_users(client)
        iid = _delegated_epic(client)
        child = {**H2, "X-Athena-Run": "B1", "X-Athena-Parent-Run": "A1"}
        client.patch(f"/issues/{iid}", json={"status": "done"}, headers=child)

        ev = client.get("/events?parent_run_id=A1", headers=H1).json()["events"]
        assert ev and all(e["parent_run_id"] == "A1" and e["run_id"] == "B1" for e in ev)


def test_untagged_action_has_no_parent(tmp_path):
    # Backward-compat: a plain request (no headers) records NULL run + parent.
    with TestClient(create_app(tmp_path / "plain.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        client.post("/issues", json={"title": "plain"}, headers=H1)
        feed = client.get("/activity", headers=H1).json()
        assert feed and all(
            e["run_id"] is None and e["parent_run_id"] is None for e in feed
        )
