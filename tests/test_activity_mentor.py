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
