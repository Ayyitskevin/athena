"""Deleting Mentor spaces — the destructive path for a whole documentation container.

Symmetric to project-delete (Aegis) and page-delete (Mentor), with two rules that
earn their place here:

  * a space that still holds pages is REFUSED (409), never cascade-deleted: one
    click must not wipe a tree of documents. Delete or move the pages out first;
  * delete is CREATOR-ONLY — tighter than Mentor's otherwise-open write model (any
    signed-in actor may create spaces and edit pages). Removing a container is
    destructive and, unlike a page edit, isn't recorded in any history, so only the
    creator may do it (401 anon, 403 non-creator, 404 missing).
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mentor import pages, spaces


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(conn, email="a@e.com", name="A"):
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()


# --- unit: data access ------------------------------------------------------


def test_delete_space_removes_the_row(tmp_path):
    conn = _migrated_conn(tmp_path / "del.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    assert spaces.delete_space(conn, sp["id"]) is True
    assert spaces.get_space(conn, sp["id"]) is None


def test_delete_missing_space_returns_false(tmp_path):
    conn = _migrated_conn(tmp_path / "delm.db")
    assert spaces.delete_space(conn, 999) is False


def test_count_pages_in_space_counts_at_any_depth(tmp_path):
    # WHY: the block rule must see EVERY page the space holds, not just top-level
    # ones — a nested page is still data that would be orphaned by a cascade.
    conn = _migrated_conn(tmp_path / "cnt.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    assert pages.count_pages_in_space(conn, sp["id"]) == 0
    parent = pages.create_page(conn, space_id=sp["id"], title="Parent", body="", created_by=1)
    pages.create_page(
        conn, space_id=sp["id"], title="Child", body="", parent_id=parent["id"], created_by=1
    )
    assert pages.count_pages_in_space(conn, sp["id"]) == 2
    # a page in ANOTHER space doesn't count toward this one
    other = spaces.create_space(conn, key="OPS", name="Ops", created_by=1)
    pages.create_page(conn, space_id=other["id"], title="Elsewhere", body="", created_by=1)
    assert pages.count_pages_in_space(conn, sp["id"]) == 2


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


def test_api_delete_space(tmp_path):
    app = _api_setup(tmp_path, "ad.db")
    with TestClient(app) as client:
        sp = _make_space(client)
        r = client.delete(f"/spaces/{sp['id']}", headers=_H)
        assert r.status_code == 204
        assert client.get(f"/spaces/{sp['id']}").status_code == 404


def test_api_delete_missing_is_404(tmp_path):
    app = _api_setup(tmp_path, "adm.db")
    with TestClient(app) as client:
        assert client.delete("/spaces/999", headers=_H).status_code == 404


def test_api_delete_with_pages_is_409(tmp_path):
    # WHY: no silent tree wipe — refuse, and leave the space (and its pages) intact.
    app = _api_setup(tmp_path, "adp.db")
    with TestClient(app) as client:
        sp = _make_space(client)
        client.post(f"/spaces/{sp['id']}/pages", json={"title": "Doc"}, headers=_H)
        r = client.delete(f"/spaces/{sp['id']}", headers=_H)
        assert r.status_code == 409
        assert client.get(f"/spaces/{sp['id']}").status_code == 200  # still there


def test_api_delete_requires_auth(tmp_path):
    app = _api_setup(tmp_path, "ada.db")
    with TestClient(app) as client:
        sp = _make_space(client)
        assert client.delete(f"/spaces/{sp['id']}").status_code == 401


def test_api_delete_non_creator_is_403(tmp_path):
    # WHY: creator-only — a different signed-in actor (who CAN create spaces and
    # edit pages) still may not delete someone else's space.
    app = _api_setup(tmp_path, "adn.db")
    with TestClient(app) as client:
        sp = _make_space(client, actor="1")  # created by user 1
        r = client.delete(f"/spaces/{sp['id']}", headers={"X-Athena-Actor": "2"})
        assert r.status_code == 403
        assert client.get(f"/spaces/{sp['id']}").status_code == 200  # untouched


# --- web --------------------------------------------------------------------


def _login(client, email="a@e.com", name="A"):
    client.post("/users", json={"email": email, "name": name, "password": "secret"}, headers=_H)
    client.post("/login", data={"email": email, "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_delete_space_requires_login(tmp_path):
    app = create_app(tmp_path / "wdl.db")
    with TestClient(app) as client:
        _login(client)
        sp = _make_space(client)
    with TestClient(app) as anon:
        assert anon.post(f"/mentor/spaces/{sp['id']}/delete").status_code == 401


def test_web_delete_space_redirects_to_mentor(tmp_path):
    app = create_app(tmp_path / "wdr.db")
    with TestClient(app) as client:
        _login(client)  # user 1 = creator
        sp = _make_space(client)
        r = client.post(f"/mentor/spaces/{sp['id']}/delete", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/mentor"
        assert client.get(f"/spaces/{sp['id']}").status_code == 404


def test_web_delete_space_with_pages_is_409(tmp_path):
    app = create_app(tmp_path / "wdp.db")
    with TestClient(app) as client:
        _login(client)
        sp = _make_space(client)
        client.post(f"/spaces/{sp['id']}/pages", json={"title": "Doc"}, headers=_H)
        r = client.post(f"/mentor/spaces/{sp['id']}/delete")
        assert r.status_code == 409
        assert client.get(f"/spaces/{sp['id']}").status_code == 200


def test_web_delete_space_non_creator_is_403(tmp_path):
    # WHY: the browser path enforces the same creator-only gate as the API.
    app = create_app(tmp_path / "wdn.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 = creator
        sp = _make_space(client)
        _login(client, "bob@e.com", "Bob")  # user 2 now the session — bystander
        r = client.post(f"/mentor/spaces/{sp['id']}/delete")
        assert r.status_code == 403
        assert client.get(f"/spaces/{sp['id']}").status_code == 200


def test_web_space_detail_shows_delete_only_to_creator(tmp_path):
    # WHY: the danger zone is creator-gated in the UI too — a non-creator (or an
    # anon) viewing the space must not even see the Delete control.
    app = create_app(tmp_path / "wdc.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 = creator
        sp = _make_space(client)
        creator_view = client.get(f"/mentor/spaces/{sp['id']}").text
        assert "Delete Space" in creator_view

        _login(client, "bob@e.com", "Bob")  # user 2 — not the creator
        bystander_view = client.get(f"/mentor/spaces/{sp['id']}").text
        assert "Delete Space" not in bystander_view
