"""The sprint lifecycle is audited through commands.

Creating a sprint, editing its name/goal/dates, starting it, completing it, and
deleting it are all durable writes that shape what the fleet is working on — and
every one of them was a bare data-layer write with NO activity event on ANY
transport. The append-only trail could not answer "who started this sprint" or
"who deleted the iteration that held last week's work", and the sprint surface was
absent from the command-migration inventory entirely.

sprint_commands now owns each write: the row change and its event commit or roll
back together, reachable identically from REST and the browser. These pin the
audit facts, attribution, atomicity, no-op silence, and transport parity.
"""

from fastapi.testclient import TestClient

from athena.aegis import sprint_commands, sprints
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="sprint_cmd.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _project(client, name="Proj", key="PRJ"):
    return client.post("/projects", json={"name": name, "key": key}, headers=H1).json()


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    sql = "SELECT actor_id, verb, target_kind, target_id, detail FROM activity"
    params: tuple = ()
    if verb is not None:
        sql += " WHERE verb = ?"
        params = (verb,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_full_lifecycle_is_recorded_and_attributed(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        sprint = c.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "Cycle 1", "goal": "ship it"},
            headers=H1,
        ).json()
        assert (
            c.patch(
                f"/sprints/{sprint['id']}", json={"name": "Cycle One"}, headers=H1
            ).status_code
            == 200
        )
        assert c.post(f"/sprints/{sprint['id']}/start", headers=H1).status_code == 200
        assert (
            c.post(f"/sprints/{sprint['id']}/complete", headers=H1).status_code == 200
        )
        assert c.delete(f"/sprints/{sprint['id']}", headers=H1).status_code == 204

    lifecycle = [
        (e["verb"], e["target_kind"], e["target_id"], e["actor_id"], e["detail"])
        for e in _events(db_file)
        if e["verb"].startswith("sprint_")
    ]
    assert lifecycle == [
        ("sprint_created", "sprint", sprint["id"], 1, "Cycle 1"),
        ("sprint_edited", "sprint", sprint["id"], 1, "Cycle 1 → Cycle One"),
        ("sprint_started", "sprint", sprint["id"], 1, "Cycle One"),
        ("sprint_completed", "sprint", sprint["id"], 1, "Cycle One"),
        # The name survives in the detail even though the row is gone.
        ("sprint_deleted", "sprint", sprint["id"], 1, "Cycle One"),
    ]


def test_edit_that_changes_nothing_records_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        sprint = c.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "Cycle 1", "goal": "ship it"},
            headers=H1,
        ).json()
        # Re-submitting the same values is a no-op, not a lifecycle moment.
        assert (
            c.patch(
                f"/sprints/{sprint['id']}",
                json={"name": "Cycle 1", "goal": "ship it"},
                headers=H1,
            ).status_code
            == 200
        )
        # A goal-only change is named without quoting the text.
        c.patch(f"/sprints/{sprint['id']}", json={"goal": "ship it twice"}, headers=H1)
    assert [e["detail"] for e in _events(db_file, "sprint_edited")] == ["goal changed"]


def test_create_rolls_back_the_row_when_audit_fails(tmp_path):
    import athena.core.activity as activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
    conn = db.connect(db_file)
    original = activity.record

    def boom(*a, **k):
        raise RuntimeError("audit down")

    activity.record = boom
    try:
        try:
            sprint_commands.create_sprint(
                conn, actor_id=1, project_id=project["id"], name="Doomed"
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        activity.record = original
    # No orphan sprint survives its failed event.
    assert sprints.list_sprints(conn, project_id=project["id"]) == []
    conn.close()
    assert _events(db_file, "sprint_created") == []


def test_delete_rolls_back_the_row_when_audit_fails(tmp_path):
    import athena.core.activity as activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        sprint = c.post(
            f"/projects/{project['id']}/sprints", json={"name": "Kept"}, headers=H1
        ).json()
    conn = db.connect(db_file)
    original = activity.record

    def boom(*a, **k):
        raise RuntimeError("audit down")

    activity.record = boom
    try:
        try:
            sprint_commands.delete_sprint(conn, actor_id=1, sprint_id=sprint["id"])
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        activity.record = original
    # The sprint is still there: a destructive write cannot outrun its trail entry.
    assert sprints.get_sprint(conn, sprint["id"]) is not None
    conn.close()
    assert _events(db_file, "sprint_deleted") == []


def test_rejected_start_records_nothing_and_keeps_one_active(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        first = c.post(
            f"/projects/{project['id']}/sprints", json={"name": "One"}, headers=H1
        ).json()
        second = c.post(
            f"/projects/{project['id']}/sprints", json={"name": "Two"}, headers=H1
        ).json()
        assert c.post(f"/sprints/{first['id']}/start", headers=H1).status_code == 200
        # One cadence at a time: the second start is refused, and refusing is not a
        # lifecycle moment.
        refused = c.post(f"/sprints/{second['id']}/start", headers=H1)
        assert refused.status_code == 409
        # Completing a sprint that never started is likewise refused.
        assert (
            c.post(f"/sprints/{second['id']}/complete", headers=H1).status_code == 409
        )
    assert [e["detail"] for e in _events(db_file, "sprint_started")] == ["One"]
    assert _events(db_file, "sprint_completed") == []


def test_browser_reaches_the_same_commands(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        r = c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        project = _project(c)
        assert (
            c.post(
                f"/aegis/projects/{project['id']}/sprints",
                data={
                    "name": "Web cycle",
                    "goal": "",
                    "start_date": "",
                    "end_date": "",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        sprint = c.get(f"/projects/{project['id']}/sprints", headers=H1).json()[0]
        assert (
            c.post(
                f"/aegis/sprints/{sprint['id']}/start", follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            c.post(
                f"/aegis/sprints/{sprint['id']}/complete", follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            c.post(
                f"/aegis/sprints/{sprint['id']}/delete", follow_redirects=False
            ).status_code
            == 303
        )
    verbs = [e["verb"] for e in _events(db_file) if e["verb"].startswith("sprint_")]
    assert verbs == [
        "sprint_created",
        "sprint_started",
        "sprint_completed",
        "sprint_deleted",
    ]
    # Attributed to the signed-in operator, not an anonymous write.
    assert {
        e["actor_id"] for e in _events(db_file) if e["verb"].startswith("sprint_")
    } == {1}
