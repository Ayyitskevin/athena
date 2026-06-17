"""Tests for the core users API: create, fetch, list, and the boundary rules."""
from fastapi.testclient import TestClient

from athena.main import create_app


def test_create_then_fetch_and_list_user(tmp_path):
    app = create_app(tmp_path / "users.db")
    with TestClient(app) as client:
        created = client.post("/users", json={"email": "kevin@example.com", "name": "Kevin"})
        assert created.status_code == 201
        body = created.json()
        assert body["email"] == "kevin@example.com"
        assert body["id"] == 1
        assert body["created_at"]  # the schema DEFAULT stamped a time

        fetched = client.get(f"/users/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Kevin"

        listing = client.get("/users")
        assert listing.status_code == 200
        assert [u["email"] for u in listing.json()] == ["kevin@example.com"]


def test_duplicate_email_is_rejected(tmp_path):
    # WHY: email is a user's identity here — two users sharing one would make
    # "who acted?" ambiguous. The UNIQUE constraint catches it in the DB; the
    # API turns that into a clean 400 instead of a 500.
    app = create_app(tmp_path / "dup.db")
    with TestClient(app) as client:
        first = client.post("/users", json={"email": "dupe@example.com", "name": "First"})
        assert first.status_code == 201
        second = client.post("/users", json={"email": "dupe@example.com", "name": "Second"})
        assert second.status_code == 400


def test_unknown_user_is_404(tmp_path):
    # WHY: asking for a user that doesn't exist is a clean "not found", not a crash.
    app = create_app(tmp_path / "missing.db")
    with TestClient(app) as client:
        assert client.get("/users/999").status_code == 404


def test_missing_email_is_a_validation_error(tmp_path):
    # WHY: Pydantic rejects malformed input (422) before our handler runs.
    app = create_app(tmp_path / "v.db")
    with TestClient(app) as client:
        assert client.post("/users", json={"name": "No Email"}).status_code == 422
