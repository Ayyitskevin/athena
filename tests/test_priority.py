"""Tests for issue priority — the urgency field (low/medium/high/urgent).

Priority mirrors status: a small validated lifecycle set, defaulted on create,
editable through the API (PATCH) and the web detail page, and surfaced in the
list/board views. These tests encode the contract that matters: the default is
'medium', off-lifecycle values are rejected at the boundary (422 API / 400 web)
and never reach the column, and changing priority is a WRITE — so it obeys the
same creator-or-assignee authorization as status and assignment.
"""

from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import db
from athena.main import create_app


def _seed_two_users(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("ann@e.com", "Ann"))
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("bob@e.com", "Bob"))
    conn.commit()
    conn.close()


def _make_issue(client, actor="1", **body) -> dict:
    payload = {"title": "t", **body}
    r = client.post("/issues", json=payload, headers={"X-Athena-Actor": actor})
    assert r.status_code == 201, r.text
    return r.json()


# --- defaults + create ----------------------------------------------------


def test_priority_defaults_to_medium(tmp_path):
    # WHY: every issue must have a priority; an unspecified one is the neutral
    # middle, not NULL — matches the migration's backfill default.
    db_file = tmp_path / "a.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue = _make_issue(client, actor="1")
        assert issue["priority"] == "medium"


def test_create_honors_explicit_priority(tmp_path):
    db_file = tmp_path / "b.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue = _make_issue(client, actor="1", priority="urgent")
        assert issue["priority"] == "urgent"


def test_create_rejects_unknown_priority(tmp_path):
    # WHY: off-lifecycle input is refused at the boundary (422) and never reaches
    # the column — the Literal validates before our code runs.
    db_file = tmp_path / "c.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        r = client.post(
            "/issues",
            json={"title": "t", "priority": "yesterday"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


# --- edit via PATCH -------------------------------------------------------


def test_patch_changes_priority(tmp_path):
    db_file = tmp_path / "d.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue = _make_issue(client, actor="1")
        r = client.patch(
            f"/issues/{issue['id']}",
            json={"priority": "high"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["priority"] == "high"


def test_patch_priority_leaves_other_fields_untouched(tmp_path):
    # WHY: a partial edit must only touch what it sends. Bumping priority must not
    # disturb title/status (the exclude_unset contract).
    db_file = tmp_path / "e.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue = _make_issue(client, actor="1", status="in_progress")
        r = client.patch(
            f"/issues/{issue['id']}",
            json={"priority": "urgent"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["priority"] == "urgent"
        assert body["status"] == "in_progress"  # unchanged
        assert body["title"] == "t"  # unchanged


def test_patch_rejects_unknown_priority(tmp_path):
    db_file = tmp_path / "f.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue = _make_issue(client, actor="1")
        r = client.patch(
            f"/issues/{issue['id']}",
            json={"priority": "meh"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_patch_status_still_works_alongside_priority(tmp_path):
    # WHY (regression): generalizing update_issue to carry priority must not break
    # the status path that shares the same function.
    db_file = tmp_path / "g.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        issue = _make_issue(client, actor="1")
        r = client.patch(
            f"/issues/{issue['id']}",
            json={"status": "done"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "done"
        assert r.json()["priority"] == "medium"  # untouched


# --- authorization: priority is a write -----------------------------------


def test_bystander_cannot_change_priority_via_api(tmp_path):
    # WHY (load-bearing): priority change is a write, so it must obey the same
    # creator-or-assignee gate as status/assign. A bystander gets 403.
    db_file = tmp_path / "h.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        # third user so there's a genuine bystander
        conn = db.connect(db_file)
        conn.execute("INSERT INTO users (email, name) VALUES ('cy@e.com', 'Cy')")
        conn.commit()
        conn.close()
        issue = _make_issue(client, actor="1")
        r = client.patch(
            f"/issues/{issue['id']}",
            json={"priority": "urgent"},
            headers={"X-Athena-Actor": "3"},
        )
        assert r.status_code == 403


# --- web detail page ------------------------------------------------------


def _login(client, email, name):
    # User creation is authenticated after the first (bootstrap) user; act as
    # user 1, who always exists once the first _login has run.
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_creator_can_change_priority(tmp_path):
    db_file = tmp_path / "i.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator (current session)
        issue = _make_issue(client, actor="1")
        r = client.post(
            f"/aegis/issues/{issue['id']}/priority",
            data={"priority": "high"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert client.get("/issues").json()[0]["priority"] == "high"


def test_web_bystander_cannot_change_priority(tmp_path):
    db_file = tmp_path / "j.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator
        _login(client, "bob@e.com", "Bob")  # user 2 — bystander (current session)
        issue = _make_issue(client, actor="1")
        r = client.post(
            f"/aegis/issues/{issue['id']}/priority", data={"priority": "urgent"}
        )
        assert r.status_code == 403
        assert client.get("/issues").json()[0]["priority"] == "medium"  # unchanged


def test_web_detail_shows_priority(tmp_path):
    db_file = tmp_path / "k.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        issue = _make_issue(client, actor="1", priority="urgent")
        page = client.get(f"/aegis/issues/{issue['id']}").text
        assert "urgent" in page
        assert 'action="/aegis/issues/%d/priority"' % issue["id"] in page


# --- unit: PRIORITIES is the canonical set --------------------------------


def test_priorities_constant_shape():
    # WHY: the web templates hardcode these four in order; if the set changes, the
    # forms and this guard should change together.
    assert issues.PRIORITIES == ("low", "medium", "high", "urgent")
