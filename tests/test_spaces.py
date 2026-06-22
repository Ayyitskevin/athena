"""Tests for spaces — the first slice of the Mentor (docs) module.

A space is a top-level container for documentation pages, identified by a short
KEY (e.g. "ENG"). These tests encode the contract:

  * spaces are created by any signed-in actor; an anonymous request is rejected;
  * the key is the canonical identity — normalized to uppercase, case-insensitively
    unique, and re-using one is a 409 not a duplicate;
  * key and name are both required (blank -> 422);
  * the space list is open to read, and a missing space is a 404.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mentor import spaces


def _migrated_conn(db_file):
    """A connection to a freshly-migrated DB, for unit tests that touch the
    data-access layer directly (create_app only migrates inside its lifespan)."""
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('ann@e.com', 'Ann')")
    conn.commit()
    conn.close()


def _create_space(client, key, name, actor="1", description="") -> dict:
    r = client.post(
        "/spaces",
        json={"key": key, "name": name, "description": description},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- unit: data access ----------------------------------------------------


def test_create_and_get_space_round_trip(tmp_path):
    conn = _migrated_conn(tmp_path / "s.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    made = spaces.create_space(
        conn, key="ENG", name="Engineering", description="the eng docs", created_by=1
    )
    got = spaces.get_space(conn, made["id"])
    assert got["key"] == "ENG"
    assert got["name"] == "Engineering"
    assert got["description"] == "the eng docs"
    assert got["created_by"] == 1


def test_get_space_by_key_is_case_insensitive(tmp_path):
    conn = _migrated_conn(tmp_path / "sk.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    spaces.create_space(conn, key="OPS", name="Operations", created_by=1)
    assert spaces.get_space_by_key(conn, "ops")["key"] == "OPS"


def test_list_spaces_is_alphabetical_by_name(tmp_path):
    conn = _migrated_conn(tmp_path / "sl.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    spaces.create_space(conn, key="Z", name="Zebra", created_by=1)
    spaces.create_space(conn, key="A", name="apple", created_by=1)
    assert [s["name"] for s in spaces.list_spaces(conn)] == ["apple", "Zebra"]


# --- API ------------------------------------------------------------------


def test_create_and_show_space_via_api(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "api.db")
        made = _create_space(client, "ENG", "Engineering")
        assert made["key"] == "ENG"
        assert made["created_by"] == 1
        got = client.get(f"/spaces/{made['id']}")
        assert got.status_code == 200
        assert got.json()["name"] == "Engineering"


def test_list_spaces_via_api_is_open(tmp_path):
    app = create_app(tmp_path / "list.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "list.db")
        _create_space(client, "ENG", "Engineering")
        # No actor header — reads are open, like listing issues/projects.
        r = client.get("/spaces")
        assert r.status_code == 200
        assert [s["key"] for s in r.json()] == ["ENG"]


def test_create_space_requires_an_actor(tmp_path):
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "auth.db")
        r = client.post("/spaces", json={"key": "ENG", "name": "Engineering"})
        assert r.status_code == 401


def test_key_is_normalized_to_uppercase(tmp_path):
    app = create_app(tmp_path / "up.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "up.db")
        made = _create_space(client, "eng", "Engineering")
        # Stored uppercase so URLs and lookups are predictable regardless of input.
        assert made["key"] == "ENG"


def test_duplicate_key_is_409_case_insensitive(tmp_path):
    app = create_app(tmp_path / "dup.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "dup.db")
        _create_space(client, "ENG", "Engineering")
        # Same key in a different case, different display name -> still a conflict,
        # because the key (not the name) is the identity.
        r = client.post(
            "/spaces",
            json={"key": "eng", "name": "Something Else"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 409


def test_blank_key_or_name_is_422(tmp_path):
    app = create_app(tmp_path / "blank.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "blank.db")
        blank_key = client.post(
            "/spaces",
            json={"key": "   ", "name": "Engineering"},
            headers={"X-Athena-Actor": "1"},
        )
        assert blank_key.status_code == 422
        blank_name = client.post(
            "/spaces",
            json={"key": "ENG", "name": "  "},
            headers={"X-Athena-Actor": "1"},
        )
        assert blank_name.status_code == 422


def test_missing_space_is_404(tmp_path):
    app = create_app(tmp_path / "miss.db")
    with TestClient(app) as client:
        r = client.get("/spaces/999")
        assert r.status_code == 404
