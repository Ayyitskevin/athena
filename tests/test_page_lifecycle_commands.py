"""Page create / move / version-restore are now atomic audited commands.

Each previously ran its mutation (mentor.pages) and its activity event in SEPARATE
commits, so a crash between them could create a page with no page_created event, move
one with no page_moved, or restore content with no page_restored. page_commands.create_page
/ move_page / restore_page_version fold each write and its audit into one db.transaction.
These pin the atomicity gap the migration closes: when the recorder fails, the mutation
rolls back with it — nothing half-lands.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app
from athena.mentor import page_commands, pages

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="page_lifecycle.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _space(client, key="ENG", name="Eng"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _page(client, space_id, title="Doc", body="one"):
    return client.post(
        f"/spaces/{space_id}/pages",
        json={"title": title, "body": body},
        headers=H1,
    ).json()


def _events(db_file, verb=None):
    conn = db.connect(db_file)
    rows = activity.list_activity(conn, verb=verb, limit=100)
    conn.close()
    return rows


def test_create_rolls_back_the_page_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
    conn = db.connect(db_file)
    original = page_activity.record_page_created

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_created = boom
    try:
        try:
            page_commands.create_page(
                conn, actor_id=1, space_id=sp["id"], title="Doomed", body="x"
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_created = original
    # No page landed, and no event — the insert rolled back with its failed event.
    assert pages.list_pages_in_space(conn, sp["id"]) == []
    conn.close()
    assert _events(db_file, "page_created") == []


def test_move_rolls_back_the_reparent_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        parent = _page(c, sp["id"], title="Parent")
        child = _page(c, sp["id"], title="Child")
    conn = db.connect(db_file)
    original = page_activity.record_page_moved

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_moved = boom
    try:
        try:
            page_commands.move_page(
                conn, actor_id=1, page_id=child["id"], new_parent_id=parent["id"]
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_moved = original
    # The child is still at the top level — the re-parent rolled back with its event.
    assert pages.get_page(conn, child["id"])["parent_id"] is None
    conn.close()
    assert _events(db_file, "page_moved") == []


def test_restore_rolls_back_the_content_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"], body="v1 body")
        # Edit so version 1 (the "v1 body") exists in history to restore.
        c.patch(f"/pages/{page['id']}", json={"body": "v2 body"}, headers=H1)
    conn = db.connect(db_file)
    original = page_activity.record_page_restored

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_restored = boom
    try:
        try:
            page_commands.restore_page_version(
                conn, actor_id=1, page_id=page["id"], version=1
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_restored = original
    # The live body is still v2 — the restore rolled back with its failed event, and no
    # redundant version was filed.
    assert pages.get_page(conn, page["id"])["body"] == "v2 body"
    versions = pages.list_page_versions(conn, page["id"])
    conn.close()
    assert [v["version"] for v in versions] == [
        1
    ]  # only the edit's snapshot, no restore
    assert _events(db_file, "page_restored") == []


def test_delete_rolls_back_the_page_and_dependents_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"], body="v1")
        # Give it a version (an edit) and a comment, so the delete has dependents whose
        # survival proves the whole delete rolled back — not just the page row.
        c.patch(f"/pages/{page['id']}", json={"body": "v2"}, headers=H1)
        c.post(f"/pages/{page['id']}/comments", json={"body": "hi"}, headers=H1)
    conn = db.connect(db_file)
    original = page_activity.record_page_deleted

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_deleted = boom
    try:
        try:
            page_commands.delete_page(
                conn, actor_id=1, page_id=page["id"], title=page["title"]
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_deleted = original
    # The page, its version, and its comment all survive — the delete rolled back with
    # its failed event, and no page_deleted landed.
    from athena.mentor import page_comments

    assert pages.get_page(conn, page["id"]) is not None
    assert pages.list_page_versions(conn, page["id"]) != []
    assert page_comments.list_comments(conn, page["id"]) != []
    conn.close()
    assert _events(db_file, "page_deleted") == []


def test_delete_removes_the_page_and_records_the_event(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        assert c.delete(f"/pages/{page['id']}", headers=H1).status_code == 204
        # Gone, and the deletion is on the trail (title preserved in the detail).
        assert c.get(f"/pages/{page['id']}", headers=H1).status_code == 404
    deleted = _events(db_file, "page_deleted")
    assert [(e["actor_id"], e["target_id"], e["detail"]) for e in deleted] == [
        (1, page["id"], page["title"])
    ]


def test_create_move_restore_record_attributed_events(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        parent = _page(c, sp["id"], title="Parent")
        child = _page(c, sp["id"], title="Child", body="v1")
        c.patch(f"/pages/{child['id']}", json={"body": "v2"}, headers=H1)
        c.put(
            f"/pages/{child['id']}/move",
            json={"parent_id": parent["id"]},
            headers=H1,
        )
        c.post(f"/pages/{child['id']}/versions/1/restore", headers=H1)
    # Each lifecycle verb landed, attributed to actor 1.
    assert [e["actor_id"] for e in _events(db_file, "page_created")] == [1, 1]
    assert [
        (e["actor_id"], e["target_id"]) for e in _events(db_file, "page_moved")
    ] == [(1, child["id"])]
    assert [
        (e["actor_id"], e["target_id"]) for e in _events(db_file, "page_restored")
    ] == [(1, child["id"])]
