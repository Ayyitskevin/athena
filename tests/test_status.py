"""Tests for the issue status lifecycle: PATCH on the API, the Change Status
control on the web, and status honored at create time.

These encode the contract, not just HTTP shapes: only the lifecycle statuses
(open/in_progress/done) are accepted, a status change requires authentication,
moving a missing issue is a 404 (not a silent success), and the web write path
obeys the same actor rule as the API — logged-out callers cannot change status.
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


# --- REST API -------------------------------------------------------------


def test_patch_moves_issue_through_lifecycle(tmp_path):
    # WHY: an issue tracker is useless if issues can't move. A valid PATCH
    # returns the issue at its new status.
    db_file = tmp_path / "patch.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)

        r = client.patch(
            f"/issues/{issue_id}",
            json={"status": "in_progress"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"

        # And it persists — a fresh GET sees the new status.
        assert client.get("/issues").json()[0]["status"] == "in_progress"


def test_patch_rejects_status_outside_lifecycle(tmp_path):
    # WHY: status is a closed set. Anything else is a 422 at the boundary and
    # never reaches the DB — the column does not become a free-text dumping ground.
    db_file = tmp_path / "bad_status.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.patch(
            f"/issues/{issue_id}",
            json={"status": "wontfix"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_patch_unknown_issue_is_404(tmp_path):
    # WHY: moving an issue that doesn't exist must fail loudly, not pretend to
    # succeed against zero rows.
    db_file = tmp_path / "missing.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.patch(
            "/issues/999", json={"status": "done"}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 404


def test_patch_requires_authentication(tmp_path):
    # WHY: changing state is a write, and writes need an actor — same rule as create.
    db_file = tmp_path / "noauth.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.patch(f"/issues/{issue_id}", json={"status": "done"})
        assert r.status_code == 401


def test_create_honors_initial_status(tmp_path):
    # WHY: the create form offers an Initial Status — picking "in_progress" must
    # actually create the issue there, not silently fall back to open.
    db_file = tmp_path / "create_status.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post(
            "/issues",
            json={"title": "already started", "status": "in_progress"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 201
        assert r.json()["status"] == "in_progress"


def test_create_rejects_status_outside_lifecycle(tmp_path):
    db_file = tmp_path / "create_bad.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post(
            "/issues",
            json={"title": "x", "status": "nope"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


# --- Web (browser session) ------------------------------------------------


def _login(client, db_file):
    """Create a user with a password and log the browser in (sets the cookie)."""
    client.post(
        "/users",
        json={"email": "kevin@example.com", "name": "Kevin", "password": "secret"},
    )
    client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_change_status_is_gated_on_login(tmp_path):
    # WHY: the detail-page control is a write path. Logged out it is refused with
    # a sign-in prompt; logged in it moves the issue and bounces back to it.
    db_file = tmp_path / "web_status.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, db_file)
        issue_id = _make_issue(client)  # actor "1" is the user we just made

        # Drop the session cookie → logged-out attempt is refused.
        client.cookies.clear()
        logged_out = client.post(
            f"/aegis/issues/{issue_id}/status", data={"status": "done"}
        )
        assert logged_out.status_code == 401
        assert "sign in" in logged_out.text.lower()

        # Log back in → the change goes through (303 back to the issue).
        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        ok = client.post(
            f"/aegis/issues/{issue_id}/status",
            data={"status": "done"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert ok.headers["location"] == f"/aegis/issues/{issue_id}"

        c = db.connect(db_file)
        row = c.execute(
            "SELECT status FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        c.close()
        assert row["status"] == "done"


def test_web_change_status_rejects_unknown_status(tmp_path):
    # WHY: a crafted POST outside the lifecycle is a 400, not a DB write.
    db_file = tmp_path / "web_bad.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, db_file)
        issue_id = _make_issue(client)
        r = client.post(f"/aegis/issues/{issue_id}/status", data={"status": "banana"})
        assert r.status_code == 400
