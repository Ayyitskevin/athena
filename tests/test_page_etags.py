"""Pages get issue-grade optimistic concurrency: a strong ETag on read and an
``If-Match`` precondition on edit, evaluated inside the write lock.

Two agents editing shared memory used to silently clobber each other — page edits
were unconditional last-write-wins, and the row change and its ``page_edited`` event
committed separately (a crash between them could rewrite a body with no audit fact).
``mentor.page_commands.edit_page`` now folds the snapshot+overwrite, its event, and the
optional precondition into one transaction, reachable identically from REST, the
browser, and (via REST) MCP. These pin: the ETag round-trips, a stale tag gets a clean
412 with the current tag, malformed/absent conditions behave per RFC, the audit is
atomic with the row, and the shared MCP client reaches the guarded write.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app
from athena.mentor import page_commands
from athena.mcp.client import AthenaClient, AthenaError

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="page_etag.db"):
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


def test_page_read_emits_etag_and_a_matching_edit_advances_it(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        got = c.get(f"/pages/{page['id']}", headers=H1)
        tag = got.headers["ETag"]
        assert tag and tag.startswith('"sha256-')
        # A conditional edit carrying the current tag succeeds and returns a NEW tag.
        r = c.patch(
            f"/pages/{page['id']}",
            json={"body": "two"},
            headers={**H1, "If-Match": tag},
        )
        assert r.status_code == 200
        assert r.json()["body"] == "two"
        new_tag = r.headers["ETag"]
        assert new_tag and new_tag != tag
        # The stale tag no longer matches: a second edit with it is refused.
        again = c.patch(
            f"/pages/{page['id']}",
            json={"body": "three"},
            headers={**H1, "If-Match": tag},
        )
        assert again.status_code == 412


def test_stale_if_match_is_412_with_the_current_tag_and_code(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        stale = c.get(f"/pages/{page['id']}", headers=H1).headers["ETag"]
        # Someone else edits first, moving the live tag.
        c.patch(f"/pages/{page['id']}", json={"body": "moved"}, headers=H1)
        current = c.get(f"/pages/{page['id']}", headers=H1).headers["ETag"]
        assert current != stale
        r = c.patch(
            f"/pages/{page['id']}",
            json={"body": "loser"},
            headers={**H1, "If-Match": stale},
        )
        assert r.status_code == 412
        assert r.json()["code"] == "precondition_failed"
        # The 412 echoes the live validator so the client can refetch-and-retry.
        assert r.headers["ETag"] == current
        # The losing write never landed.
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["body"] == "moved"


def test_wildcard_if_match_matches_an_existing_page(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        r = c.patch(
            f"/pages/{page['id']}",
            json={"body": "wild"},
            headers={**H1, "If-Match": "*"},
        )
        assert r.status_code == 200
        assert r.json()["body"] == "wild"


def test_malformed_if_match_is_400_invalid(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        r = c.patch(
            f"/pages/{page['id']}",
            json={"body": "x"},
            headers={**H1, "If-Match": "not-a-quoted-tag"},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_if_match"
        # A malformed precondition never mutates.
        assert c.get(f"/pages/{page['id']}", headers=H1).json()["body"] == "one"


def test_unconditional_edit_records_page_edited_once(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        # No If-Match header: still a valid edit (backward compatible), audited once.
        assert (
            c.patch(
                f"/pages/{page['id']}", json={"body": "edited"}, headers=H1
            ).status_code
            == 200
        )
    edited = _events(db_file, "page_edited")
    assert [(e["actor_id"], e["target_id"]) for e in edited] == [(1, page["id"])]


def test_edit_command_rolls_back_the_event_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
    conn = db.connect(db_file)
    original = page_activity.record_page_edited

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_edited = boom
    try:
        try:
            page_commands.edit_page(conn, actor_id=1, page_id=page["id"], body="doomed")
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_edited = original
    # The edit rolled back with its failed event: no body change, no version, no event.
    from athena.mentor import pages

    assert pages.get_page(conn, page["id"])["body"] == "one"
    assert pages.list_page_versions(conn, page["id"]) == []
    conn.close()
    assert _events(db_file, "page_edited") == []


def test_anonymous_edit_is_rejected(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        page = _page(c, sp["id"])
        # No actor header, no token — a write must be authenticated (401).
        r = c.patch(f"/pages/{page['id']}", json={"body": "anon"})
        assert r.status_code == 401


def test_mcp_client_reaches_the_guarded_page_edit(tmp_path):
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
        ath = AthenaClient(client=tc, run_id="page-run")
        # get_page surfaces the ETag as _etag; a guarded edit with it succeeds.
        fetched = ath.get_page(page["id"])
        tag = fetched["_etag"]
        updated = ath.update_page(page["id"], body="via-mcp", if_match=tag)
        assert updated["body"] == "via-mcp"
        # Reusing the now-stale tag raises a 412 carrying the current validator.
        try:
            ath.update_page(page["id"], body="stale", if_match=tag)
            raise AssertionError("expected a precondition failure")
        except AthenaError as exc:
            assert exc.status_code == 412
            assert exc.current_etag is not None and exc.current_etag != tag
    finally:
        tc.__exit__(None, None, None)
    # The write landed under the agent's run id (attributed, replayable).
    conn = db.connect(db_file)
    ev = conn.execute(
        "SELECT run_id FROM activity WHERE verb = 'page_edited'"
    ).fetchone()
    conn.close()
    assert ev["run_id"] == "page-run"
