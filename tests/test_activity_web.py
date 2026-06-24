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
