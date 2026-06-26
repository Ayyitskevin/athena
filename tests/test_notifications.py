"""Watching + the notification inbox.

These encode the contract: watching is idempotent; an event notifies every watcher
of its target EXCEPT the actor; creators/commenters/assignees start watching
automatically; the inbox lists and clears per user; and the web surface (inbox
page, nav badge, watch toggle) reflects it.
"""
from athena.core import activity, db, notifications
from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO users (email, name) VALUES ('b@e.com', 'B')")
    conn.commit()
    return conn


def _bootstrap(client):
    # First user via the bootstrap path is admin (id 1).
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def _make_user2(client):
    client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)  # id 2


# --- unit: fan-out ----------------------------------------------------------


def test_watch_is_idempotent(tmp_path):
    conn = _conn(tmp_path / "w.db")
    notifications.watch(conn, 1, "issue", 5)
    notifications.watch(conn, 1, "issue", 5)
    assert notifications.is_watching(conn, 1, "issue", 5)
    assert notifications.unwatch(conn, 1, "issue", 5) is True
    assert notifications.is_watching(conn, 1, "issue", 5) is False
    assert notifications.unwatch(conn, 1, "issue", 5) is False  # no longer watching


def test_event_notifies_watchers_except_actor(tmp_path):
    conn = _conn(tmp_path / "n.db")
    notifications.watch(conn, 1, "issue", 5)  # actor, also watching
    notifications.watch(conn, 2, "issue", 5)  # a watcher
    activity.record(
        conn, actor_id=1, verb="changed_status", target_kind="issue", target_id=5
    )
    # The watcher hears about it; the actor does not (even though they watch).
    assert notifications.unread_count(conn, 2) == 1
    assert notifications.unread_count(conn, 1) == 0


def test_mark_read_and_read_all(tmp_path):
    conn = _conn(tmp_path / "r.db")
    notifications.watch(conn, 2, "issue", 5)
    activity.record(conn, actor_id=1, verb="a", target_kind="issue", target_id=5)
    activity.record(conn, actor_id=1, verb="b", target_kind="issue", target_id=5)
    inbox = notifications.list_notifications(conn, 2)
    assert len(inbox) == 2 and notifications.unread_count(conn, 2) == 2
    assert notifications.mark_read(conn, 2, inbox[0]["id"]) is True
    assert notifications.unread_count(conn, 2) == 1
    assert notifications.mark_all_read(conn, 2) == 1
    assert notifications.unread_count(conn, 2) == 0


# --- API integration --------------------------------------------------------


def test_watcher_notified_actor_not(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        # user2 watches; user1 (the creator) already auto-watches.
        assert (
            client.post(
                "/watches",
                json={"target_kind": "issue", "target_id": issue["id"]},
                headers=H2,
            ).status_code
            == 204
        )
        client.patch(f"/issues/{issue['id']}", json={"status": "in_progress"}, headers=H1)
        # The watcher (user2) is notified; the actor (user1) is not.
        assert client.get("/notifications/unread_count", headers=H2).json()["count"] == 1
        assert client.get("/notifications/unread_count", headers=H1).json()["count"] == 0
        item = client.get("/notifications", headers=H2).json()[0]
        assert item["verb"] == "changed_status" and item["target_id"] == issue["id"]


def test_assignment_auto_watches_and_notifies_assignee(tmp_path):
    app = create_app(tmp_path / "assign.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        client.put(f"/issues/{issue['id']}/assignee", json={"assignee_id": 2}, headers=H1)
        items = client.get("/notifications", headers=H2).json()
        assert any(i["verb"] == "assigned" for i in items)


def test_commenter_autowatches_and_creator_is_notified(tmp_path):
    app = create_app(tmp_path / "comment.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        # user2 comments -> user1 (creator/watcher) notified; user2 now auto-watches.
        client.post(f"/issues/{issue['id']}/comments", json={"body": "hi"}, headers=H2)
        assert client.get("/notifications/unread_count", headers=H1).json()["count"] == 1
        # user1 replies -> user2 (now watching) notified.
        client.post(f"/issues/{issue['id']}/comments", json={"body": "re"}, headers=H1)
        assert any(
            i["verb"] == "commented"
            for i in client.get("/notifications", headers=H2).json()
        )


def test_watch_validation_and_unwatch(tmp_path):
    app = create_app(tmp_path / "v.db")
    with TestClient(app) as client:
        _bootstrap(client)
        assert (
            client.post(
                "/watches", json={"target_kind": "space", "target_id": 1}, headers=H1
            ).status_code
            == 422  # not a watchable kind
        )
        assert client.delete("/watches/issue/9", headers=H1).status_code == 404  # not watching
        client.post("/watches", json={"target_kind": "issue", "target_id": 9}, headers=H1)
        assert client.delete("/watches/issue/9", headers=H1).status_code == 204


def test_mark_read_via_api(tmp_path):
    app = create_app(tmp_path / "mr.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H2).json()  # user2 creates+watches
        client.patch(f"/issues/{issue['id']}", json={"status": "done"}, headers=H2)  # actor user2, no notif
        # user1 watches, user2 acts -> user1 notified.
        client.post(
            "/watches", json={"target_kind": "issue", "target_id": issue["id"]}, headers=H1
        )
        client.post(f"/issues/{issue['id']}/comments", json={"body": "c"}, headers=H2)
        nid = client.get("/notifications", headers=H1).json()[0]["id"]
        assert client.post(f"/notifications/{nid}/read", headers=H1).status_code == 204
        assert client.get("/notifications/unread_count", headers=H1).json()["count"] == 0


# --- web --------------------------------------------------------------------


def _login(client):
    client.post("/login", data={"email": "a@e.com", "password": "pw"})
    return client.cookies.get("athena_csrf")


def test_web_inbox_and_nav_badge(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        _bootstrap(client)
        csrf = _login(client)  # session = user1
        _make_user2(client)
        issue = client.post("/issues", json={"title": "ship"}, headers=H1).json()  # user1 watches
        client.post(f"/issues/{issue['id']}/comments", json={"body": "hi"}, headers=H2)  # notifies user1
        # The nav badge shows on any page once there's an unread notification.
        assert 'class="badge"' in client.get("/").text
        # The inbox page lists the event.
        inbox = client.get("/inbox").text
        assert "commented" in inbox and f"/aegis/issues/{issue['id']}" in inbox
        # Mark all read clears the badge.
        client.post("/inbox/read-all", headers={"X-CSRF-Token": csrf})
        assert 'class="badge"' not in client.get("/").text


def test_web_watch_toggle(tmp_path):
    app = create_app(tmp_path / "toggle.db")
    with TestClient(app) as client:
        _bootstrap(client)
        csrf = _login(client)
        _make_user2(client)
        # An issue created by user2, so user1 is NOT auto-watching it.
        issue = client.post("/issues", json={"title": "x"}, headers=H2).json()
        assert ">Watch<" in client.get(f"/aegis/issues/{issue['id']}").text
        client.post(
            f"/aegis/issues/{issue['id']}/watch", headers={"X-CSRF-Token": csrf}
        )
        assert ">Unwatch<" in client.get(f"/aegis/issues/{issue['id']}").text
