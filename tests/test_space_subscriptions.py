"""Space subscriptions — a shared space that says when it moved.

The contract these encode:

  * watching a SPACE subscribes you to every event on every page inside it, not
    just to the space row's own lifecycle. That is the whole point: a space used
    as fleet memory is only useful if you hear it change without re-reading it;
  * the fan-out stays in ONE place (notifications.notify_watchers) — a page event
    reaches space watchers through the same call that reaches page watchers, so
    there is no second call site to forget;
  * the existing rules survive the new kind: your own action never notifies you,
    a watcher of both the page and its space gets ONE row, watching twice is a
    no-op, and the inbox still gates on visibility at READ time;
  * a page in a DIFFERENT space is silence — a subscription is to a container,
    not to Mentor at large;
  * deletion notifies. It used to notify nobody (the purge dropped the page and
    its watches before the event existed), which made the loudest change in a
    space the one nobody heard.
"""

import asyncio

from fastapi.testclient import TestClient
import pytest

from athena.core import activity, db, notifications
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mentor import page_commands, pages, spaces

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO users (email, name) VALUES ('b@e.com', 'B')")
    conn.commit()
    return conn


def _space_with_page(conn, *, key="ENG", name="Eng", title="Runbook"):
    space = spaces.create_space(conn, key=key, name=name, created_by=1)
    page = pages.create_page(
        conn, space_id=space["id"], title=title, body="", created_by=1
    )
    return space, page


@pytest.fixture()
def client(tmp_path):
    """Two real users over the real app — user 1 is the admin/actor, user 2 the
    subscriber whose inbox the assertions read."""
    with TestClient(create_app(tmp_path / "app.db")) as c:
        c.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "secret"},
            headers=H1,
        )
        c.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)
        yield c


def _login(client):
    """Sign in as user 1 in the browser sense and arm the CSRF header, matching
    the Mentor web tests' helper."""
    client.post("/login", data={"email": "a@e.com", "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- unit: the fan-out ------------------------------------------------------


def test_space_watcher_hears_a_page_event_inside_the_space(tmp_path):
    # WHY: the headline claim. User 2 never touched the page and does not watch
    # it — the space subscription alone carries the news.
    conn = _conn(tmp_path / "s.db")
    space, page = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    activity.record(
        conn, actor_id=1, verb="page_edited", target_kind="page", target_id=page["id"]
    )
    assert notifications.unread_count(conn, 2) == 1


def test_page_in_another_space_is_silence(tmp_path):
    # WHY: a subscription is to a container, not to Mentor at large.
    conn = _conn(tmp_path / "other.db")
    watched, _ = _space_with_page(conn, key="ENG", name="Eng")
    _, elsewhere = _space_with_page(conn, key="OPS", name="Ops", title="Other")
    notifications.watch(conn, 2, "space", watched["id"])
    activity.record(
        conn,
        actor_id=1,
        verb="page_edited",
        target_kind="page",
        target_id=elsewhere["id"],
    )
    assert notifications.unread_count(conn, 2) == 0


def test_your_own_page_edit_does_not_notify_you(tmp_path):
    # WHY: the actor-exclusion rule must hold on the indirect path too, or a
    # subscribed agent would wake itself up on every write it makes.
    conn = _conn(tmp_path / "self.db")
    space, page = _space_with_page(conn)
    notifications.watch(conn, 1, "space", space["id"])
    activity.record(
        conn, actor_id=1, verb="page_edited", target_kind="page", target_id=page["id"]
    )
    assert notifications.unread_count(conn, 1) == 0


def test_watching_page_and_its_space_yields_one_notification(tmp_path):
    # WHY: two passes over watches must not become two inbox rows — the UNIQUE
    # (user_id, event_id) is what collapses them, and this proves it does.
    conn = _conn(tmp_path / "dedupe.db")
    space, page = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    notifications.watch(conn, 2, "page", page["id"])
    activity.record(
        conn, actor_id=1, verb="page_edited", target_kind="page", target_id=page["id"]
    )
    assert notifications.unread_count(conn, 2) == 1


def test_space_watch_also_covers_the_space_rows_own_events(tmp_path):
    # WHY: 'space_edited' and friends already record with target_kind='space', so
    # the direct pass covers them — the subscription is one thing, not two.
    conn = _conn(tmp_path / "own.db")
    space, _ = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    activity.record(
        conn,
        actor_id=1,
        verb="space_edited",
        target_kind="space",
        target_id=space["id"],
    )
    assert notifications.unread_count(conn, 2) == 1


def test_unwatch_stops_the_delivery(tmp_path):
    conn = _conn(tmp_path / "off.db")
    space, page = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    assert notifications.unwatch(conn, 2, "space", space["id"]) is True
    activity.record(
        conn, actor_id=1, verb="page_edited", target_kind="page", target_id=page["id"]
    )
    assert notifications.unread_count(conn, 2) == 0


def test_watching_a_space_twice_is_a_no_op(tmp_path):
    conn = _conn(tmp_path / "idem.db")
    space, _ = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    notifications.watch(conn, 2, "space", space["id"])
    assert notifications.is_watching(conn, 2, "space", space["id"])
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM watches WHERE target_kind = 'space'"
        ).fetchone()["n"]
        == 1
    )


def test_a_page_comment_reaches_the_space_watcher(tmp_path):
    # WHY: 'the handbook changed' includes the discussion on it — every event
    # whose target is a page in the space counts, one rule with no carve-outs.
    conn = _conn(tmp_path / "comment.db")
    space, page = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    activity.record(
        conn,
        actor_id=1,
        verb="page_commented",
        target_kind="page",
        target_id=page["id"],
    )
    assert notifications.unread_count(conn, 2) == 1


# --- deletion: the event now precedes the purge -----------------------------


def test_deleting_a_page_notifies_the_space_watcher(tmp_path):
    # WHY: purge_page drops the page row AND its watches, so recording the event
    # afterwards reached nobody by either route. A deletion is the loudest thing
    # that can happen to a shared space; silence was the wrong answer.
    conn = _conn(tmp_path / "del-space.db")
    space, page = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    assert (
        page_commands.delete_page(
            conn, actor_id=1, page_id=page["id"], title=page["title"]
        )
        is True
    )
    inbox = notifications.list_notifications(conn, 2)
    assert [n["verb"] for n in inbox] == ["page_deleted"]
    # The page really is gone — the notification survives it because it points at
    # the append-only trail, not at the row.
    assert pages.get_page(conn, page["id"]) is None


def test_a_deleted_pages_notification_renders_only_for_an_admin(tmp_path):
    # WHY: the honest limit, pinned so nobody claims more than this delivers. The
    # inbox gate proves page visibility by looking the page up — and once the row
    # is gone there is nothing left to prove it with, so it fails CLOSED. An
    # admin's read is ungated and renders the deletion; a member's does not. The
    # full fix (an event-time visibility envelope for page targets, like the one
    # issues carry) is an access-model change, deliberately not smuggled in here.
    conn = _conn(tmp_path / "del-gate.db")
    space, page = _space_with_page(conn)
    notifications.watch(conn, 2, "space", space["id"])
    page_commands.delete_page(conn, actor_id=1, page_id=page["id"], title=page["title"])
    member = {"id": 2, "role": "member"}
    admin = {"id": 3, "role": "admin"}
    assert notifications.list_notifications(conn, 2, actor=member) == []
    assert notifications.unread_count(conn, 2, actor=member) == 0
    assert [
        n["verb"] for n in notifications.list_notifications(conn, 2, actor=admin)
    ] == ["page_deleted"]


def test_deleting_a_page_notifies_its_direct_watcher_too(tmp_path):
    # WHY: the same ordering fix; a page watcher's final delivery is the news that
    # the thing they watched no longer exists. Their watch is then purged.
    conn = _conn(tmp_path / "del-page.db")
    _, page = _space_with_page(conn)
    notifications.watch(conn, 2, "page", page["id"])
    page_commands.delete_page(conn, actor_id=1, page_id=page["id"], title=page["title"])
    assert notifications.unread_count(conn, 2) == 1
    assert notifications.is_watching(conn, 2, "page", page["id"]) is False


# --- visibility -------------------------------------------------------------


def test_inbox_hides_events_from_a_space_the_watcher_cannot_see(tmp_path):
    # WHY: watches are written ungated (a space can go private after the fact), so
    # the inbox is what enforces visibility — the same read-time gate page watches
    # already rely on. Ungated internal reads still see the row; the owner's own
    # gated read does not.
    conn = _conn(tmp_path / "private.db")
    space, page = _space_with_page(conn)
    conn.execute(
        "UPDATE spaces SET visibility = 'private' WHERE id = ?", (space["id"],)
    )
    conn.commit()
    notifications.watch(conn, 2, "space", space["id"])
    activity.record(
        conn, actor_id=1, verb="page_edited", target_kind="page", target_id=page["id"]
    )
    viewer = {"id": 2, "role": "member"}
    assert notifications.unread_count(conn, 2) == 1  # ungated: the row exists
    assert notifications.unread_count(conn, 2, actor=viewer) == 0  # gated: invisible
    assert notifications.list_notifications(conn, 2, actor=viewer) == []


# --- REST + MCP + web -------------------------------------------------------


def test_rest_watches_a_space_and_refuses_a_garbage_kind(client):
    space = client.post(
        "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
    ).json()
    assert (
        client.post(
            "/watches",
            json={"target_kind": "space", "target_id": space["id"]},
            headers=H2,
        ).status_code
        == 204
    )
    page = client.post(
        f"/spaces/{space['id']}/pages", json={"title": "Runbook"}, headers=H1
    ).json()
    inbox = client.get("/notifications", headers=H2).json()
    assert [n["verb"] for n in inbox] == ["page_created"]
    assert page["title"] == "Runbook"

    bad = client.post(
        "/watches", json={"target_kind": "planet", "target_id": 1}, headers=H2
    )
    assert bad.status_code == 422
    assert "space" in bad.json()["detail"]

    assert client.delete(f"/watches/space/{space['id']}", headers=H2).status_code == 204
    # Unwatching what you no longer watch is a refusal, not a silent success.
    assert client.delete(f"/watches/space/{space['id']}", headers=H2).status_code == 404


def test_mcp_watch_kind_matches_the_data_layer_vocabulary():
    # WHY: the MCP Literal is spelled out (a Literal must be static), so this is
    # the guard that stops it drifting away from WATCHABLE_KINDS.
    from typing import get_args

    assert set(get_args(mcp_server.WatchKind)) == set(notifications.WATCHABLE_KINDS)


def test_mcp_watch_tools_forward_and_enforce_the_vocabulary():
    # WHY: an agent's only route to a subscription is these two tools. They must
    # reach the right REST calls, and refuse a kind the boundary would refuse.
    from mcp.server.fastmcp.exceptions import ToolError

    class RecordingAthenaClient:
        def __init__(self):
            self.calls: list[tuple] = []

        def watch(self, target_kind, target_id):
            self.calls.append(("watch", target_kind, target_id))

        def unwatch(self, target_kind, target_id):
            self.calls.append(("unwatch", target_kind, target_id))

    recorder = RecordingAthenaClient()
    server = mcp_server.build_server(recorder)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools["watch"].inputSchema["properties"]["target_kind"]["enum"]) == set(
        notifications.WATCHABLE_KINDS
    )
    asyncio.run(server.call_tool("watch", {"target_kind": "space", "target_id": 3}))
    asyncio.run(server.call_tool("unwatch", {"target_kind": "space", "target_id": 3}))
    assert recorder.calls == [("watch", "space", 3), ("unwatch", "space", 3)]
    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool("watch", {"target_kind": "planet", "target_id": 3})
        )
    with pytest.raises(ToolError):  # ids are row ids, never zero or negative
        asyncio.run(server.call_tool("watch", {"target_kind": "space", "target_id": 0}))


def test_web_space_watch_toggle(client):
    space = client.post(
        "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
    ).json()
    _login(client)
    detail = client.get(f"/mentor/spaces/{space['id']}")
    assert f"/mentor/spaces/{space['id']}/watch" in detail.text

    watched = client.post(f"/mentor/spaces/{space['id']}/watch", follow_redirects=True)
    assert f"/mentor/spaces/{space['id']}/unwatch" in watched.text
    unwatched = client.post(
        f"/mentor/spaces/{space['id']}/unwatch", follow_redirects=True
    )
    assert f"/mentor/spaces/{space['id']}/watch" in unwatched.text
    assert f"/mentor/spaces/{space['id']}/unwatch" not in unwatched.text


def test_web_refuses_to_watch_a_space_you_cannot_see(client):
    # WHY: "private" and "missing" must look identical, or the watch button
    # becomes an existence oracle.
    _login(client)
    missing = client.post("/mentor/spaces/9999/watch", follow_redirects=False)
    assert missing.status_code == 404
    assert "not found" in missing.text
