"""The project lifecycle is on the trail — a container can't appear or vanish silently.

Creating a project, editing its name/key/description, or deleting it were bare
writes that recorded nothing: a workspace container could be created or destroyed
with no record of who did it. project_commands owns each write now — the row
change and its activity event land in one transaction. These pin the three verbs
(created_project / edited_project / deleted_project), their attribution and
detail, atomicity (a failed write leaves no orphan event), no-op silence on an
edit that changes nothing, and that the delete event OUTLIVES its target.
"""

from fastapi.testclient import TestClient

from athena.aegis import project_commands, projects
from athena.core import db

from support import ADMIN, bootstrap, events, make_app


def test_create_edit_delete_each_land_one_attributed_event(tmp_path):
    app, db_file = make_app(tmp_path, "proj_audit.db")
    with TestClient(app) as c:
        bootstrap(c)
        pid = c.post(
            "/projects", json={"name": "Ops", "key": "OPS"}, headers=ADMIN
        ).json()["id"]
        c.patch(f"/projects/{pid}", json={"name": "Operations"}, headers=ADMIN)
        assert c.delete(f"/projects/{pid}", headers=ADMIN).status_code == 204

    created = events(db_file, "created_project")
    edited = events(db_file, "edited_project")
    deleted = events(db_file, "deleted_project")
    assert [(e["actor_id"], e["target_id"]) for e in created] == [(1, pid)]
    assert created[0]["detail"] == "Ops (OPS)"
    assert [(e["actor_id"], e["target_id"]) for e in edited] == [(1, pid)]
    assert "Ops" in edited[0]["detail"] and "Operations" in edited[0]["detail"]
    assert [(e["actor_id"], e["target_id"]) for e in deleted] == [(1, pid)]
    # The delete event OUTLIVES the vanished project (target_id has no FK).
    assert deleted[0]["detail"] == "Operations (OPS)"


def test_create_is_atomic_no_event_when_the_insert_fails(tmp_path):
    # A duplicate key raises IntegrityError inside the command's transaction; the
    # 'created_project' event must roll back with it, leaving no orphan on the trail.
    app, db_file = make_app(tmp_path, "proj_audit.db")
    with TestClient(app) as c:
        bootstrap(c)
        assert (
            c.post(
                "/projects", json={"name": "A", "key": "DUP"}, headers=ADMIN
            ).status_code
            == 201
        )
        assert (
            c.post(
                "/projects", json={"name": "B", "key": "DUP"}, headers=ADMIN
            ).status_code
            == 409
        )
    # Exactly one created_project (the first); the refused second minted nothing.
    assert len(events(db_file, "created_project")) == 1


def test_edit_that_changes_nothing_records_no_event(tmp_path):
    app, db_file = make_app(tmp_path, "proj_audit.db")
    with TestClient(app) as c:
        bootstrap(c)
        pid = c.post(
            "/projects",
            json={"name": "Ops", "key": "OPS", "description": "d"},
            headers=ADMIN,
        ).json()["id"]
        # Re-save the exact same values — a no-op edit is not a lifecycle moment.
        assert (
            c.patch(
                f"/projects/{pid}",
                json={"name": "Ops", "key": "OPS", "description": "d"},
                headers=ADMIN,
            ).status_code
            == 200
        )
    assert events(db_file, "edited_project") == []


def test_command_rolls_back_the_event_if_the_delete_row_is_gone(tmp_path):
    # Directly at the command layer: deleting a nonexistent project returns False
    # and records nothing (the get-guard short-circuits before any write).
    app, db_file = make_app(tmp_path, "proj_audit.db")
    with TestClient(app) as c:
        bootstrap(c)
    conn = db.connect(db_file)
    assert project_commands.delete_project(conn, actor_id=1, project_id=999) is False
    conn.close()
    assert events(db_file, "deleted_project") == []


def test_delete_event_is_recorded_before_the_row_is_removed(tmp_path):
    # The event is written while the project is still live (so its visibility
    # scope resolves), then the row goes. Verify both the event AND the absence
    # of the project after one atomic command call.
    app, db_file = make_app(tmp_path, "proj_audit.db")
    with TestClient(app) as c:
        bootstrap(c)
        pid = c.post(
            "/projects", json={"name": "Gone", "key": "GONE"}, headers=ADMIN
        ).json()["id"]
    conn = db.connect(db_file)
    assert project_commands.delete_project(conn, actor_id=1, project_id=pid) is True
    assert projects.get_project(conn, pid) is None
    conn.close()
    deleted = events(db_file, "deleted_project")
    assert len(deleted) == 1 and deleted[0]["target_id"] == pid
