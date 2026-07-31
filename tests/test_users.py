"""Tests for the core users API: create, fetch, list, and the boundary rules.

User management is authenticated after the first (bootstrap) user; the suite
runs with the X-Athena-Actor fallback enabled (see conftest), so post-bootstrap
calls authenticate as user 1.
"""

from fastapi.testclient import TestClient

from athena.main import create_app

# Authenticate as the bootstrap user (id 1) for endpoints gated after bootstrap.
_AUTH = {"X-Athena-Actor": "1"}


def test_create_then_fetch_and_list_user(tmp_path):
    app = create_app(tmp_path / "users.db")
    with TestClient(app) as client:
        # First user: the suite fixture supplies the bootstrap transport grant.
        created = client.post(
            "/users", json={"email": "kevin@example.com", "name": "Kevin"}
        )
        assert created.status_code == 201
        body = created.json()
        assert body["email"] == "kevin@example.com"
        assert body["id"] == 1
        assert body["created_at"]  # the schema DEFAULT stamped a time

        fetched = client.get(f"/users/{body['id']}", headers=_AUTH)
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Kevin"

        listing = client.get("/users", headers=_AUTH)
        assert listing.status_code == 200
        assert [u["email"] for u in listing.json()] == ["kevin@example.com"]


def test_duplicate_email_is_rejected(tmp_path):
    # WHY: email is a user's identity here — two users sharing one would make
    # "who acted?" ambiguous. The UNIQUE constraint catches it in the DB; the
    # API turns that into a clean 400 instead of a 500.
    app = create_app(tmp_path / "dup.db")
    with TestClient(app) as client:
        first = client.post(
            "/users", json={"email": "dupe@example.com", "name": "First"}
        )
        assert first.status_code == 201
        # Second create is post-bootstrap, so it authenticates as user 1.
        second = client.post(
            "/users",
            json={"email": "dupe@example.com", "name": "Second"},
            headers=_AUTH,
        )
        assert second.status_code == 400


def test_unknown_user_is_404(tmp_path):
    # WHY: asking for a user that doesn't exist is a clean "not found", not a crash.
    app = create_app(tmp_path / "missing.db")
    with TestClient(app) as client:
        client.post(
            "/users", json={"email": "boot@example.com", "name": "Boot"}
        )  # bootstrap
        assert client.get("/users/999", headers=_AUTH).status_code == 404


def test_missing_email_is_a_validation_error(tmp_path):
    # WHY: Pydantic rejects malformed input (422) before our handler runs.
    app = create_app(tmp_path / "v.db")
    with TestClient(app) as client:
        assert client.post("/users", json={"name": "No Email"}).status_code == 422
