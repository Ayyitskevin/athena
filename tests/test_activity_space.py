"""Activity trail for Mentor spaces: creating or deleting a space must leave the
SAME audit facts on the trail that page and issue events do, on both surfaces
(REST API and web forms), recorded by the mentor-owned recorder
(mentor/space_activity.py).

Spaces are the top-level container of the docs module; their lifecycle was the last
unaudited Mentor write. These encode that a space appearing or disappearing is a
recorded fact (who, the name preserved even after deletion), surfaced on the space's
own Activity section and linked from the global feed.
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


def test_api_space_create_records_activity(tmp_path):
    # WHY: a space born through the REST API must leave the first audit fact in its
    # history — "space_created", attributed to the actor, stamped with its name.
    db_file = tmp_path / "api_space_create.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client, name="Platform")
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, target_kind, detail FROM activity "
        "WHERE target_kind = 'space' AND target_id = ?",
        (space["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1
    assert row["verb"] == "space_created"
    assert row["detail"] == "Platform"


def test_api_space_delete_records_with_name_preserved(tmp_path):
    # WHY: a space_deleted row outlives the space it names — the name must be kept in
    # the detail since the space row is gone, so the feed can still say what fell.
    db_file = tmp_path / "api_space_delete.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        space = _make_space(client, name="Obsolete")
        r = client.delete(
            f"/spaces/{space['id']}", headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 204
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT verb, detail FROM activity "
        "WHERE verb = 'space_deleted' AND target_id = ?",
        (space["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["detail"] == "Obsolete"


# --- Web surface records ---------------------------------------------------


def test_web_space_create_records_and_shows_on_detail(tmp_path):
    # WHY: a space created from the browser form must record like the API, and its
    # own detail page must surface that fact in an Activity section.
    db_file = tmp_path / "web_space_create.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/mentor/spaces",
            data={"key": "ENG", "name": "Engineering"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        space_id = int(r.headers["location"].rsplit("/", 1)[-1])
        detail = client.get(f"/mentor/spaces/{space_id}")
    assert detail.status_code == 200
    assert "Activity" in detail.text
    assert "created space" in detail.text
    assert "Ann" in detail.text


def test_web_space_delete_records(tmp_path):
    # WHY: deleting a space from the browser is the audit-worthy removal of a whole
    # container — record "space_deleted" (who took it down), like the API.
    db_file = tmp_path / "web_space_delete.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client, name="Scratch")  # actor 1 == the session user
        r = client.post(
            f"/mentor/spaces/{space['id']}/delete", follow_redirects=False
        )
        assert r.status_code == 303
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, detail FROM activity "
        "WHERE verb = 'space_deleted' AND target_id = ?",
        (space["id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1
    assert row["detail"] == "Scratch"


def test_global_feed_links_space_event_to_its_space(tmp_path):
    # WHY: the global timeline spans every surface, so a space event must say which
    # space it happened to and link there — the feed is navigable across modules.
    db_file = tmp_path / "feed_space.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client, name="Linked")
        feed = client.get("/aegis/activity")
    assert feed.status_code == 200
    assert "created space" in feed.text
    assert f'href="/mentor/spaces/{space["id"]}"' in feed.text
    # The kind filter must offer "space" from real recorded data.
    assert ">space</option>" in feed.text
