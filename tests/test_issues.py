"""Tests for the Aegis issues API: create, list, and the boundary rules."""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def _seed_user(db_file):
    # No user API yet, so seed one directly for issues to reference.
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("kevin@example.com", "Kevin"))
    conn.commit()
    conn.close()


def test_create_then_list_issue(tmp_path):
    db_file = tmp_path / "issues.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)

        created = client.post("/issues", json={"title": "ship it", "created_by": 1})
        assert created.status_code == 201
        body = created.json()
        assert body["title"] == "ship it"
        assert body["status"] == "open"  # the schema DEFAULT applied
        assert body["id"] == 1

        listing = client.get("/issues")
        assert listing.status_code == 200
        assert [i["title"] for i in listing.json()] == ["ship it"]


def test_create_issue_for_unknown_user_is_rejected(tmp_path):
    # WHY: an issue must belong to a real user. The foreign key catches it in
    # the DB; the API turns that into a clean 400 instead of a 500.
    app = create_app(tmp_path / "bad.db")
    with TestClient(app) as client:
        r = client.post("/issues", json={"title": "orphan", "created_by": 999})
        assert r.status_code == 400


def test_missing_title_is_a_validation_error(tmp_path):
    # WHY: Pydantic rejects malformed input (422) before our handler runs.
    app = create_app(tmp_path / "v.db")
    with TestClient(app) as client:
        r = client.post("/issues", json={"created_by": 1})
        assert r.status_code == 422
