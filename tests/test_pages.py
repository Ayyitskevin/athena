"""Tests for pages — documents inside a Mentor space (the docs unit of value).

A page belongs to one space and may nest under a parent page in the SAME space.
These tests encode the contract:

  * pages are created by any signed-in actor (anonymous -> 401), inside a real
    space (unknown space -> 404);
  * a blank title is rejected (422);
  * a parent must be a real page in the same space — a missing parent or one from
    another space is a 422, never an FK 500 or a cross-space tree;
  * reads are open: list a space's pages (flat, alphabetical, [] when empty) and
    fetch a single page on its own URL (missing -> 404).
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mentor import pages, spaces


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('ann@e.com', 'Ann')")
    conn.commit()
    conn.close()


def _make_space(client, key="ENG", name="Engineering", actor="1") -> dict:
    r = client.post(
        "/spaces",
        json={"key": key, "name": name},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_page(client, space_id, actor="1", **body) -> dict:
    r = client.post(
        f"/spaces/{space_id}/pages",
        json={"title": "Home", **body},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- unit: data access ----------------------------------------------------


def test_create_and_get_page_round_trip(tmp_path):
    conn = _migrated_conn(tmp_path / "p.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    space = spaces.create_space(conn, key="ENG", name="Engineering", created_by=1)
    made = pages.create_page(
        conn, space_id=space["id"], title="Runbook", body="steps", created_by=1
    )
    got = pages.get_page(conn, made["id"])
    assert got["title"] == "Runbook"
    assert got["body"] == "steps"
    assert got["space_id"] == space["id"]
    assert got["parent_id"] is None


def test_list_pages_in_space_is_alphabetical(tmp_path):
    conn = _migrated_conn(tmp_path / "pl.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    space = spaces.create_space(conn, key="ENG", name="Engineering", created_by=1)
    pages.create_page(conn, space_id=space["id"], title="Zebra", created_by=1)
    pages.create_page(conn, space_id=space["id"], title="apple", created_by=1)
    assert [p["title"] for p in pages.list_pages_in_space(conn, space["id"])] == [
        "apple",
        "Zebra",
    ]


def test_child_page_records_its_parent(tmp_path):
    conn = _migrated_conn(tmp_path / "tree.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    space = spaces.create_space(conn, key="ENG", name="Engineering", created_by=1)
    parent = pages.create_page(conn, space_id=space["id"], title="Parent", created_by=1)
    child = pages.create_page(
        conn, space_id=space["id"], title="Child", parent_id=parent["id"], created_by=1
    )
    assert child["parent_id"] == parent["id"]


# --- API ------------------------------------------------------------------


def test_create_list_and_show_page_via_api(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "api.db")
        space = _make_space(client)
        made = _make_page(client, space["id"], title="Runbook", body="steps")
        assert made["space_id"] == space["id"]
        assert made["created_by"] == 1
        # listed under its space
        listed = client.get(f"/spaces/{space['id']}/pages")
        assert listed.status_code == 200
        assert [p["title"] for p in listed.json()] == ["Runbook"]
        # addressable on its own URL
        got = client.get(f"/pages/{made['id']}")
        assert got.status_code == 200
        assert got.json()["body"] == "steps"


def test_empty_space_lists_no_pages(tmp_path):
    app = create_app(tmp_path / "empty.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "empty.db")
        space = _make_space(client)
        r = client.get(f"/spaces/{space['id']}/pages")
        assert r.status_code == 200
        assert r.json() == []


def test_create_page_requires_an_actor(tmp_path):
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "auth.db")
        space = _make_space(client)
        r = client.post(f"/spaces/{space['id']}/pages", json={"title": "Home"})
        assert r.status_code == 401


def test_create_page_in_unknown_space_is_404(tmp_path):
    app = create_app(tmp_path / "nospace.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "nospace.db")
        r = client.post(
            "/spaces/999/pages",
            json={"title": "Home"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 404


def test_blank_title_is_422(tmp_path):
    app = create_app(tmp_path / "blank.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "blank.db")
        space = _make_space(client)
        r = client.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "   "},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_nest_under_parent_in_same_space(tmp_path):
    app = create_app(tmp_path / "nest.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "nest.db")
        space = _make_space(client)
        parent = _make_page(client, space["id"], title="Parent")
        child = _make_page(client, space["id"], title="Child", parent_id=parent["id"])
        assert child["parent_id"] == parent["id"]


def test_parent_from_another_space_is_422(tmp_path):
    app = create_app(tmp_path / "cross.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "cross.db")
        eng = _make_space(client, key="ENG", name="Engineering")
        ops = _make_space(client, key="OPS", name="Operations")
        # A page that lives in OPS...
        ops_page = _make_page(client, ops["id"], title="Ops Home")
        # ...cannot be the parent of a page created in ENG (tree can't span spaces).
        r = client.post(
            f"/spaces/{eng['id']}/pages",
            json={"title": "Child", "parent_id": ops_page["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_unknown_parent_is_422(tmp_path):
    app = create_app(tmp_path / "noparent.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "noparent.db")
        space = _make_space(client)
        r = client.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Child", "parent_id": 999},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_missing_page_is_404(tmp_path):
    app = create_app(tmp_path / "miss.db")
    with TestClient(app) as client:
        r = client.get("/pages/999")
        assert r.status_code == 404
