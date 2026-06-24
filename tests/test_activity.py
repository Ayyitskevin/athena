"""Tests for the activity log: the audit trail behind "who did what".

These encode the contract, not just HTTP shapes. The data layer records a row
stamped with the actor and returns it with the actor's display name; the global
feed is newest-first; a target filter narrows to one thing's history. The REST
feed is a privileged read (auth required, like search). And the issue endpoints
record the lifecycle facts as a side effect: created, status changes (with the
"old → new" detail), and assign/unassign — only when something actually changed.
"""
from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app


def _migrated_conn(db_file):
    """A connection on a fresh, fully-migrated DB — for data-layer tests that
    don't boot the app (migrations otherwise run in the app's lifespan startup)."""
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(db_file, email="kevin@example.com", name="Kevin"):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _make_issue(client, title="ship it", actor="1") -> int:
    r = client.post("/issues", json={"title": title}, headers={"X-Athena-Actor": actor})
    assert r.status_code == 201
    return r.json()["id"]


# --- Data layer -----------------------------------------------------------


def test_record_returns_row_with_actor_name(tmp_path):
    # WHY: a feed must render "Kevin closed AEGIS-12" without a second lookup, so
    # every recorded row carries the actor's display name resolved at read time.
    db_file = tmp_path / "record.db"
    conn = _migrated_conn(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("k@e.com", "Kevin"))
    conn.commit()
    row = activity.record(
        conn, actor_id=1, verb="created", target_kind="issue", target_id=7
    )
    conn.close()
    assert row["actor_id"] == 1
    assert row["actor_name"] == "Kevin"
    assert row["verb"] == "created"
    assert row["target_kind"] == "issue"
    assert row["target_id"] == 7
    assert row["detail"] == ""


def test_list_is_newest_first_and_target_scoped(tmp_path):
    # WHY: the timeline only reads right newest-first, and a per-target query must
    # return ONLY that target's history — never bleed in another issue's events.
    db_file = tmp_path / "list.db"
    conn = _migrated_conn(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("k@e.com", "Kevin"))
    conn.commit()
    activity.record(conn, actor_id=1, verb="created", target_kind="issue", target_id=1)
    activity.record(conn, actor_id=1, verb="created", target_kind="issue", target_id=2)
    activity.record(
        conn,
        actor_id=1,
        verb="changed_status",
        target_kind="issue",
        target_id=1,
        detail="open → done",
    )

    feed = activity.list_activity(conn)
    assert [r["verb"] for r in feed] == ["changed_status", "created", "created"]

    one = activity.list_activity(conn, target_kind="issue", target_id=1)
    conn.close()
    assert [r["target_id"] for r in one] == [1, 1]
    assert one[0]["verb"] == "changed_status"


# --- REST feed ------------------------------------------------------------


def test_feed_requires_authentication(tmp_path):
    # WHY: the feed spans every issue and actor at once — a privileged cross-cutting
    # read, gated the same as listing users or searching.
    db_file = tmp_path / "feed_auth.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        assert client.get("/activity").status_code == 401


def test_feed_half_filter_is_422(tmp_path):
    # WHY: a target_kind without a target_id (or vice versa) is an ambiguous
    # half-query; reject it rather than silently ignore one side.
    db_file = tmp_path / "feed_half.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.get("/activity?target_kind=issue", headers={"X-Athena-Actor": "1"})
        assert r.status_code == 422


# --- Wired into the issue lifecycle ---------------------------------------


def test_create_records_activity(tmp_path):
    # WHY: creating an issue is the first audit fact. It must land in the feed,
    # attributed to its creator, with verb "created".
    db_file = tmp_path / "create.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        feed = client.get("/activity", headers={"X-Athena-Actor": "1"}).json()
        assert len(feed) == 1
        assert feed[0]["verb"] == "created"
        assert feed[0]["actor_id"] == 1
        assert feed[0]["target_kind"] == "issue"
        assert feed[0]["target_id"] == issue_id


def test_status_change_records_old_and_new(tmp_path):
    # WHY: the value of the trail is the transition itself — "open → done" as a
    # recorded fact. The detail must capture both ends so history reads precisely.
    db_file = tmp_path / "status.db"
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
        feed = client.get(
            f"/activity?target_kind=issue&target_id={issue_id}",
            headers={"X-Athena-Actor": "1"},
        ).json()
        assert feed[0]["verb"] == "changed_status"
        assert feed[0]["detail"] == "open → done"


def test_noop_edit_records_nothing(tmp_path):
    # WHY: the trail must reflect real change, not API traffic. Editing only the
    # title (or re-setting the same status) records no status event.
    db_file = tmp_path / "noop.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue_id = _make_issue(client)
        client.patch(
            f"/issues/{issue_id}",
            json={"title": "renamed"},
            headers={"X-Athena-Actor": "1"},
        )
        client.patch(
            f"/issues/{issue_id}",
            json={"status": "open"},  # already open — no transition
            headers={"X-Athena-Actor": "1"},
        )
        feed = client.get(
            f"/activity?target_kind=issue&target_id={issue_id}",
            headers={"X-Athena-Actor": "1"},
        ).json()
        # Only the original "created" event — no spurious status rows.
        assert [r["verb"] for r in feed] == ["created"]


def test_assign_and_unassign_record_correct_verbs(tmp_path):
    # WHY: assignment is a tracked responsibility change. Assigning records the new
    # owner's name; clearing records "unassigned" with no detail.
    db_file = tmp_path / "assign.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file, email="ann@example.com", name="Ann")
        issue_id = _make_issue(client)
        client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 1},
            headers={"X-Athena-Actor": "1"},
        )
        client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": None},
            headers={"X-Athena-Actor": "1"},
        )
        feed = client.get(
            f"/activity?target_kind=issue&target_id={issue_id}",
            headers={"X-Athena-Actor": "1"},
        ).json()
        # newest first: unassigned, assigned, created
        assert [r["verb"] for r in feed] == ["unassigned", "assigned", "created"]
        assigned = feed[1]
        assert assigned["detail"] == "Ann"


def test_activity_attributes_close_to_the_actor_not_the_creator(tmp_path):
    # WHY: this is the whole promise — "Grok closed AEGIS-88" as a recorded fact,
    # not a guess. The creator (Kevin) opens and assigns the issue to Grok; when
    # GROK closes it, the status-change event must be stamped with Grok's id, not
    # the creator's. An audit trail that always credits the creator is worthless.
    db_file = tmp_path / "attribution.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file, email="kevin@example.com", name="Kevin")  # id 1
        _seed_user(db_file, email="grok@example.com", name="Grok")  # id 2
        issue_id = _make_issue(client, actor="1")
        # Kevin (creator) assigns the issue to Grok.
        client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 2},
            headers={"X-Athena-Actor": "1"},
        )
        # Grok (now the assignee, so permitted) closes it.
        r = client.patch(
            f"/issues/{issue_id}",
            json={"status": "done"},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 200
        feed = client.get(
            f"/activity?target_kind=issue&target_id={issue_id}",
            headers={"X-Athena-Actor": "1"},
        ).json()
        close = feed[0]
        assert close["verb"] == "changed_status"
        assert close["actor_id"] == 2
        assert close["actor_name"] == "Grok"
