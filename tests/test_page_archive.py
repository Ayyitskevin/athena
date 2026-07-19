"""Pages get issue-grade soft-delete: archive hides a page from the tree, navigation,
and search while preserving it (and its versions and comments), reversibly.

Deleting a page destroyed its history and comments irreversibly — the only "get it out
of the way" was the nuclear option. mentor.page_commands.set_page_archived stamps
archived_at atomically with its page_archived/page_unarchived event, under the same
visibility gate, reachable from REST, the browser, and (via REST) MCP — the Mentor twin
of issue archive. These pin the hide/restore behavior, attribution, atomicity, the
search exclusion, idempotence, and transport parity.
"""
from fastapi.testclient import TestClient

from athena.core import activity, db, search
from athena.main import create_app
from athena.mentor import page_commands
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="page_archive.db"):
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


def test_archive_hides_from_the_list_and_unarchive_restores(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        # Visible by default.
        listed = c.get(f"/spaces/{sp['id']}/pages", headers=H1).json()
        assert [p["id"] for p in listed] == [page["id"]]

        assert c.post(f"/pages/{page['id']}/archive", headers=H1).status_code == 200
        # Gone from the default list; present with include_archived; still readable by id.
        assert c.get(f"/spaces/{sp['id']}/pages", headers=H1).json() == []
        with_arch = c.get(
            f"/spaces/{sp['id']}/pages?include_archived=true", headers=H1
        ).json()
        assert [p["id"] for p in with_arch] == [page["id"]]
        fetched = c.get(f"/pages/{page['id']}", headers=H1).json()
        assert fetched["archived_at"] is not None

        assert c.post(f"/pages/{page['id']}/unarchive", headers=H1).status_code == 200
        back = c.get(f"/spaces/{sp['id']}/pages", headers=H1).json()
        assert [p["id"] for p in back] == [page["id"]]
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["archived_at"] is None


def test_archive_and_unarchive_are_recorded_and_idempotent(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        c.post(f"/pages/{page['id']}/archive", headers=H1)
        # Re-archiving an archived page records nothing new (idempotent).
        c.post(f"/pages/{page['id']}/archive", headers=H1)
        c.post(f"/pages/{page['id']}/unarchive", headers=H1)
    archived = _events(db_file, "page_archived")
    unarchived = _events(db_file, "page_unarchived")
    assert [(e["actor_id"], e["target_id"]) for e in archived] == [(1, page["id"])]
    assert [(e["actor_id"], e["target_id"]) for e in unarchived] == [(1, page["id"])]


def test_archived_page_drops_out_of_search_unless_included(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"], title="Zebra runbook", body="unique zebra content")
        c.post(f"/pages/{page['id']}/archive", headers=H1)
    conn = db.connect(db_file)
    # Default search excludes the archived page; include_archived surfaces it.
    assert search.search(conn, "zebra") == []
    with_arch = search.search(conn, "zebra", include_archived=True)
    conn.close()
    assert [(h["kind"], h["source_id"]) for h in with_arch] == [("page", page["id"])]


def test_archive_command_rolls_back_the_flip_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
    conn = db.connect(db_file)
    original = page_activity.record_page_archive_change

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_archive_change = boom
    try:
        try:
            page_commands.set_page_archived(
                conn, actor_id=1, page_id=page["id"], archived=True
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_archive_change = original
    from athena.mentor import pages

    # The flip rolled back with its failed event — still active, no event.
    assert pages.get_page(conn, page["id"])["archived_at"] is None
    conn.close()
    assert _events(db_file, "page_archived") == []


def test_anonymous_archive_is_rejected(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        assert c.post(f"/pages/{page['id']}/archive").status_code == 401


def test_mcp_client_reaches_page_archive_under_a_run(tmp_path):
    app, db_file = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        sp = _space(tc)
        page = _page(tc, sp["id"])
        raw = tc.post(
            "/tokens", json={"name": "t", "scopes": ["read", "docs:write"]}, headers=H1
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        tc.headers.pop("X-Athena-Actor", None)
        ath = AthenaClient(client=tc, run_id="archive-run")
        archived = ath.archive_page(page["id"])
        assert archived["archived_at"] is not None
        # The default page list (via MCP) hides it; include_archived surfaces it.
        assert ath.list_pages(sp["id"]) == []
        assert [p["id"] for p in ath.list_pages(sp["id"], include_archived=True)] == [
            page["id"]
        ]
        ath.unarchive_page(page["id"])
        assert [p["id"] for p in ath.list_pages(sp["id"])] == [page["id"]]
    finally:
        tc.__exit__(None, None, None)
    conn = db.connect(db_file)
    ev = conn.execute(
        "SELECT run_id FROM activity WHERE verb = 'page_archived'"
    ).fetchone()
    conn.close()
    assert ev["run_id"] == "archive-run"  # attributed to the agent's run
