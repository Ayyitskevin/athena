"""Priority / mute / digest projections over the notification inbox.

These tests encode the contract: watch preferences are a personal read-time lens
over existing subscriptions and activity records. They do NOT introduce a second
notification authority — the activity log and notifications table stay the only
event stores. Owner scoping, priority precedence, mute/digest interaction, and
stable observed_at behavior are the load-bearing properties.
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from athena.core import activity, db, notifications
from athena.main import create_app

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
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def _make_user2(client):
    client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)


# --- unit: priority precedence ----------------------------------------------


def test_priority_preference_wins_over_issue_priority(tmp_path):
    conn = _conn(tmp_path / "p.db")
    notifications.watch(conn, 1, "issue", 5)
    # No preference yet -> falls back to issue priority.
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'urgent', 1)")
    conn.commit()

    proj = notifications.list_priority_notifications(conn, 1)
    assert proj["items"] == []

    # User overrides down to 'low'.
    notifications.set_preference(conn, 1, "issue", 5, priority="low")
    activity.record(conn, actor_id=2, verb="changed_status",
                    target_kind="issue", target_id=5)
    proj = notifications.list_priority_notifications(conn, 1)
    assert proj["items"][0]["priority"] == "low"
    assert proj["items"][0]["source"]["issue_priority"] == "urgent"


def test_priority_defaults_to_issue_priority_then_normal(tmp_path):
    conn = _conn(tmp_path / "p.db")
    notifications.watch(conn, 1, "issue", 5)
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'high', 1)")
    conn.commit()
    activity.record(conn, actor_id=2, verb="changed_status",
                    target_kind="issue", target_id=5)

    proj = notifications.list_priority_notifications(conn, 1)
    assert proj["items"][0]["priority"] == "high"


def test_priority_unknown_value_falls_back_to_normal(tmp_path):
    conn = _conn(tmp_path / "p.db")
    notifications.watch(conn, 1, "issue", 5)
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'high', 1)")
    conn.commit()
    # Simulate a stale/unknown priority stored directly (defensive test).
    conn.execute(
        "INSERT INTO watch_preferences (user_id, target_kind, target_id, priority) "
        "VALUES (?, 'issue', 5, 'bogus')",
        (1,),
    )
    conn.commit()
    activity.record(conn, actor_id=2, verb="changed_status",
                    target_kind="issue", target_id=5)

    proj = notifications.list_priority_notifications(conn, 1)
    assert proj["items"][0]["priority"] == "normal"


# --- unit: mute / digest interaction ----------------------------------------


def test_mute_suppresses_notifications(tmp_path):
    conn = _conn(tmp_path / "m.db")
    notifications.watch(conn, 1, "issue", 5)
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'medium', 1)")
    conn.commit()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    notifications.set_preference(conn, 1, "issue", 5, mute_until=future)
    activity.record(conn, actor_id=2, verb="changed_status",
                    target_kind="issue", target_id=5)

    proj = notifications.list_priority_notifications(conn, 1)
    assert proj["items"] == []

    # include_muted surfaces it with the muted flag.
    proj = notifications.list_priority_notifications(conn, 1, include_muted=True)
    assert len(proj["items"]) == 1
    assert proj["items"][0]["muted"] is True


def test_mute_fail_closed_on_malformed_timestamp(tmp_path):
    conn = _conn(tmp_path / "m.db")
    notifications.watch(conn, 1, "issue", 5)
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'medium', 1)")
    conn.commit()
    conn.execute(
        "INSERT INTO watch_preferences (user_id, target_kind, target_id, mute_until) "
        "VALUES (?, 'issue', 5, 'not-a-date')",
        (1,),
    )
    conn.commit()
    activity.record(conn, actor_id=2, verb="changed_status",
                    target_kind="issue", target_id=5)

    # Malformed mute_until fails closed: notification is NOT suppressed.
    proj = notifications.list_priority_notifications(conn, 1)
    assert len(proj["items"]) == 1
    assert proj["items"][0]["muted"] is False


def test_digest_buckets_group_by_window(tmp_path):
    conn = _conn(tmp_path / "d.db")
    notifications.watch(conn, 1, "issue", 5)
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'medium', 1)")
    conn.commit()
    notifications.set_preference(conn, 1, "issue", 5, digest_window_minutes=60)

    # Two events one minute apart fall in the same hour bucket.
    base = datetime(2026, 8, 28, 12, 5, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, created_at) "
        "VALUES (?, 'changed_status', 'issue', 5, ?)",
        (2, base.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.execute(
        "INSERT INTO notifications (user_id, event_id) VALUES (?, ?)",
        (1, conn.execute("SELECT last_insert_rowid()").fetchone()[0]),
    )
    conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, created_at) "
        "VALUES (?, 'commented', 'issue', 5, ?)",
        (2, (base + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.execute(
        "INSERT INTO notifications (user_id, event_id) VALUES (?, ?)",
        (1, conn.execute("SELECT last_insert_rowid()").fetchone()[0]),
    )
    conn.commit()

    proj = notifications.list_priority_notifications(conn, 1, digest=True)
    buckets = {item["digest_bucket"] for item in proj["items"]}
    assert len(buckets) == 1


def test_mute_overrides_digest(tmp_path):
    conn = _conn(tmp_path / "md.db")
    notifications.watch(conn, 1, "issue", 5)
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'medium', 1)")
    conn.commit()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    notifications.set_preference(
        conn, 1, "issue", 5, mute_until=future, digest_window_minutes=60
    )
    activity.record(conn, actor_id=2, verb="changed_status",
                    target_kind="issue", target_id=5)

    # Muted items are excluded from the digest projection by default.
    proj = notifications.list_priority_notifications(conn, 1, digest=True)
    assert proj["items"] == []


# --- unit: owner scoping and observed_at ------------------------------------


def test_preferences_are_scoped_to_owner(tmp_path):
    conn = _conn(tmp_path / "o.db")
    notifications.watch(conn, 1, "issue", 5)
    notifications.watch(conn, 2, "issue", 5)
    notifications.set_preference(conn, 1, "issue", 5, priority="urgent")

    pref1 = notifications.get_preference(conn, 1, "issue", 5)
    pref2 = notifications.get_preference(conn, 2, "issue", 5)
    assert pref1 is not None
    assert pref2 is None


def test_observed_at_is_stable_within_request(tmp_path):
    conn = _conn(tmp_path / "obs.db")
    notifications.watch(conn, 1, "issue", 5)
    conn.execute("INSERT INTO issues (id, title, body, status, priority, created_by) "
                 "VALUES (5, 't', 'b', 'open', 'medium', 1)")
    conn.commit()
    for _ in range(3):
        activity.record(conn, actor_id=2, verb="changed_status",
                        target_kind="issue", target_id=5)

    proj = notifications.list_priority_notifications(conn, 1)
    assert all(item["observed_at"] == proj["observed_at"] for item in proj["items"])
    # observed_at is a recent UTC timestamp.
    parsed = datetime.fromisoformat(proj["observed_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


# --- API integration --------------------------------------------------------


def test_api_priority_inbox_and_summary(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x", "priority": "high"},
                            headers=H2).json()
        client.post(
            "/watches",
            json={"target_kind": "issue", "target_id": issue["id"]},
            headers=H1,
        )
        client.patch(f"/issues/{issue['id']}", json={"status": "in_progress"},
                     headers=H2)

        # Default projection inherits issue priority.
        inbox = client.get("/notifications/priority", headers=H1).json()
        assert inbox["items"][0]["priority"] == "high"
        assert inbox["items"][0]["muted"] is False

        # Override priority and set mute.
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        client.put(
            f"/watches/issue/{issue['id']}/preference",
            json={"priority": "urgent", "mute_until": future},
            headers=H1,
        )

        inbox = client.get("/notifications/priority", headers=H1).json()
        assert inbox["items"] == []

        summary = client.get("/notifications/priority/summary?unread=true",
                             headers=H1).json()
        assert summary["by_priority"] == {
            "urgent": {"total": 1, "muted": 1}
        }

        # include_muted surfaces the item.
        inbox = client.get("/notifications/priority?include_muted=true",
                           headers=H1).json()
        assert inbox["items"][0]["priority"] == "urgent"
        assert inbox["items"][0]["muted"] is True


def test_api_preference_crud(tmp_path):
    app = create_app(tmp_path / "pref.db")
    with TestClient(app) as client:
        _bootstrap(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        client.post(
            "/watches",
            json={"target_kind": "issue", "target_id": issue["id"]},
            headers=H1,
        )

        # Create
        resp = client.put(
            f"/watches/issue/{issue['id']}/preference",
            json={"priority": "low", "digest_window_minutes": 120},
            headers=H1,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["priority"] == "low"
        assert body["digest_window_minutes"] == 120

        # Read
        assert client.get(f"/watches/issue/{issue['id']}/preference",
                          headers=H1).json()["priority"] == "low"

        # Update
        client.put(
            f"/watches/issue/{issue['id']}/preference",
            json={"priority": "high"},
            headers=H1,
        )
        assert client.get(f"/watches/issue/{issue['id']}/preference",
                          headers=H1).json()["priority"] == "high"

        # Delete
        assert client.delete(
            f"/watches/issue/{issue['id']}/preference", headers=H1
        ).status_code == 204
        assert client.get(
            f"/watches/issue/{issue['id']}/preference", headers=H1
        ).status_code == 404


def test_api_preference_validation(tmp_path):
    app = create_app(tmp_path / "val.db")
    with TestClient(app) as client:
        _bootstrap(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        resp = client.put(
            f"/watches/issue/{issue['id']}/preference",
            json={"mute_until": "not-a-date"},
            headers=H1,
        )
        assert resp.status_code == 422
        resp = client.put(
            f"/watches/issue/{issue['id']}/preference",
            json={"digest_window_minutes": 0},
            headers=H1,
        )
        assert resp.status_code == 422


def test_api_min_priority_filter(tmp_path):
    app = create_app(tmp_path / "min.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        low = client.post("/issues", json={"title": "low", "priority": "low"},
                          headers=H2).json()
        high = client.post("/issues", json={"title": "high", "priority": "high"},
                           headers=H2).json()
        client.post("/watches", json={"target_kind": "issue", "target_id": low["id"]},
                    headers=H1)
        client.post("/watches", json={"target_kind": "issue", "target_id": high["id"]},
                    headers=H1)
        client.patch(f"/issues/{low['id']}", json={"status": "in_progress"},
                     headers=H2)
        client.patch(f"/issues/{high['id']}", json={"status": "in_progress"},
                     headers=H2)

        inbox = client.get("/notifications/priority?min_priority=high",
                           headers=H1).json()
        assert len(inbox["items"]) == 1
        assert inbox["items"][0]["target_id"] == high["id"]


def test_api_owner_cannot_see_others_preferences(tmp_path):
    app = create_app(tmp_path / "own.db")
    with TestClient(app) as client:
        _bootstrap(client)
        _make_user2(client)
        issue = client.post("/issues", json={"title": "x"}, headers=H1).json()
        client.put(
            f"/watches/issue/{issue['id']}/preference",
            json={"priority": "urgent"},
            headers=H1,
        )
        # User 2 cannot read user 1's preference.
        assert client.get(
            f"/watches/issue/{issue['id']}/preference", headers=H2
        ).status_code == 404
