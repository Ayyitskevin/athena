"""Imported history stays on the trail, not in generic watcher inboxes."""

import pytest

from athena.core import activity, db, notifications
from athena.mentor import pages, spaces


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "notifications.db")
    db.migrate(connection)
    for name in ("author", "watcher", "mentioned", "untouched"):
        connection.execute(
            "INSERT INTO users (email, name) VALUES (?, ?)",
            (f"{name}@example.test", name),
        )
    connection.commit()
    yield connection
    connection.close()


def test_imported_issue_keeps_history_without_watcher_mail(conn):
    notifications.watch(conn, 2, "issue", 5)
    native = activity.record(
        conn, actor_id=1, verb="commented", target_kind="issue", target_id=5
    )
    before = notifications.list_notifications(conn, 2)
    assert [row["event_id"] for row in before] == [native["id"]]
    imported = activity.record(
        conn,
        actor_id=1,
        verb="forge_commit",
        target_kind="issue",
        target_id=5,
        imported_at="2026-10-21 00:00:00",
    )
    assert activity.get_activity(conn, imported["id"]) == imported
    assert notifications.list_notifications(conn, 2) == before
    assert notifications.unread_count(conn, 2) == 1


@pytest.mark.parametrize("verb", ["forge_commit", "commented", "future_import"])
@pytest.mark.parametrize("imported_at", [None, "2026-10-21 00:00:00", ""])
def test_import_marker_not_verb_controls_watcher_fanout(conn, verb, imported_at):
    # Even an empty non-null marker is imported; native forge-looking verbs are not.
    notifications.watch(conn, 1, "issue", 5)
    notifications.watch(conn, 2, "issue", 5)
    event = activity.record(
        conn,
        actor_id=1,
        verb=verb,
        target_kind="issue",
        target_id=5,
        imported_at=imported_at,
    )
    assert activity.get_activity(conn, event["id"])["imported_at"] == imported_at
    assert notifications.unread_count(conn, 1) == 0
    assert notifications.unread_count(conn, 2) == (1 if imported_at is None else 0)


@pytest.mark.parametrize("kind", ["issue", "page", "space"])
def test_imported_targets_suppress_direct_and_inherited_watches(conn, kind):
    space = spaces.create_space(conn, key="TEST", name="Test", created_by=1)
    page = pages.create_page(
        conn, space_id=space["id"], title="Test", body="", created_by=1
    )
    target_id = {"issue": 5, "page": page["id"], "space": space["id"]}[kind]
    notifications.watch(conn, 2, kind, target_id)
    notifications.watch(conn, 3, "space", space["id"])
    imported = activity.record(
        conn,
        actor_id=1,
        verb="changed",
        target_kind=kind,
        target_id=target_id,
        imported_at="2026-10-21 00:00:00",
    )
    assert activity.get_activity(conn, imported["id"]) == imported
    assert notifications.unread_count(conn, 2) == 0
    assert notifications.unread_count(conn, 3) == 0
    native = activity.record(
        conn, actor_id=1, verb="changed", target_kind=kind, target_id=target_id
    )
    assert [row["event_id"] for row in notifications.list_notifications(conn, 2)] == [
        native["id"]
    ]
    assert notifications.unread_count(conn, 3) == (0 if kind == "issue" else 1)


@pytest.mark.parametrize("imported_at", [None, "2026-10-21 00:00:00"])
def test_mentions_survive_and_deduplicate_independently_of_watcher_fanout(
    conn, imported_at
):
    for uid in (1, 2, 4):
        notifications.watch(conn, uid, "issue", 5)
    event = activity.record(
        conn,
        actor_id=1,
        verb="commented",
        target_kind="issue",
        target_id=5,
        imported_at=imported_at,
    )
    for _ in range(2):
        notifications.process_mentions(
            conn,
            event_id=event["id"],
            actor_id=1,
            text="[[user:1]] [[user:2]] [[user:2]] [[user:3]] [[user:999]]",
        )
        notifications.notify_watchers(
            conn, event_id=event["id"], actor_id=1, target_kind="issue", target_id=5
        )
    for uid in (2, 3):
        assert [
            row["event_id"] for row in notifications.list_notifications(conn, uid)
        ] == [event["id"]]
        assert notifications.is_watching(conn, uid, "issue", 5)
    assert notifications.unread_count(conn, 1) == 0
    assert notifications.unread_count(conn, 4) == (1 if imported_at is None else 0)


def test_import_does_not_acknowledge_or_rewrite_existing_inbox_rows(conn):
    notifications.watch(conn, 2, "issue", 5)
    for _ in range(2):
        activity.record(
            conn, actor_id=1, verb="commented", target_kind="issue", target_id=5
        )
    inbox = notifications.list_notifications(conn, 2)
    assert notifications.mark_read(conn, 2, inbox[0]["id"])
    before = notifications.list_notifications(conn, 2)
    assert sorted(row["read_at"] is not None for row in before) == [False, True]
    activity.record(
        conn,
        actor_id=1,
        verb="forge_commit",
        target_kind="issue",
        target_id=5,
        imported_at="2026-10-21 00:00:00",
    )
    assert notifications.list_notifications(conn, 2) == before


@pytest.mark.parametrize("imported_at", [None, "2026-10-21 00:00:00"])
def test_event_and_notifications_still_rollback_together(conn, imported_at):
    notifications.watch(conn, 2, "issue", 5)
    with pytest.raises(RuntimeError, match="deliberate rollback"):
        with db.transaction(conn, immediate=True):
            event = activity.record(
                conn,
                actor_id=1,
                verb="commented",
                target_kind="issue",
                target_id=5,
                imported_at=imported_at,
                commit=False,
            )
            notifications.process_mentions(
                conn, event_id=event["id"], actor_id=1, text="[[user:3]]", commit=False
            )
            assert notifications.unread_count(conn, 3) == 1
            raise RuntimeError("deliberate rollback")
    assert activity.get_activity(conn, event["id"]) is None
    assert notifications.unread_count(conn, 2) == 0
    assert notifications.unread_count(conn, 3) == 0
    assert not notifications.is_watching(conn, 3, "issue", 5)
