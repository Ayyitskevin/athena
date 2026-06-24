"""Activity trail for Mentor pages: create / edit / delete a page must leave the
SAME audit facts on the trail that issue events do, on both surfaces (REST API and
web forms), recorded by the mentor-owned recorder (mentor/page_activity.py).

These encode that the docs module is audited too — who created/edited/deleted a
page, surfaced on the page's own Activity section and linked from the global feed —
and that the "record only on real change" rule holds (a save that changes nothing
writes no row), mirroring the issue trail.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def _seed_user(db_file, email="ann@e.com", name="Ann"):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _make_space(client, key="ENG", name="Engineering", actor="1") -> dict:
    r = client.post(
        "/spaces", json={"key": key, "name": name}, headers={"X-Athena-Actor": actor}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_page(client, space_id, actor="1", **body) -> dict:
    r = client.post(
        f"/spaces/{space_id}/pages",
        json={"title": "Home", **body},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email="ann@e.com", name="Ann"):
    """Bootstrap-first-user + browser login (cookie + CSRF header for write forms)."""
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- REST API records ------------------------------------------------------


def test_api_page_create_records_activity(tmp_path):
    # WHY: a page born through the REST API must leave the first audit fact in its
    # history — "created page", attributed to the actor, stamped with the title.
    db_file = tmp_path / "api_create.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Runbook")
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, target_kind, detail FROM activity "
        "WHERE target_kind = 'page' AND target_id = ?",
        (page["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1
    assert row["verb"] == "page_created"
    assert row["detail"] == "Runbook"


def test_api_page_edit_records_and_noop_is_silent(tmp_path):
    # WHY: a real edit records "page_edited"; a save that changes neither title nor
    # body writes nothing — the trail reflects change, not requests.
    db_file = tmp_path / "api_edit.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Draft", body="one")
        # Real change.
        client.patch(
            f"/pages/{page['id']}",
            json={"body": "two"},
            headers={"X-Athena-Actor": "1"},
        )
        # No-op: same title, same body.
        client.patch(
            f"/pages/{page['id']}",
            json={"title": "Draft", "body": "two"},
            headers={"X-Athena-Actor": "1"},
        )
    conn = db.connect(db_file)
    edits = conn.execute(
        "SELECT COUNT(*) AS n FROM activity "
        "WHERE verb = 'page_edited' AND target_id = ?",
        (page["id"],),
    ).fetchone()
    conn.close()
    assert edits["n"] == 1  # the no-op wrote nothing


def test_api_page_delete_records_with_title_preserved(tmp_path):
    # WHY: a page_deleted row outlives the page it names — the title must be kept in
    # the detail since the page row is gone, so the feed can still say what fell.
    db_file = tmp_path / "api_delete.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Obsolete")
        r = client.delete(
            f"/pages/{page['id']}", headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 204
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT verb, detail FROM activity "
        "WHERE verb = 'page_deleted' AND target_id = ?",
        (page["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["detail"] == "Obsolete"


# --- Web surface records ---------------------------------------------------


def test_web_page_create_records_and_shows_on_detail(tmp_path):
    # WHY: a page created from the browser form must record like the API, and its
    # own detail page must surface that fact in an Activity section.
    db_file = tmp_path / "web_create.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        r = client.post(
            f"/mentor/spaces/{space['id']}/pages",
            data={"title": "Browser Page", "body": "hi"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page_id = int(r.headers["location"].rsplit("/", 1)[-1])
        detail = client.get(f"/mentor/pages/{page_id}")
    assert detail.status_code == 200
    assert "Activity" in detail.text
    assert "created page" in detail.text
    assert "Ann" in detail.text


def test_web_page_edit_records_attributed_to_session_user(tmp_path):
    # WHY: editing from the detail-page form calls the data layer directly; it must
    # record "page_edited" stamped with the logged-in user, not a default.
    db_file = tmp_path / "web_edit.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Spec")
        r = client.post(
            f"/mentor/pages/{page['id']}/edit",
            data={"title": "Spec", "body": "now with content"},
            follow_redirects=False,
        )
        assert r.status_code == 303
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb FROM activity "
        "WHERE verb = 'page_edited' AND target_id = ?",
        (page["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1


def test_web_page_delete_records(tmp_path):
    # WHY: deleting a page from the browser is the audit-worthy removal — record
    # "page_deleted" (who took the document down), like the API.
    db_file = tmp_path / "web_delete.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Scratch")
        r = client.post(
            f"/mentor/pages/{page['id']}/delete", follow_redirects=False
        )
        assert r.status_code == 303
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, detail FROM activity "
        "WHERE verb = 'page_deleted' AND target_id = ?",
        (page["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1
    assert row["detail"] == "Scratch"


def test_api_page_move_records_with_parent_name(tmp_path):
    # WHY: re-parenting is a structural change to the page; it must record
    # "page_moved" naming the new parent so the feed says where the page landed.
    db_file = tmp_path / "api_move.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client)
        parent = _make_page(client, space["id"], title="Parent")
        child = _make_page(client, space["id"], title="Child")
        r = client.put(
            f"/pages/{child['id']}/move",
            json={"parent_id": parent["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, detail FROM activity "
        "WHERE verb = 'page_moved' AND target_id = ?",
        (child["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1
    assert row["detail"] == "Parent"


def test_api_page_move_to_top_level_records_empty_detail(tmp_path):
    # WHY: moving a nested page back to the root is still a recorded move, with an
    # empty detail the feed renders as "to the top level" — no parent to name.
    db_file = tmp_path / "api_move_top.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client)
        parent = _make_page(client, space["id"], title="Parent")
        child = _make_page(
            client, space["id"], title="Child", parent_id=parent["id"]
        )
        r = client.put(
            f"/pages/{child['id']}/move",
            json={"parent_id": None},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT verb, detail FROM activity "
        "WHERE verb = 'page_moved' AND target_id = ?",
        (child["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["detail"] == ""


def test_api_page_move_noop_is_silent(tmp_path):
    # WHY: re-submitting the same parent is not a move — the trail reflects real
    # structural change, not requests (set_parent matches the row regardless).
    db_file = tmp_path / "api_move_noop.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client)
        parent = _make_page(client, space["id"], title="Parent")
        child = _make_page(
            client, space["id"], title="Child", parent_id=parent["id"]
        )
        # Move under the SAME parent it already has.
        r = client.put(
            f"/pages/{child['id']}/move",
            json={"parent_id": parent["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
    conn = db.connect(db_file)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM activity "
        "WHERE verb = 'page_moved' AND target_id = ?",
        (child["id"],),
    ).fetchone()["n"]
    conn.close()
    assert n == 0


def test_api_restore_records_and_identical_restore_is_silent(tmp_path):
    # WHY: rolling back to a prior revision records "page_restored vN" (its own verb,
    # not a generic edit); restoring content identical to the live row writes nothing
    # — restore_version never files a redundant version, so neither do we.
    db_file = tmp_path / "api_restore.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Doc", body="v1 body")
        # Edit so v1 is snapshotted into history.
        client.patch(
            f"/pages/{page['id']}",
            json={"body": "v2 body"},
            headers={"X-Athena-Actor": "1"},
        )
        # Restore v1 (a real content change back to "v1 body").
        r = client.post(
            f"/pages/{page['id']}/versions/1/restore",
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        # Live content now equals v1 again; restoring v1 a second time is identical
        # to the live row -> no-op, writes no second page_restored row.
        client.post(
            f"/pages/{page['id']}/versions/1/restore",
            headers={"X-Athena-Actor": "1"},
        )
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT detail FROM activity "
        "WHERE verb = 'page_restored' AND target_id = ?",
        (page["id"],),
    ).fetchall()
    conn.close()
    assert [r["detail"] for r in rows] == ["v1"]


def test_web_page_move_records(tmp_path):
    # WHY: moving from the detail-page form must record like the API, stamped with
    # the session user.
    db_file = tmp_path / "web_move.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        parent = _make_page(client, space["id"], title="Parent")
        child = _make_page(client, space["id"], title="Child")
        r = client.post(
            f"/mentor/pages/{child['id']}/move",
            data={"parent_id": str(parent["id"])},
            follow_redirects=False,
        )
        assert r.status_code == 303
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, detail FROM activity "
        "WHERE verb = 'page_moved' AND target_id = ?",
        (child["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1
    assert row["detail"] == "Parent"


def test_web_restore_records(tmp_path):
    # WHY: restoring from the History form must record "page_restored" stamped with
    # the session user, like the API.
    db_file = tmp_path / "web_restore.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Doc", body="orig")
        client.post(
            f"/mentor/pages/{page['id']}/edit",
            data={"title": "Doc", "body": "changed"},
            follow_redirects=False,
        )
        r = client.post(
            f"/mentor/pages/{page['id']}/versions/1/restore",
            follow_redirects=False,
        )
        assert r.status_code == 303
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, detail FROM activity "
        "WHERE verb = 'page_restored' AND target_id = ?",
        (page["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1
    assert row["detail"] == "v1"


def test_global_feed_links_page_event_to_its_page(tmp_path):
    # WHY: the global timeline spans every surface, so a page event must say which
    # page it happened to and link there — the feed is navigable across modules.
    db_file = tmp_path / "feed_page.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        page = _make_page(client, space["id"], title="Linked")
        feed = client.get("/aegis/activity")
    assert feed.status_code == 200
    assert "created page" in feed.text
    assert f'href="/mentor/pages/{page["id"]}"' in feed.text
