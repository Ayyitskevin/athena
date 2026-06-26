"""@mentions — notifying a user named by [[user:N]] in a body or comment.

These encode the contract: a mention notifies the named user and starts them
watching the thread; you can't mention yourself into your own inbox; a mention of a
non-existent user is just text; and [[user:N]] renders as @Name (unknown ids stay
literal so a typo is visible).
"""
from athena.core import db, notifications
from athena.main import create_app
from athena.web.render import render_body, render_comment
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'Alice')")
    conn.execute("INSERT INTO users (email, name) VALUES ('b@e.com', 'Bob')")
    conn.commit()
    return conn


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})


def _make_user2(client):
    client.post("/users", json={"email": "b@e.com", "name": "Bob"}, headers=H1)


# --- unit -------------------------------------------------------------------


def test_parse_mentions_distinct_in_order_and_strict():
    assert notifications.parse_mentions("[[user:2]] x [[user:5]] [[user:2]]") == [2, 5]
    assert notifications.parse_mentions("[[user:abc]] [[user:]] none") == []
    assert notifications.parse_mentions("") == []
    assert notifications.parse_mentions(None) == []


def test_process_mentions_notifies_and_watches(tmp_path):
    conn = _conn(tmp_path / "p.db")
    from athena.core import activity

    ev = activity.record(
        conn, actor_id=1, verb="created", target_kind="issue", target_id=7
    )
    notifications.process_mentions(conn, event_id=ev["id"], actor_id=1, text="hi [[user:2]]")
    assert notifications.unread_count(conn, 2) == 1
    assert notifications.is_watching(conn, 2, "issue", 7)  # now follows the thread


def test_process_mentions_skips_self_and_unknown(tmp_path):
    conn = _conn(tmp_path / "s.db")
    from athena.core import activity

    ev = activity.record(
        conn, actor_id=1, verb="created", target_kind="issue", target_id=7
    )
    notifications.process_mentions(
        conn, event_id=ev["id"], actor_id=1, text="[[user:1]] and [[user:999]]"
    )
    assert notifications.unread_count(conn, 1) == 0  # no self-notify
    assert notifications.unread_count(conn, 999) == 0  # unknown user, no row


# --- integration: mentions across the write paths ---------------------------


def test_mention_in_new_issue_notifies_and_subscribes(tmp_path):
    app = create_app(tmp_path / "ni.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post(
            "/issues", json={"title": "x", "body": "ping [[user:2]]"}, headers=H1
        ).json()
        assert client.get("/notifications/unread_count", headers=H2).json()["count"] == 1
        assert client.get("/notifications", headers=H2).json()[0]["verb"] == "created"
        # The mention also subscribed user2, so a later change reaches them too.
        client.patch(f"/issues/{issue['id']}", json={"status": "done"}, headers=H1)
        assert client.get("/notifications/unread_count", headers=H2).json()["count"] == 2


def test_mention_in_comment_notifies(tmp_path):
    app = create_app(tmp_path / "cm.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        client.post(
            f"/issues/{issue['id']}/comments", json={"body": "look [[user:2]]"}, headers=H1
        )
        items = client.get("/notifications", headers=H2).json()
        assert items and items[0]["verb"] == "commented"


def test_mention_added_on_edit_notifies(tmp_path):
    app = create_app(tmp_path / "ed.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x", "body": "plain"}, headers=H1).json()
        assert client.get("/notifications/unread_count", headers=H2).json()["count"] == 0
        client.patch(f"/issues/{issue['id']}", json={"body": "now [[user:2]]"}, headers=H1)
        items = client.get("/notifications", headers=H2).json()
        assert any(i["verb"] == "issue_edited" for i in items)


def test_mention_in_new_page_notifies(tmp_path):
    app = create_app(tmp_path / "pg.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1).json()
        client.post(
            f"/spaces/{sp['id']}/pages",
            json={"title": "Doc", "body": "cc [[user:2]]"},
            headers=H1,
        )
        items = client.get("/notifications", headers=H2).json()
        assert items and items[0]["verb"] == "page_created"


def test_self_mention_does_not_notify(tmp_path):
    app = create_app(tmp_path / "self.db")
    with TestClient(app) as client:
        _bootstrap(client)
        client.post("/issues", json={"title": "x", "body": "[[user:1]]"}, headers=H1)
        assert client.get("/notifications/unread_count", headers=H1).json()["count"] == 0


def test_unknown_user_mention_is_harmless(tmp_path):
    app = create_app(tmp_path / "unk.db")
    with TestClient(app) as client:
        _bootstrap(client)
        r = client.post(
            "/issues", json={"title": "x", "body": "[[user:999]]"}, headers=H1
        )
        assert r.status_code == 201  # no crash on a dangling mention


# --- rendering --------------------------------------------------------------


def test_render_body_renders_mention_as_name(tmp_path):
    conn = _conn(tmp_path / "r.db")
    html = str(render_body(conn, "hey [[user:2]] take a look"))
    assert 'class="mention"' in html and "@Bob" in html


def test_render_unknown_mention_is_literal(tmp_path):
    conn = _conn(tmp_path / "ru.db")
    html = str(render_body(conn, "ghost [[user:999]]"))
    assert "[[user:999]]" in html  # shown literally so the typo is visible


def test_render_comment_renders_mention(tmp_path):
    conn = _conn(tmp_path / "rc.db")
    html = str(render_comment(conn, "ping [[user:1]]"))
    assert "@Alice" in html and 'class="mention"' in html


def test_web_issue_detail_shows_mention(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post(
            "/issues", json={"title": "x", "body": "cc [[user:2]]"}, headers=H1
        ).json()
        page = client.get(f"/aegis/issues/{issue['id']}").text
        assert "@Bob" in page and 'class="mention"' in page
