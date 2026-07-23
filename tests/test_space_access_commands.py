"""Space visibility + membership are now atomic audited commands — the FINAL piece
of the Mentor command migration.

The visibility flip is the trickiest: going private both stamps spaces.visibility AND
inserts the creator into space_members, then records the event — three writes that ran
as SEPARATE commits, so a crash could leave a space private with no creator membership,
or flipped with no event. space_commands.set_space_visibility / add_space_member /
remove_space_member fold each into one db.transaction. These pin that atomicity: when
the recorder fails, EVERY write in the command rolls back with it — a space-access
control change never half-lands.
"""

from fastapi.testclient import TestClient

from athena.core import access, activity, db
from athena.main import create_app
from athena.mentor import space_commands, spaces

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="space_access.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _member(client, email="m@e.com", name="Mem"):
    return client.post(
        "/users", json={"email": email, "name": name, "role": "member"}, headers=H1
    ).json()


def _space(client, key="ENG", name="Eng"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=100)
    conn.close()
    return rows


def test_going_private_rolls_back_flip_and_member_add_when_audit_fails(tmp_path):
    import athena.mentor.space_activity as space_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)  # created by user 1, starts public
    conn = db.connect(db_file)
    original = space_activity.record_space_visibility_changed

    def boom(*a, **k):
        raise RuntimeError("audit down")

    space_activity.record_space_visibility_changed = boom
    try:
        try:
            space_commands.set_space_visibility(
                conn, actor_id=1, space_id=sp["id"], visibility="private"
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        space_activity.record_space_visibility_changed = original
    # The space is still public AND the creator was NOT added as a member — the whole
    # three-write command (flip + member add + event) rolled back together.
    assert spaces.get_space(conn, sp["id"])["visibility"] == "public"
    assert access.is_space_member(conn, sp["id"], 1) is False
    conn.close()
    assert _events(db_file, "space_made_private") == []


def test_member_add_rolls_back_when_audit_fails(tmp_path):
    import athena.mentor.space_activity as space_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = _member(c)
        sp = _space(c)
    conn = db.connect(db_file)
    original = space_activity.record_space_member_added

    def boom(*a, **k):
        raise RuntimeError("audit down")

    space_activity.record_space_member_added = boom
    try:
        try:
            space_commands.add_space_member(
                conn,
                actor_id=1,
                space_id=sp["id"],
                user_id=member["id"],
                member_name=member["name"],
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        space_activity.record_space_member_added = original
    assert access.is_space_member(conn, sp["id"], member["id"]) is False
    conn.close()
    assert _events(db_file, "space_member_added") == []


def test_member_remove_rolls_back_when_audit_fails(tmp_path):
    import athena.mentor.space_activity as space_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = _member(c)
        sp = _space(c)
        # Make it private and add the member, so there's a membership to remove.
        c.put(
            f"/spaces/{sp['id']}/visibility", json={"visibility": "private"}, headers=H1
        )
        c.post(
            f"/spaces/{sp['id']}/members", json={"user_id": member["id"]}, headers=H1
        )
    conn = db.connect(db_file)
    assert access.is_space_member(conn, sp["id"], member["id"]) is True
    original = space_activity.record_space_member_removed

    def boom(*a, **k):
        raise RuntimeError("audit down")

    space_activity.record_space_member_removed = boom
    try:
        try:
            space_commands.remove_space_member(
                conn,
                actor_id=1,
                space_id=sp["id"],
                user_id=member["id"],
                member_name=member["name"],
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        space_activity.record_space_member_removed = original
    # The membership survives — the removal rolled back with its failed event.
    assert access.is_space_member(conn, sp["id"], member["id"]) is True
    conn.close()
    assert _events(db_file, "space_member_removed") == []


def test_visibility_and_membership_record_attributed_events(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = _member(c)
        sp = _space(c)
        # Private (adds creator as member, records made_private) → add member → remove
        # member → public.
        c.put(
            f"/spaces/{sp['id']}/visibility", json={"visibility": "private"}, headers=H1
        )
        c.post(
            f"/spaces/{sp['id']}/members", json={"user_id": member["id"]}, headers=H1
        )
        c.delete(f"/spaces/{sp['id']}/members/{member['id']}", headers=H1)
        c.put(
            f"/spaces/{sp['id']}/visibility", json={"visibility": "public"}, headers=H1
        )
    assert [e["target_id"] for e in _events(db_file, "space_made_private")] == [
        sp["id"]
    ]
    assert [e["target_id"] for e in _events(db_file, "space_made_public")] == [sp["id"]]
    assert [
        (e["actor_id"], e["detail"]) for e in _events(db_file, "space_member_added")
    ] == [(1, member["name"])]
    assert [
        (e["actor_id"], e["detail"]) for e in _events(db_file, "space_member_removed")
    ] == [(1, member["name"])]
    # Going private added the creator (user 1) as an explicit member atomically.
    conn = db.connect(db_file)
    assert access.is_space_member(conn, sp["id"], 1) is True
    conn.close()
