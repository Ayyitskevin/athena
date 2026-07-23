"""Tests for editing an issue's title and body: the partial PATCH on the API
and the edit form on the web.

These encode the contract, not just HTTP shapes: a PATCH touches only the fields
sent (a title edit must not blank the body), an empty title is rejected (the
column is NOT NULL and a titleless issue is meaningless), editing a missing
issue is a 404, and the web edit path obeys the same actor rule as every write —
logged-out callers cannot edit.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def _seed_user(db_file, email="kevin@example.com", name="Kevin"):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _make_issue(client, title="original", body="original body", actor="1") -> int:
    r = client.post(
        "/issues",
        json={"title": title, "body": body},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201
    return r.json()["id"]


# --- REST API -------------------------------------------------------------


def test_patch_edits_title_and_body(tmp_path):
    # WHY: an issue's description is wrong on day one as often as not. Editing
    # title and body must persist the new content.
    db_file = tmp_path / "edit.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.patch(
            f"/issues/{issue_id}",
            json={"title": "fixed title", "body": "fixed body"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["title"] == "fixed title"
        assert r.json()["body"] == "fixed body"
        # Persists across a fresh read.
        again = client.get("/issues").json()[0]
        assert again["title"] == "fixed title"
        assert again["body"] == "fixed body"


def test_patch_is_partial_body_only_leaves_title(tmp_path):
    # WHY: a partial update must not clobber fields the client didn't send.
    # Editing only the body must leave the title (and status) intact.
    db_file = tmp_path / "partial.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client, title="keep me", body="old")
        r = client.patch(
            f"/issues/{issue_id}",
            json={"body": "new body"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["title"] == "keep me"  # untouched
        assert r.json()["body"] == "new body"


def test_patch_status_still_works_alongside_edit(tmp_path):
    # WHY: generalizing PATCH must not regress the status-change path it grew out
    # of — a status-only PATCH still moves the issue.
    db_file = tmp_path / "status_still.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.patch(
            f"/issues/{issue_id}",
            json={"status": "done"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "done"


def test_patch_rejects_empty_title(tmp_path):
    # WHY: title is NOT NULL and a titleless issue is meaningless — a blank title
    # is a 422 at the boundary, never written.
    db_file = tmp_path / "empty_title.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.patch(
            f"/issues/{issue_id}",
            json={"title": "   "},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_patch_empty_body_is_a_422(tmp_path):
    # WHY: a PATCH that sends nothing to change is a client mistake, surfaced as
    # 422 rather than a silent no-op the caller might read as success.
    db_file = tmp_path / "nothing.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.patch(
            f"/issues/{issue_id}", json={}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 422


def test_patch_unknown_issue_is_404(tmp_path):
    db_file = tmp_path / "missing.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.patch(
            "/issues/999", json={"title": "x"}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 404


def test_patch_edit_requires_authentication(tmp_path):
    db_file = tmp_path / "noauth.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.patch(f"/issues/{issue_id}", json={"title": "x"})
        assert r.status_code == 401


# --- Web (browser session) ------------------------------------------------


def _login(client):
    client.post(
        "/users",
        json={"email": "kevin@example.com", "name": "Kevin", "password": "secret"},
    )
    client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_edit_form_prefills_and_is_gated(tmp_path):
    # WHY: the edit form must come up prefilled with the current content (so you
    # edit, not retype), and only for a logged-in user.
    db_file = tmp_path / "web_form.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client, title="prefilled title", body="prefilled body")

        form = client.get(f"/aegis/issues/{issue_id}/edit")
        assert form.status_code == 200
        assert "prefilled title" in form.text
        assert "prefilled body" in form.text

        client.cookies.clear()
        logged_out = client.get(f"/aegis/issues/{issue_id}/edit")
        assert logged_out.status_code == 401
        assert "sign in" in logged_out.text.lower()


def test_web_edit_saves_and_redirects(tmp_path):
    # WHY: submitting the edit form must persist the change and bounce back to the
    # issue; logged out it is refused.
    db_file = tmp_path / "web_save.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)

        client.cookies.clear()
        refused = client.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "hacked", "body": "x"},
        )
        assert refused.status_code == 401

        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        ok = client.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "edited via web", "body": "new web body"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert ok.headers["location"] == f"/aegis/issues/{issue_id}"

        c = db.connect(db_file)
        row = c.execute(
            "SELECT title, body FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        c.close()
        assert row["title"] == "edited via web"
        assert row["body"] == "new web body"


def test_web_edit_rejects_empty_title(tmp_path):
    # WHY: a blank title from the form is a 400, not a write that breaks the
    # NOT NULL invariant.
    db_file = tmp_path / "web_empty.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        r = client.post(
            f"/aegis/issues/{issue_id}/edit", data={"title": "   ", "body": "x"}
        )
        assert r.status_code == 400
