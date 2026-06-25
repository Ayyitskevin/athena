"""Tests for issue comments: the API sub-resource and the web detail thread.

These encode the contract: a comment is stamped to the authenticated author
(never a caller-supplied field), it must hang off a real issue (404 otherwise),
commenting needs authentication, empty comments are rejected, listing is
chronological, and the web form obeys the same actor rule as the API.
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


def test_comment_is_stamped_to_the_author_and_listed(tmp_path):
    # WHY: a comment records WHO said it — the authenticated actor, with their
    # name resolved for display — and it shows up when listing the issue's thread.
    db_file = tmp_path / "c.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)

        posted = client.post(
            f"/issues/{issue_id}/comments",
            json={"body": "looking into it"},
            headers={"X-Athena-Actor": "1"},
        )
        assert posted.status_code == 201
        c = posted.json()
        assert c["author_id"] == 1
        assert c["author_name"] == "Kevin"
        assert c["body"] == "looking into it"
        assert c["issue_id"] == issue_id

        listing = client.get(f"/issues/{issue_id}/comments")
        assert listing.status_code == 200
        assert [x["body"] for x in listing.json()] == ["looking into it"]


def test_comments_list_is_chronological(tmp_path):
    # WHY: a thread reads oldest-to-newest. Order is by id (insertion), not
    # whatever the DB feels like returning.
    db_file = tmp_path / "order.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        for text in ("first", "second", "third"):
            client.post(
                f"/issues/{issue_id}/comments",
                json={"body": text},
                headers={"X-Athena-Actor": "1"},
            )
        bodies = [x["body"] for x in client.get(f"/issues/{issue_id}/comments").json()]
        assert bodies == ["first", "second", "third"]


def test_comment_on_missing_issue_is_404(tmp_path):
    # WHY: you can't comment on an issue that doesn't exist — fail loudly rather
    # than let the foreign key raise a 500.
    db_file = tmp_path / "missing.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post(
            "/issues/999/comments",
            json={"body": "ghost"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 404
        # And listing a missing issue's comments is also a 404, not an empty 200.
        assert client.get("/issues/999/comments").status_code == 404


def test_commenting_requires_authentication(tmp_path):
    # WHY: a comment is attributed to someone — no actor, no write.
    db_file = tmp_path / "noauth.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.post(f"/issues/{issue_id}/comments", json={"body": "anon"})
        assert r.status_code == 401


def test_empty_comment_is_rejected(tmp_path):
    # WHY: a blank comment is noise. Whitespace-only is treated as empty (422).
    db_file = tmp_path / "empty.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        r = client.post(
            f"/issues/{issue_id}/comments",
            json={"body": "   "},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


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


def test_web_comment_is_gated_and_renders(tmp_path):
    # WHY: the detail-page comment form is a write path. Logged out it is refused;
    # logged in the comment is stored and shows up on the rendered page.
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)  # actor 1 = the user we just made

        # Logged out: refused.
        client.cookies.clear()
        out = client.post(
            f"/aegis/issues/{issue_id}/comments", data={"body": "nope"}
        )
        assert out.status_code == 401
        assert "sign in" in out.text.lower()

        # Logged in: stored, 303 back to the issue, and visible on the page.
        client.post("/login", data={"email": "kevin@example.com", "password": "secret"})
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        ok = client.post(
            f"/aegis/issues/{issue_id}/comments",
            data={"body": "on it"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert ok.headers["location"] == f"/aegis/issues/{issue_id}"

        page = client.get(f"/aegis/issues/{issue_id}")
        assert "on it" in page.text
        assert "Kevin" in page.text


def test_web_comment_escapes_untrusted_html(tmp_path):
    # WHY: comment bodies are untrusted user text. The detail thread may preserve
    # line breaks, but it must never mark raw HTML safe.
    db_file = tmp_path / "web_xss.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        client.post(
            f"/aegis/issues/{issue_id}/comments",
            data={"body": "<script>alert(1)</script>\nsecond"},
            follow_redirects=False,
        )
        page = client.get(f"/aegis/issues/{issue_id}")

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;<br>second" in page.text


def test_web_empty_comment_is_rejected(tmp_path):
    db_file = tmp_path / "web_empty.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client)
        issue_id = _make_issue(client)
        r = client.post(f"/aegis/issues/{issue_id}/comments", data={"body": "   "})
        assert r.status_code == 400
