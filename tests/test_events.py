"""The event feed (GET /events) — the agent/integration view of the audit trail.

These encode the contract that matters for a *consumer* draining a stream:

  * it is authenticated, like the activity feed (not an open read);
  * events come oldest-first so they can be processed in order;
  * the `after` cursor + `next_after`/`has_more` envelope let a consumer resume
    exactly where it left off and know when to keep draining vs. poll;
  * the same filters as the activity feed narrow the stream (kind/target/verb),
    and a target without its kind is rejected.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def _seed_user(db_file, email="a@e.com", name="A"):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


H = {"X-Athena-Actor": "1"}


def _make_issue(client, title):
    r = client.post("/issues", json={"title": title}, headers=H)
    assert r.status_code == 201
    return r.json()


def test_events_requires_authentication(tmp_path):
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "auth.db")
        # No actor header and no bearer token -> the feed must not be an open read.
        assert client.get("/events").status_code == 401


def test_events_empty_feed(tmp_path):
    app = create_app(tmp_path / "empty.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "empty.db")
        body = client.get("/events", headers=H).json()
        assert body == {"events": [], "next_after": None, "has_more": False}


def test_events_oldest_first_with_cursor(tmp_path):
    app = create_app(tmp_path / "order.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "order.db")
        _make_issue(client, "one")
        _make_issue(client, "two")
        _make_issue(client, "three")

        body = client.get("/events", headers=H).json()
        ids = [e["id"] for e in body["events"]]
        assert ids == sorted(ids)  # ascending
        assert len(ids) == 3
        assert body["has_more"] is False
        assert body["next_after"] == ids[-1]
        # Every event carries the resolved actor name, like the activity feed.
        assert all(e["actor_name"] == "A" for e in body["events"])
        assert [e["verb"] for e in body["events"]] == ["created", "created", "created"]


def test_events_pagination_has_more_and_resume(tmp_path):
    app = create_app(tmp_path / "page.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "page.db")
        for n in range(5):
            _make_issue(client, f"i{n}")

        first = client.get("/events?limit=2", headers=H).json()
        assert len(first["events"]) == 2
        assert first["has_more"] is True

        second = client.get(
            f"/events?limit=2&after={first['next_after']}", headers=H
        ).json()
        # No overlap, still ascending, continues past the first batch.
        assert [e["id"] for e in second["events"]] == [
            first["next_after"] + 1,
            first["next_after"] + 2,
        ]


def test_events_resume_returns_only_new(tmp_path):
    app = create_app(tmp_path / "resume.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "resume.db")
        _make_issue(client, "before")
        caught_up = client.get("/events", headers=H).json()
        cursor = caught_up["next_after"]

        # Caught up: polling from the cursor yields nothing and holds the cursor.
        idle = client.get(f"/events?after={cursor}", headers=H).json()
        assert idle["events"] == []
        assert idle["has_more"] is False
        assert idle["next_after"] == cursor

        # A new action shows up, and only that one, when polling from the cursor.
        _make_issue(client, "after")
        fresh = client.get(f"/events?after={cursor}", headers=H).json()
        assert [e["verb"] for e in fresh["events"]] == ["created"]
        assert fresh["events"][0]["id"] > cursor


def test_events_filter_by_kind(tmp_path):
    app = create_app(tmp_path / "kind.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "kind.db")
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=H).json()
        client.post(f"/spaces/{sp['id']}/pages", json={"title": "Doc"}, headers=H)
        _make_issue(client, "an issue")

        page_events = client.get("/events?kind=page", headers=H).json()["events"]
        assert page_events and all(e["target_kind"] == "page" for e in page_events)
        issue_events = client.get("/events?kind=issue", headers=H).json()["events"]
        assert issue_events and all(e["target_kind"] == "issue" for e in issue_events)


def test_events_filter_by_verb(tmp_path):
    app = create_app(tmp_path / "verb.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "verb.db")
        issue = _make_issue(client, "movable")
        # A status change records a distinct verb alongside the create.
        client.patch(f"/issues/{issue['id']}", json={"status": "in_progress"}, headers=H)

        created = client.get("/events?verb=created", headers=H).json()["events"]
        assert created and all(e["verb"] == "created" for e in created)
        # The non-create verb exists in the unfiltered feed too.
        verbs = {e["verb"] for e in client.get("/events", headers=H).json()["events"]}
        assert "created" in verbs and len(verbs) > 1


def test_events_target_requires_kind(tmp_path):
    app = create_app(tmp_path / "tgt.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "tgt.db")
        r = client.get("/events?target=1", headers=H)
        assert r.status_code == 422


def test_events_filter_one_target(tmp_path):
    app = create_app(tmp_path / "one.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "one.db")
        a = _make_issue(client, "A")
        b = _make_issue(client, "B")
        client.patch(f"/issues/{b['id']}", json={"status": "done"}, headers=H)

        events = client.get(
            f"/events?kind=issue&target={a['id']}", headers=H
        ).json()["events"]
        assert events and all(e["target_id"] == a["id"] for e in events)
