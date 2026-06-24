"""Tests for the web view of the activity log: the per-issue History section on
the detail page and the global /aegis/activity timeline.

These encode that the browser feed is a thin client over the SAME data the REST
feed serves — it renders the recorded facts (who/verb/detail) and, on the global
page, links each event to its issue. Reads are open, like the rest of the site.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def _seed_user(db_file, email="kevin@example.com", name="Kevin"):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _make_issue(client, title="ship it", actor="1") -> int:
    r = client.post("/issues", json={"title": title}, headers={"X-Athena-Actor": actor})
    assert r.status_code == 201
    return r.json()["id"]


def _login(client, email="kevin@example.com", name="Kevin", password="secret"):
    """Create a user with a password and log the browser in (sets the cookie +
    CSRF header the write forms require). Returns nothing — the client is now
    an authenticated browser session for that user."""
    client.post("/users", json={"email": email, "name": name, "password": password})
    client.post("/login", data={"email": email, "password": password})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_issue_detail_shows_history(tmp_path):
    # WHY: the detail page must surface the issue's own audit trail so a reader
    # sees how it got to its current state — at minimum the "created" event, and a
    # status change rendered with its "open → done" detail.
    db_file = tmp_path / "detail_history.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        client.patch(
            f"/issues/{issue_id}",
            json={"status": "done"},
            headers={"X-Athena-Actor": "1"},
        )
        page = client.get(f"/aegis/issues/{issue_id}")
    assert page.status_code == 200
    assert "History" in page.text
    assert "changed status" in page.text
    assert "open → done" in page.text
    assert "created" in page.text


def test_global_feed_links_each_event_to_its_issue(tmp_path):
    # WHY: the global timeline spans every issue, so each row must say which issue
    # it happened to and link there — otherwise the feed is unnavigable.
    db_file = tmp_path / "global_feed.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client, title="first")
        page = client.get("/aegis/activity")
    assert page.status_code == 200
    assert "Activity" in page.text
    assert "created" in page.text
    assert f'href="/aegis/issues/{issue_id}"' in page.text


def test_global_feed_empty_state(tmp_path):
    # WHY: a fresh install has no activity. The page must render an honest empty
    # state, not crash or invent rows (the cardinal rule — no faked data).
    db_file = tmp_path / "empty_feed.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        page = client.get("/aegis/activity")
    assert page.status_code == 200
    assert "No activity yet" in page.text


def test_activity_in_nav(tmp_path):
    # WHY: the feed is only useful if it's reachable; the global timeline gets a
    # top-level nav entry like Aegis/Projects/Mentor.
    db_file = tmp_path / "nav.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        page = client.get("/aegis")
    assert page.status_code == 200
    assert 'href="/aegis/activity"' in page.text


# --- The audit gap: browser writes must record too ------------------------


def test_web_create_records_activity(tmp_path):
    # WHY: Athena is dogfooded through the web UI. An issue created from the
    # browser form must leave the same trail an API-created one does — otherwise
    # the audit log silently misses every human action.
    db_file = tmp_path / "web_create.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        r = client.post("/aegis/issues", data={"title": "from the browser"})
        assert r.status_code == 200  # HTMX success fragment
        feed = client.get("/aegis/activity")
    assert "created" in feed.text
    assert "Kevin" in feed.text


def test_web_status_change_records_attributed_to_session_user(tmp_path):
    # WHY: this is the gap PR #43/#44 left. A status change via the detail-page
    # form calls the data layer directly, bypassing the REST endpoint that records.
    # The History must show the transition, stamped with the logged-in user.
    db_file = tmp_path / "web_status.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)  # actor "1" is the user we just made
        r = client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "done"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = client.get(f"/aegis/issues/{issue_id}")
    assert "changed status" in page.text
    assert "open → done" in page.text
    # Attribution must be unambiguous — assert on the recorded row, not page text
    # (the nav shows "Kevin" on every page, so a substring check would lie).
    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT actor_id, verb, detail FROM activity "
        "WHERE verb = 'changed_status' AND target_id = ?",
        (issue_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor_id"] == 1  # the logged-in session user, not a default
    assert row["detail"] == "open → done"


def test_web_assignment_records(tmp_path):
    # WHY: assigning from the browser is a tracked responsibility change too — the
    # web path must record "assigned" with the new owner's name, like the API.
    db_file = tmp_path / "web_assign.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/assignee",
            data={"assignee_id": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = client.get(f"/aegis/issues/{issue_id}")
    assert "assigned to" in page.text


def test_web_project_move_records(tmp_path):
    # WHY: moving an issue into a project from the browser is a tracked change too.
    # The web path must record "changed_project" with the project name, like the API.
    db_file = tmp_path / "web_project.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        proj = client.post(
            "/projects",
            json={"name": "Platform", "key": "PLAT"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/project",
            data={"project_id": str(proj["id"])},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = client.get(f"/aegis/issues/{issue_id}")
    assert "moved to" in page.text
    assert "Platform" in page.text


def test_web_label_add_records(tmp_path):
    # WHY: labeling from the browser is a tracked classification change — the web
    # path must record "labeled" with the label's name, like the API.
    db_file = tmp_path / "web_label.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/labels",
            data={"name": "urgent"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = client.get(f"/aegis/issues/{issue_id}")
    assert "labeled" in page.text
    assert "urgent" in page.text


def test_web_noop_status_records_nothing(tmp_path):
    # WHY: the trail reflects real change, not form submissions. Re-submitting the
    # current status from the browser must not write a spurious event — the same
    # no-op rule the API path follows.
    db_file = tmp_path / "web_noop.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "open"},  # already open
            follow_redirects=False,
        )
        page = client.get(f"/aegis/issues/{issue_id}")
    # The History holds only "created" — no spurious status row.
    assert "changed status" not in page.text
