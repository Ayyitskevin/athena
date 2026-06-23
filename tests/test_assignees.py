"""Tests for issue assignees: the REST PUT endpoint, the detail-page assign
control, and the name resolution that lets the UI show who's on an issue.

These encode the contract, not just HTTP shapes: an issue starts unassigned,
assigning resolves the user's display name, unassigning (null) is always valid,
assigning to a user that doesn't exist is a 422 (the FK never raises a 500),
assigning on a missing issue is a 404, and every write path needs an actor.
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


def test_issue_starts_unassigned(tmp_path):
    # WHY: assignment is optional. A freshly created issue must come back with a
    # null assignee, not an accidental default owner.
    db_file = tmp_path / "unassigned.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        issue = client.get("/issues").json()[0]
        assert issue["id"] == issue_id
        assert issue["assignee_id"] is None
        assert issue["assignee_name"] is None


def test_put_assigns_and_resolves_name(tmp_path):
    # WHY: the whole point of an assignee is showing "who". Assigning must persist
    # the id AND surface the user's display name so the UI needs no second lookup.
    db_file = tmp_path / "assign.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file, email="ann@example.com", name="Ann")
        issue_id = _make_issue(client)

        r = client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 1},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["assignee_id"] == 1
        assert body["assignee_name"] == "Ann"

        # Persists — a fresh GET sees the assignment.
        assert client.get("/issues").json()[0]["assignee_name"] == "Ann"


def test_put_null_unassigns(tmp_path):
    # WHY: unassigning must be expressible. assignee_id=null clears the owner and
    # is always valid — there is no "user 0" to reject.
    db_file = tmp_path / "unassign.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 1},
            headers={"X-Athena-Actor": "1"},
        )
        r = client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": None},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["assignee_id"] is None
        assert r.json()["assignee_name"] is None


def test_put_unknown_user_is_422(tmp_path):
    # WHY: assigning to a user that doesn't exist is bad input, caught at the
    # boundary as a 422 — never a 500 from the foreign key blowing up.
    db_file = tmp_path / "bad_user.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 999},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_put_unknown_issue_is_404(tmp_path):
    # WHY: assigning an issue that doesn't exist must fail loudly, not silently
    # update zero rows.
    db_file = tmp_path / "missing_issue.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.put(
            "/issues/999/assignee",
            json={"assignee_id": 1},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 404


def test_put_requires_authentication(tmp_path):
    # WHY: assignment is a write, and writes need an actor — same rule as create.
    db_file = tmp_path / "noauth.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.put(f"/issues/{issue_id}/assignee", json={"assignee_id": 1})
        assert r.status_code == 401


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


def test_web_assign_is_gated_on_login(tmp_path):
    # WHY: the detail-page assign control is a write path. Logged out it is refused
    # with a sign-in prompt; logged in it assigns and bounces back to the issue.
    db_file = tmp_path / "web_assign.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, db_file)
        issue_id = _make_issue(client)  # actor "1" is the user we just made

        client.cookies.clear()
        logged_out = client.post(
            f"/aegis/issues/{issue_id}/assignee", data={"assignee_id": "1"}
        )
        assert logged_out.status_code == 401
        assert "sign in" in logged_out.text.lower()

        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        ok = client.post(
            f"/aegis/issues/{issue_id}/assignee",
            data={"assignee_id": "1"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert ok.headers["location"] == f"/aegis/issues/{issue_id}"

        c = db.connect(db_file)
        row = c.execute(
            "SELECT assignee_id FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        c.close()
        assert row["assignee_id"] == 1


def test_web_unassign_with_empty_value(tmp_path):
    # WHY: the "Unassigned" option submits an empty string, which the web layer
    # must read as None (clear), not choke on or treat as user 0.
    db_file = tmp_path / "web_unassign.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, db_file)
        issue_id = _make_issue(client)
        client.post(f"/aegis/issues/{issue_id}/assignee", data={"assignee_id": "1"})

        r = client.post(
            f"/aegis/issues/{issue_id}/assignee",
            data={"assignee_id": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303

        c = db.connect(db_file)
        row = c.execute(
            "SELECT assignee_id FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        c.close()
        assert row["assignee_id"] is None


def test_web_assign_unknown_user_rejected(tmp_path):
    # WHY: a crafted POST naming a nonexistent user is a 400, not a DB write that
    # the foreign key turns into a 500.
    db_file = tmp_path / "web_bad_user.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, db_file)
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/assignee", data={"assignee_id": "999"}
        )
        assert r.status_code == 400
