"""Tests for the Aegis issues API: create, list, and the boundary rules.

Issues are created *by* someone. The creator is the actor on the request
(the `X-Athena-Actor` header), not a value the caller puts in the body — so
these tests drive creation through that header.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def _seed_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("kevin@example.com", "Kevin"))
    conn.commit()
    conn.close()


def test_create_then_list_issue(tmp_path):
    db_file = tmp_path / "issues.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)

        created = client.post(
            "/issues", json={"title": "ship it"}, headers={"X-Athena-Actor": "1"}
        )
        assert created.status_code == 201
        body = created.json()
        assert body["title"] == "ship it"
        assert body["status"] == "open"  # the schema DEFAULT applied
        assert body["created_by"] == 1  # stamped from the actor, not the request body
        assert body["id"] == 1

        listing = client.get("/issues")
        assert listing.status_code == 200
        assert [i["title"] for i in listing.json()] == ["ship it"]


def test_missing_actor_header_is_rejected(tmp_path):
    # WHY: an issue must be attributed to someone. No actor, no write.
    db_file = tmp_path / "noactor.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post("/issues", json={"title": "anonymous"})
        assert r.status_code == 401


def test_unknown_actor_is_rejected(tmp_path):
    # WHY: the actor header identifies a real user — a made-up id is not a free
    # pass to write. (It identifies, it doesn't yet authenticate; see identity.py.)
    app = create_app(tmp_path / "bad.db")
    with TestClient(app) as client:
        r = client.post("/issues", json={"title": "orphan"}, headers={"X-Athena-Actor": "999"})
        assert r.status_code == 401


def test_missing_title_is_a_validation_error(tmp_path):
    # WHY: Pydantic rejects malformed input (422) before the handler runs. A
    # valid actor isolates the failure to the body.
    db_file = tmp_path / "v.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post("/issues", json={}, headers={"X-Athena-Actor": "1"})
        assert r.status_code == 422


def test_get_single_issue(tmp_path):
    # WHY: API-first means every issue is addressable on its own URL, not only
    # as a row in the list. GET /issues/{id} is the canonical single read.
    db_file = tmp_path / "one.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        created = client.post(
            "/issues", json={"title": "find me"}, headers={"X-Athena-Actor": "1"}
        ).json()
        r = client.get(f"/issues/{created['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == created["id"]
        assert body["title"] == "find me"
        assert body["labels"] == []  # composed in like every other issue read


def test_get_single_issue_is_open_to_anyone(tmp_path):
    # WHY: reads are open — no actor header required, same contract as the list
    # endpoint. Only writes pass through the creator-or-assignee gate.
    db_file = tmp_path / "open.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        created = client.post(
            "/issues", json={"title": "public read"}, headers={"X-Athena-Actor": "1"}
        ).json()
        r = client.get(f"/issues/{created['id']}")  # no actor header
        assert r.status_code == 200


def test_get_missing_issue_is_404(tmp_path):
    db_file = tmp_path / "missing.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.get("/issues/999")
        assert r.status_code == 404
