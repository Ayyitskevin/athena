"""Editing a Mentor space — completing the space CRUD (create/read/edit/delete).

The contract these encode — why each rule earns its place:

  * edit is a PARTIAL write: only the fields sent change, the rest are left as-is
    (mirrors page edit and aegis project edit);
  * the key is normalized to UPPERCASE, exactly like create, so "eng" and "ENG"
    can never become two identities;
  * a key change that would collide with a DIFFERENT space is refused (409), but
    re-submitting a space's OWN key is fine — the collision check excludes self;
  * edit is OPEN to any signed-in actor — NOT creator-locked the way delete is.
    Editing a name/description is non-destructive and reversible, the same shared-
    wiki model as editing a page or moving it; only DELETE keeps the creator lock.
    Anonymous -> 401, missing space -> 404, empty key/name -> 422.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mentor import spaces


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(conn, email="a@e.com", name="A"):
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()


# --- unit: data access ------------------------------------------------------


def test_update_space_changes_only_the_fields_passed(tmp_path):
    # WHY: a partial edit must leave untouched fields alone — passing a new name
    # must not blank the key or description.
    conn = _migrated_conn(tmp_path / "u.db")
    _seed_user(conn)
    sp = spaces.create_space(
        conn, key="ENG", name="Eng", description="docs", created_by=1
    )
    updated = spaces.update_space(conn, sp["id"], name="Engineering")
    assert updated["name"] == "Engineering"
    assert updated["key"] == "ENG"  # untouched
    assert updated["description"] == "docs"  # untouched


def test_update_space_uppercases_the_key(tmp_path):
    # WHY: same identity rule as create — a lowercase key must land uppercased so
    # "eng" and "ENG" can never split into two spaces.
    conn = _migrated_conn(tmp_path / "uk.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    assert spaces.update_space(conn, sp["id"], key="ops")["key"] == "OPS"


def test_update_missing_space_returns_none(tmp_path):
    conn = _migrated_conn(tmp_path / "um.db")
    assert spaces.update_space(conn, 999, name="x") is None


def test_update_space_no_fields_is_a_noop(tmp_path):
    # WHY: with nothing to change we return the space untouched (mirrors
    # update_project / update_page) — no needless write.
    conn = _migrated_conn(tmp_path / "un.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    assert spaces.update_space(conn, sp["id"])["name"] == "Eng"


def test_update_space_to_a_taken_key_raises(tmp_path):
    # WHY: the UNIQUE key is the data layer's last line of defense — even if the
    # boundary's pre-check were skipped, the DB refuses a duplicate rather than
    # creating two spaces that share an identity.
    conn = _migrated_conn(tmp_path / "ud.db")
    _seed_user(conn)
    spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    ops = spaces.create_space(conn, key="OPS", name="Ops", created_by=1)
    with pytest.raises(sqlite3.IntegrityError):
        spaces.update_space(conn, ops["id"], key="ENG")


# --- API --------------------------------------------------------------------


def _api_setup(tmp_path, name):
    """A migrated app with two users seeded (1 = creator, 2 = bystander)."""
    app = create_app(tmp_path / name)
    with TestClient(app):
        pass
    conn = db.connect(tmp_path / name)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO users (email, name) VALUES ('b@e.com', 'B')")
    conn.commit()
    conn.close()
    return app


_H = {"X-Athena-Actor": "1"}


def _make_space(client, *, key="ENG", name="Eng", actor="1") -> dict:
    return client.post(
        "/spaces", json={"key": key, "name": name}, headers={"X-Athena-Actor": actor}
    ).json()


def test_api_edit_space_updates_fields(tmp_path):
    app = _api_setup(tmp_path, "ae.db")
    with TestClient(app) as client:
        sp = _make_space(client)
        r = client.patch(
            f"/spaces/{sp['id']}", json={"name": "Engineering"}, headers=_H
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Engineering"
        assert r.json()["key"] == "ENG"


def test_api_edit_is_open_to_any_actor(tmp_path):
    # WHY: editing is NOT creator-locked — a different signed-in actor may edit a
    # space, exactly like they may edit a page. Only delete is creator-only.
    app = _api_setup(tmp_path, "aeo.db")
    with TestClient(app) as client:
        sp = _make_space(client, actor="1")  # created by user 1
        r = client.patch(
            f"/spaces/{sp['id']}",
            json={"name": "By Bob"},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "By Bob"


def test_api_edit_missing_is_404(tmp_path):
    app = _api_setup(tmp_path, "aem.db")
    with TestClient(app) as client:
        assert (
            client.patch("/spaces/999", json={"name": "x"}, headers=_H).status_code
            == 404
        )


def test_api_edit_empty_payload_is_422(tmp_path):
    app = _api_setup(tmp_path, "aep.db")
    with TestClient(app) as client:
        sp = _make_space(client)
        assert (
            client.patch(f"/spaces/{sp['id']}", json={}, headers=_H).status_code == 422
        )


def test_api_edit_empty_name_is_422(tmp_path):
    app = _api_setup(tmp_path, "aen.db")
    with TestClient(app) as client:
        sp = _make_space(client)
        r = client.patch(f"/spaces/{sp['id']}", json={"name": "   "}, headers=_H)
        assert r.status_code == 422


def test_api_edit_to_a_taken_key_is_409(tmp_path):
    # WHY: a clean 409, not a 500 — the boundary catches the clash before the DB does.
    app = _api_setup(tmp_path, "aek.db")
    with TestClient(app) as client:
        _make_space(client, key="ENG", name="Eng")
        ops = _make_space(client, key="OPS", name="Ops")
        r = client.patch(f"/spaces/{ops['id']}", json={"key": "ENG"}, headers=_H)
        assert r.status_code == 409


def test_api_edit_keeping_own_key_is_not_a_collision(tmp_path):
    # WHY: re-submitting a space's OWN key must succeed — the collision check
    # excludes self, or you could never edit the name while leaving the key shown.
    app = _api_setup(tmp_path, "aes.db")
    with TestClient(app) as client:
        sp = _make_space(client, key="ENG", name="Eng")
        r = client.patch(
            f"/spaces/{sp['id']}",
            json={"key": "ENG", "name": "Engineering"},
            headers=_H,
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Engineering"


def test_api_edit_requires_auth(tmp_path):
    app = _api_setup(tmp_path, "aea.db")
    with TestClient(app) as client:
        sp = _make_space(client)
        assert (
            client.patch(f"/spaces/{sp['id']}", json={"name": "x"}).status_code == 401
        )


# --- web --------------------------------------------------------------------


def _login(client, email="a@e.com", name="A"):
    client.post(
        "/users", json={"email": email, "name": name, "password": "secret"}, headers=_H
    )
    client.post("/login", data={"email": email, "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_edit_form_requires_login(tmp_path):
    app = create_app(tmp_path / "we.db")
    with TestClient(app) as client:
        _login(client)
        sp = _make_space(client)
    with TestClient(app) as anon:
        assert anon.get(f"/mentor/spaces/{sp['id']}/edit").status_code == 401


def test_web_edit_space_redirects_and_saves(tmp_path):
    app = create_app(tmp_path / "wes.db")
    with TestClient(app) as client:
        _login(client)
        sp = _make_space(client)
        r = client.post(
            f"/mentor/spaces/{sp['id']}/edit",
            data={"key": "ENG", "name": "Engineering", "description": "all the docs"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"/mentor/spaces/{sp['id']}"
        got = client.get(f"/spaces/{sp['id']}").json()
        assert got["name"] == "Engineering"
        assert got["description"] == "all the docs"


def test_web_edit_open_to_a_non_creator(tmp_path):
    # WHY: the browser path mirrors the API — any signed-in actor may edit, not
    # just the creator (unlike delete, which the UI and API both creator-lock).
    app = create_app(tmp_path / "wen.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 = creator
        sp = _make_space(client)
        _login(client, "bob@e.com", "Bob")  # user 2 now the session — bystander
        r = client.post(
            f"/mentor/spaces/{sp['id']}/edit",
            data={"key": "ENG", "name": "Bob Was Here", "description": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert client.get(f"/spaces/{sp['id']}").json()["name"] == "Bob Was Here"


def test_web_edit_to_a_taken_key_is_409(tmp_path):
    app = create_app(tmp_path / "wek.db")
    with TestClient(app) as client:
        _login(client)
        _make_space(client, key="ENG", name="Eng")
        ops = _make_space(client, key="OPS", name="Ops")
        r = client.post(
            f"/mentor/spaces/{ops['id']}/edit",
            data={"key": "ENG", "name": "Ops", "description": ""},
        )
        assert r.status_code == 409
        assert client.get(f"/spaces/{ops['id']}").json()["key"] == "OPS"  # unchanged


def test_web_space_detail_shows_edit_only_when_logged_in(tmp_path):
    # WHY: Edit is a write control — a signed-in reader sees it (edit is open to
    # any actor), an anonymous reader must not.
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        _login(client)
        sp = _make_space(client)
        assert ">Edit</a>" in client.get(f"/mentor/spaces/{sp['id']}").text
    with TestClient(app) as anon:
        assert ">Edit</a>" not in anon.get(f"/mentor/spaces/{sp['id']}").text
