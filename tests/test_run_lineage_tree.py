"""The run-lineage TREE — one call that reconstructs a run's whole causal tree.

The parent_run_id plumbing (test_run_lineage.py) already lets a feed filter walk ONE
level of children (/activity?parent_run_id=X). This is the aggregate the feed can't
give: activity.run_lineage / GET /activity/runs/{id}/lineage assemble, from the log
alone, the originating goal down to a run (ancestors, root-first), the run with its
events (replayable), and the full recursive tree of runs it spawned (descendants). Pure
projection of (run_id, parent_run_id) — no stored tree. These tests pin the tree shape,
the focal-vs-light event payloads, cycle-safety, and the visibility gate (a run on a
target the viewer can't see is a 404, indistinguishable from one that never existed).
"""

from fastapi.testclient import TestClient

from athena.core import activity, db, run_context
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('a@e.com', 'A', 'admin')"
    )
    conn.commit()
    return conn


def _rec(conn, *, run_id=None, parent=None, target_id=1, verb="created"):
    """Record one event stamped with run_id/parent (via the same contextvars the ASGI
    middleware sets from the run headers), so the lineage reader sees real tagged rows."""
    t1 = run_context.set_run_id(run_id)
    t2 = run_context.set_parent_run_id(parent)
    try:
        return activity.record(
            conn, actor_id=1, verb=verb, target_kind="issue", target_id=target_id
        )
    finally:
        run_context.reset_parent_run_id(t2)
        run_context.reset_run_id(t1)


def _tree(db_file):
    """goal → {child → grand, sibling}. ids ascend in creation order, so child precedes
    sibling in goal's descendants."""
    conn = _conn(db_file)
    _rec(conn, run_id="goal")
    _rec(conn, run_id="goal")
    _rec(conn, run_id="child", parent="goal")
    _rec(conn, run_id="child", parent="goal")
    _rec(conn, run_id="grand", parent="child")
    _rec(conn, run_id="sibling", parent="goal")
    return conn


def test_focal_run_carries_its_events_and_children_are_light(tmp_path):
    conn = _tree(tmp_path / "t.db")
    lin = activity.run_lineage(conn, "goal")
    assert lin["run"]["run_id"] == "goal"
    # The focal run replays its own events (oldest-first); ancestors/descendants don't.
    assert lin["run"]["event_count"] == 2 and len(lin["run"]["events"]) == 2
    assert lin["ancestors"] == []
    assert [d["run_id"] for d in lin["descendants"]] == ["child", "sibling"]
    child = lin["descendants"][0]
    assert child["events"] == [] and child["event_count"] == 2  # light, but counted
    assert [g["run_id"] for g in child["children"]] == ["grand"]
    assert lin["descendants"][1]["children"] == []  # sibling is a leaf


def test_ancestors_are_root_first_up_to_the_goal(tmp_path):
    conn = _tree(tmp_path / "a.db")
    lin = activity.run_lineage(conn, "grand")
    # The causal path reads originating-goal-first: goal → child → (grand).
    assert [a["run_id"] for a in lin["ancestors"]] == ["goal", "child"]
    assert lin["run"]["run_id"] == "grand" and lin["run"]["event_count"] == 1
    assert lin["descendants"] == []  # a leaf run spawned nothing


def test_unknown_run_is_none(tmp_path):
    conn = _tree(tmp_path / "n.db")
    assert activity.run_lineage(conn, "does-not-exist") is None


def test_a_cycle_in_parent_links_terminates(tmp_path):
    # A pathological log where two runs name each other as parent must not loop forever.
    conn = _conn(tmp_path / "cycle.db")
    _rec(conn, run_id="A", parent="B")
    _rec(conn, run_id="B", parent="A")
    lin = activity.run_lineage(conn, "A")
    # The walk stops at the first repeat; the tree is finite.
    assert lin["run"]["run_id"] == "A"
    assert all(node["run_id"] != "A" for node in lin["ancestors"])


# --- REST API ---------------------------------------------------------------


def test_lineage_endpoint_walks_the_tree(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        # One issue created under run g1, then transitioned under run c1 (child of g1).
        iid = client.post(
            "/issues", json={"title": "x"}, headers={**H_ADMIN, "X-Athena-Run": "g1"}
        ).json()["id"]
        client.patch(
            f"/issues/{iid}",
            json={"status": "in_progress"},
            headers={**H_ADMIN, "X-Athena-Run": "c1", "X-Athena-Parent-Run": "g1"},
        )

        goal = client.get("/activity/runs/g1/lineage", headers=H_ADMIN)
        assert goal.status_code == 200
        body = goal.json()
        assert body["run"]["run_id"] == "g1"
        assert [d["run_id"] for d in body["descendants"]] == ["c1"]

        child = client.get("/activity/runs/c1/lineage", headers=H_ADMIN).json()
        assert [a["run_id"] for a in child["ancestors"]] == ["g1"]

        assert (
            client.get("/activity/runs/nope/lineage", headers=H_ADMIN).status_code
            == 404
        )
        # Authentication is the gate, like the rest of the activity API.
        assert client.get("/activity/runs/g1/lineage").status_code == 401


def test_lineage_is_visibility_gated(tmp_path):
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "Admin", "password": "pw"},
            headers=H_ADMIN,
        )
        client.post(
            "/users",
            json={
                "email": "c@e.com",
                "name": "Creator",
                "password": "pw",
                "role": "member",
            },
            headers=H_ADMIN,
        )
        client.post(
            "/users",
            json={
                "email": "o@e.com",
                "name": "Outsider",
                "password": "pw",
                "role": "member",
            },
            headers=H_ADMIN,
        )
        pid = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H_CREATOR
        ).json()["id"]
        # The only event in run p1 targets an issue in P.
        client.post(
            "/issues",
            json={"title": "secret", "project_id": pid},
            headers={**H_CREATOR, "X-Athena-Run": "p1"},
        )
        client.put(
            f"/projects/{pid}/visibility",
            json={"visibility": "private"},
            headers=H_CREATOR,
        )

        # The outsider can't see the only event, so the run is a 404 — its existence
        # never leaks through lineage.
        assert (
            client.get("/activity/runs/p1/lineage", headers=H_OUTSIDER).status_code
            == 404
        )
        # The creator (a member of P) and the admin (god-view) both see it.
        assert (
            client.get("/activity/runs/p1/lineage", headers=H_CREATOR).status_code
            == 200
        )
        assert (
            client.get("/activity/runs/p1/lineage", headers=H_ADMIN).status_code == 200
        )


def test_lineage_partial_tree_gating_does_not_leak_hidden_runs(tmp_path):
    # A run whose events are all visible must not leak, through its lineage, a run in the
    # chain that the viewer CAN'T see. Tree: g (public) -> c (PRIVATE) -> gc (public).
    # An outsider sees g and gc but not c: viewing gc, the hidden parent c must truncate
    # ancestors to []; viewing g, the hidden child c must drop the whole subtree (c AND
    # its visible grandchild gc). The creator (a member of the private project) sees all.
    with TestClient(create_app(tmp_path / "partial.db")) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "Admin", "password": "pw"},
            headers=H_ADMIN,
        )
        client.post(
            "/users",
            json={
                "email": "c@e.com",
                "name": "Creator",
                "password": "pw",
                "role": "member",
            },
            headers=H_ADMIN,
        )
        client.post(
            "/users",
            json={
                "email": "o@e.com",
                "name": "Outsider",
                "password": "pw",
                "role": "member",
            },
            headers=H_ADMIN,
        )
        pub = client.post(
            "/projects", json={"name": "Pub", "key": "PUB"}, headers=H_CREATOR
        ).json()["id"]
        priv = client.post(
            "/projects", json={"name": "Priv", "key": "PRIV"}, headers=H_CREATOR
        ).json()["id"]

        # g: create issue A in the public project.
        a = client.post(
            "/issues",
            json={"title": "A", "project_id": pub},
            headers={**H_CREATOR, "X-Athena-Run": "g"},
        ).json()["id"]
        # c (child of g): create issue B in the private project.
        client.post(
            "/issues",
            json={"title": "B", "project_id": priv},
            headers={**H_CREATOR, "X-Athena-Run": "c", "X-Athena-Parent-Run": "g"},
        )
        # gc (child of c): a status change on the public issue A.
        client.patch(
            f"/issues/{a}",
            json={"status": "in_progress"},
            headers={**H_CREATOR, "X-Athena-Run": "gc", "X-Athena-Parent-Run": "c"},
        )
        client.put(
            f"/projects/{priv}/visibility",
            json={"visibility": "private"},
            headers=H_CREATOR,
        )

        # Outsider viewing gc: focal is visible (public), but the hidden parent c
        # truncates the ancestor chain — neither c nor g leaks.
        out_gc = client.get("/activity/runs/gc/lineage", headers=H_OUTSIDER)
        assert out_gc.status_code == 200
        assert out_gc.json()["ancestors"] == []
        # Outsider viewing g: focal visible, but the hidden child c (and its subtree,
        # including the otherwise-visible grandchild gc) is dropped from descendants.
        out_g = client.get("/activity/runs/g/lineage", headers=H_OUTSIDER)
        assert out_g.status_code == 200
        assert out_g.json()["descendants"] == []

        # The creator is a member of the private project, so the full tree is visible.
        cre_gc = client.get("/activity/runs/gc/lineage", headers=H_CREATOR).json()
        assert [x["run_id"] for x in cre_gc["ancestors"]] == ["g", "c"]  # root-first
        cre_g = client.get("/activity/runs/g/lineage", headers=H_CREATOR).json()
        assert [d["run_id"] for d in cre_g["descendants"]] == ["c"]
        assert [gc["run_id"] for gc in cre_g["descendants"][0]["children"]] == ["gc"]
