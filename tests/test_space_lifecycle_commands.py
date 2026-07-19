"""Space create / edit / hard-delete are now atomic audited commands.

Each previously ran its mutation (mentor.spaces) and its activity event in SEPARATE
commits, so a crash between them could create a space with no space_created event, or
delete one with no space_deleted. space_commands.create_space / edit_space /
delete_space fold each write and its audit into one db.transaction. These pin the
atomicity the migration closes: when the recorder fails, the mutation rolls back with
it — nothing half-lands — plus create/edit/delete record attributed events end to end.
"""
from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app
from athena.mentor import space_commands, spaces

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="space_lifecycle.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _space(client, key="ENG", name="Eng"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=100)
    conn.close()
    return rows


def test_create_rolls_back_the_space_when_audit_fails(tmp_path):
    import athena.mentor.space_activity as space_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    original = space_activity.record_space_created

    def boom(*a, **k):
        raise RuntimeError("audit down")

    space_activity.record_space_created = boom
    try:
        try:
            space_commands.create_space(
                conn, actor_id=1, key="ENG", name="Eng"
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        space_activity.record_space_created = original
    # No space landed, and no event — the insert rolled back with its failed event.
    assert spaces.get_space_by_key(conn, "ENG") is None
    conn.close()
    assert _events(db_file, "space_created") == []


def test_edit_rolls_back_the_space_when_audit_fails(tmp_path):
    import athena.mentor.space_activity as space_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
    conn = db.connect(db_file)
    original = space_activity.record_space_edited

    def boom(*a, **k):
        raise RuntimeError("audit down")

    space_activity.record_space_edited = boom
    try:
        try:
            space_commands.edit_space(
                conn, actor_id=1, space_id=sp["id"], name="Renamed"
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        space_activity.record_space_edited = original
    # The name is unchanged — the edit rolled back with its failed event.
    assert spaces.get_space(conn, sp["id"])["name"] == "Eng"
    conn.close()
    assert _events(db_file, "space_edited") == []


def test_delete_rolls_back_the_space_when_audit_fails(tmp_path):
    import athena.mentor.space_activity as space_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
    conn = db.connect(db_file)
    original = space_activity.record_space_deleted

    def boom(*a, **k):
        raise RuntimeError("audit down")

    space_activity.record_space_deleted = boom
    try:
        try:
            space_commands.delete_space(
                conn, actor_id=1, space_id=sp["id"], name=sp["name"]
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        space_activity.record_space_deleted = original
    # The space survives — the delete rolled back with its failed event.
    assert spaces.get_space(conn, sp["id"]) is not None
    conn.close()
    assert _events(db_file, "space_deleted") == []


def test_create_edit_delete_record_attributed_events(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        assert (
            c.patch(
                f"/spaces/{sp['id']}", json={"name": "Engineering"}, headers=H1
            ).status_code
            == 200
        )
        assert c.delete(f"/spaces/{sp['id']}", headers=H1).status_code == 204
    assert [(e["actor_id"], e["target_id"]) for e in _events(db_file, "space_created")] == [
        (1, sp["id"])
    ]
    assert [(e["actor_id"], e["target_id"]) for e in _events(db_file, "space_edited")] == [
        (1, sp["id"])
    ]
    # The delete records the space's name AS OF deletion time (it was renamed first).
    assert [
        (e["actor_id"], e["target_id"], e["detail"])
        for e in _events(db_file, "space_deleted")
    ] == [(1, sp["id"], "Engineering")]
