"""Project visibility and membership are audited-atomic commands.

These three access-control writes ran the mutation and the activity recorder as
SEPARATE commits — the visibility flip was THREE (the flip, the creator's roster
row when going private, then the event). A crash mid-sequence left a permanently
unaudited access-control change: a project narrowed to private with nothing on
the append-only trail saying who narrowed it, or private without its creator's
roster row. Because the mutation is idempotent and the event is recorded only on
a real change, a retry never backfills the missing event — the gap is permanent
by construction.

project_commands.set_project_visibility / add_project_member /
remove_project_member now fold each write and its audit into one immediate
transaction, mirroring the already-migrated space twins. These pin atomicity,
attribution, idempotence, and REST/browser parity.
"""

from fastapi.testclient import TestClient

from athena.aegis import project_commands
from athena.core import access, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="project_access_cmd.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _project(client, name="Proj", key="PRJ"):
    return client.post("/projects", json={"name": name, "key": key}, headers=H1).json()


def _member_user(client, email="m@e.com"):
    return client.post(
        "/users", json={"email": email, "name": "Mem", "role": "member"}, headers=H1
    ).json()


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, target_id, detail FROM activity WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _members(db_file, project_id):
    conn = db.connect(db_file)
    rows = access.list_project_members(conn, project_id)
    conn.close()
    return [r["user_id"] for r in rows]


def _visibility(db_file, project_id):
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT visibility FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    conn.close()
    return row["visibility"]


def test_going_private_records_the_flip_and_seeds_the_creator_roster(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        assert (
            c.put(
                f"/projects/{project['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )
        # Setting it to what it already is stays a no-op: no second event.
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
    assert _visibility(db_file, project["id"]) == "private"
    # The creator's roster row and the event landed with the flip, not after it.
    assert _members(db_file, project["id"]) == [1]
    assert [
        (e["actor_id"], e["target_id"])
        for e in _events(db_file, "project_made_private")
    ] == [(1, project["id"])]


def test_visibility_flip_rolls_back_whole_when_audit_fails(tmp_path):
    import athena.aegis.project_activity as project_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
    conn = db.connect(db_file)
    original = project_activity.record_project_visibility_changed

    def boom(*a, **k):
        raise RuntimeError("audit down")

    project_activity.record_project_visibility_changed = boom
    try:
        try:
            project_commands.set_project_visibility(
                conn, actor_id=1, project_id=project["id"], visibility="private"
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        project_activity.record_project_visibility_changed = original
    conn.close()
    # ALL THREE parts rolled back together: the project is still public, the
    # creator's roster row was not inserted, and no event was recorded. Before the
    # migration the flip and the roster row would have survived, permanently
    # unaudited — a retry records nothing because the flip is already applied.
    assert _visibility(db_file, project["id"]) == "public"
    assert _members(db_file, project["id"]) == []
    assert _events(db_file, "project_made_private") == []


def test_member_grant_and_revoke_are_recorded_once_each(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        member = _member_user(c)
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        assert (
            c.post(
                f"/projects/{project['id']}/members",
                json={"user_id": member["id"]},
                headers=H1,
            ).status_code
            == 201
        )
        # Idempotent re-add records nothing new.
        c.post(
            f"/projects/{project['id']}/members",
            json={"user_id": member["id"]},
            headers=H1,
        )
        assert (
            c.delete(
                f"/projects/{project['id']}/members/{member['id']}", headers=H1
            ).status_code
            == 200
        )
        # Removing a non-member is an honest 404, not a silent success.
        assert (
            c.delete(
                f"/projects/{project['id']}/members/{member['id']}", headers=H1
            ).status_code
            == 404
        )
    added = _events(db_file, "project_member_added")
    removed = _events(db_file, "project_member_removed")
    assert [(e["actor_id"], e["target_id"], e["detail"]) for e in added] == [
        (1, project["id"], "Mem")
    ]
    assert [(e["actor_id"], e["target_id"], e["detail"]) for e in removed] == [
        (1, project["id"], "Mem")
    ]


def test_member_grant_rolls_back_when_audit_fails(tmp_path):
    import athena.aegis.project_activity as project_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c)
        member = _member_user(c)
    conn = db.connect(db_file)
    original = project_activity.record_project_member_added

    def boom(*a, **k):
        raise RuntimeError("audit down")

    project_activity.record_project_member_added = boom
    try:
        try:
            project_commands.add_project_member(
                conn,
                actor_id=1,
                project_id=project["id"],
                user_id=member["id"],
                member_name="Mem",
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        project_activity.record_project_member_added = original
    conn.close()
    # No unaudited access grant survives the failed event.
    assert _members(db_file, project["id"]) == []
    assert _events(db_file, "project_member_added") == []


def test_browser_and_rest_reach_the_same_commands(tmp_path):
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
        member = _member_user(c)

        assert (
            c.post(
                f"/aegis/projects/{project['id']}/visibility",
                data={"visibility": "private"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            c.post(
                f"/aegis/projects/{project['id']}/members",
                data={"user_id": str(member["id"])},
                follow_redirects=False,
            ).status_code
            == 303
        )
        # A double-submit removal stays a forgiving 303 no-op in the UI...
        for _ in range(2):
            assert (
                c.post(
                    f"/aegis/projects/{project['id']}/members/{member['id']}/delete",
                    follow_redirects=False,
                ).status_code
                == 303
            )
    # ...while the trail shows exactly one of each fact, attributed to the actor.
    assert [e["target_id"] for e in _events(db_file, "project_made_private")] == [
        project["id"]
    ]
    assert [e["detail"] for e in _events(db_file, "project_member_added")] == ["Mem"]
    assert [e["detail"] for e in _events(db_file, "project_member_removed")] == ["Mem"]
    assert _members(db_file, project["id"]) == [1]
