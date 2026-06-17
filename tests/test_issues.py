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
