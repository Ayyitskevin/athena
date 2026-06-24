"""Deleting and moving Mentor pages — the first destructive path in Mentor.

The contract these encode — why each rule earns its place:

  * deleting a page removes its history AND its derived index entries (search,
    outgoing links) too, so nothing is left pointing at a ghost — the derived
    state never outlives the row it describes;
  * a page with children is REFUSED (409), never cascade-deleted: a wiki must not
    let one click wipe a subtree of documents. Move the children out first;
  * inbound references from OTHER pages survive as broken links, not errors — the
    same lazy-resolve contract a not-yet-created target gets;
  * moving re-parents within the SAME space and can never form a cycle (a page
    under its own descendant would detach a loop from the root);
  * both are writes: gated on a real actor (401 anon) against a real page (404).
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena.core import db, links, search
from athena.main import create_app
from athena.mentor import pages, spaces


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(conn):
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()


# --- unit: delete -----------------------------------------------------------


def test_delete_removes_page_and_its_history_and_indexes(tmp_path):
    # WHY: the page is the truth; everything derived from it (versions, search,
    # outgoing links) must go with it, or the index would describe a dead row.
    conn = _migrated_conn(tmp_path / "del.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    pg = pages.create_page(
        conn, space_id=sp["id"], title="Zebra notes", body="see [[issue:1]] zebra", created_by=1
    )
    # cut a version, but keep the [[issue:1]] link in the new body so the
    # outgoing-link precondition below is about delete, not about the edit.
    pages.update_page(conn, pg["id"], editor_id=1, body="zebra revised see [[issue:1]]")
    # Preconditions: it's indexed and has history + an outgoing link.
    assert [h["source_id"] for h in search.search(conn, "zebra")] == [pg["id"]]
    assert pages.list_page_versions(conn, pg["id"]) != []
    assert links.outgoing_links(conn, source_kind="page", source_id=pg["id"]) != []

    assert pages.delete_page(conn, pg["id"]) is True

    assert pages.get_page(conn, pg["id"]) is None
    assert pages.list_page_versions(conn, pg["id"]) == []
    assert search.search(conn, "zebra") == []
    assert links.outgoing_links(conn, source_kind="page", source_id=pg["id"]) == []


def test_delete_missing_page_returns_false(tmp_path):
    conn = _migrated_conn(tmp_path / "delm.db")
    _seed_user(conn)
    assert pages.delete_page(conn, 999) is False


def test_delete_is_atomic_history_survives_a_failed_page_delete(tmp_path):
    # WHY: delete clears history (page_versions) BEFORE the page row, because the
    # FK forces that order. If the page delete then fails — a stray child's
    # parent_id FK restricts it — a non-atomic delete would leave history gone for
    # a page that still exists. The BEGIN IMMEDIATE + rollback must restore both.
    # The route guards against children up front; this drives the precondition
    # violation directly to prove the data-access layer itself can't half-delete.
    conn = _migrated_conn(tmp_path / "delatomic.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    parent = pages.create_page(conn, space_id=sp["id"], title="Parent", body="v1", created_by=1)
    pages.update_page(conn, parent["id"], editor_id=1, body="v2")  # cut version 1
    pages.create_page(
        conn, space_id=sp["id"], title="Child", body="", created_by=1, parent_id=parent["id"]
    )
    assert pages.list_page_versions(conn, parent["id"]) != []  # history exists

    # The child's parent_id REFERENCES pages(id) restricts the parent delete.
    with pytest.raises(sqlite3.IntegrityError):
        pages.delete_page(conn, parent["id"])

    # Rollback put it all back: the page is still here AND so is its history.
    assert pages.get_page(conn, parent["id"]) is not None
    assert pages.list_page_versions(conn, parent["id"]) != []


def test_inbound_links_survive_delete_as_broken(tmp_path):
    # WHY: another page that referenced the deleted one shouldn't error — the ref
    # just resolves "broken", exactly like a reference to something not yet made.
    conn = _migrated_conn(tmp_path / "delin.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    target = pages.create_page(conn, space_id=sp["id"], title="Target", body="", created_by=1)
    referrer = pages.create_page(
        conn, space_id=sp["id"], title="Referrer",
        body=f"see [[page:{target['id']}]]", created_by=1
    )
    pages.delete_page(conn, target["id"])
    # The referrer's link row still exists but now resolves as broken (not found).
    out = links.outgoing_links(conn, source_kind="page", source_id=referrer["id"])
    assert out and out[0]["exists"] is False


def test_count_child_pages(tmp_path):
    conn = _migrated_conn(tmp_path / "cnt.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    parent = pages.create_page(conn, space_id=sp["id"], title="Parent", body="", created_by=1)
    assert pages.count_child_pages(conn, parent["id"]) == 0
    pages.create_page(conn, space_id=sp["id"], title="Kid", body="", parent_id=parent["id"], created_by=1)
    assert pages.count_child_pages(conn, parent["id"]) == 1


# --- unit: move / validate_move --------------------------------------------


def _tree(conn):
    """A 3-deep chain A -> B -> C in one space, plus a sibling D under root."""
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    a = pages.create_page(conn, space_id=sp["id"], title="A", body="", created_by=1)
    b = pages.create_page(conn, space_id=sp["id"], title="B", body="", parent_id=a["id"], created_by=1)
    c = pages.create_page(conn, space_id=sp["id"], title="C", body="", parent_id=b["id"], created_by=1)
    d = pages.create_page(conn, space_id=sp["id"], title="D", body="", created_by=1)
    return sp, a, b, c, d


def test_move_to_root_and_under_sibling_is_allowed(tmp_path):
    conn = _migrated_conn(tmp_path / "mv.db")
    sp, a, b, c, d = _tree(conn)
    assert pages.validate_move(conn, b, None) is None  # to top level
    assert pages.validate_move(conn, b, d["id"]) is None  # under an unrelated page
    moved = pages.set_parent(conn, b["id"], d["id"])
    assert moved["parent_id"] == d["id"]


def test_move_rejects_self_and_descendant_cycle(tmp_path):
    # WHY: a page under itself or its own descendant detaches a cycle from the root
    # — those pages would vanish from every space tree (the DFS never reaches them).
    conn = _migrated_conn(tmp_path / "cyc.db")
    sp, a, b, c, d = _tree(conn)
    assert pages.validate_move(conn, a, a["id"]) is not None  # self
    assert pages.validate_move(conn, a, b["id"]) is not None  # direct child
    assert pages.validate_move(conn, a, c["id"]) is not None  # deeper descendant


def test_move_rejects_cross_space_parent(tmp_path):
    conn = _migrated_conn(tmp_path / "xsp.db")
    sp, a, b, c, d = _tree(conn)
    other = spaces.create_space(conn, key="OPS", name="Ops", created_by=1)
    foreign = pages.create_page(conn, space_id=other["id"], title="Foreign", body="", created_by=1)
    assert pages.validate_move(conn, a, foreign["id"]) is not None


# --- API --------------------------------------------------------------------


def _api_setup(tmp_path, name):
    app = create_app(tmp_path / name)
    # Migration runs on the app's startup event; enter/exit a client once so the
    # tables exist before we seed a user via a direct connection.
    with TestClient(app):
        pass
    conn = db.connect(tmp_path / name)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    conn.close()
    return app


_H = {"X-Athena-Actor": "1"}


def _make_space_and_page(client, *, title="Doc", body="", parent_id=None):
    sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=_H).json()
    payload = {"title": title, "body": body}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    pg = client.post(f"/spaces/{sp['id']}/pages", json=payload, headers=_H).json()
    return sp, pg


def test_api_delete_page(tmp_path):
    app = _api_setup(tmp_path, "ad.db")
    with TestClient(app) as client:
        sp, pg = _make_space_and_page(client)
        r = client.delete(f"/pages/{pg['id']}", headers=_H)
        assert r.status_code == 204
        assert client.get(f"/pages/{pg['id']}").status_code == 404


def test_api_delete_missing_is_404(tmp_path):
    app = _api_setup(tmp_path, "adm.db")
    with TestClient(app) as client:
        assert client.delete("/pages/999", headers=_H).status_code == 404


def test_api_delete_with_children_is_409(tmp_path):
    # WHY: no silent subtree wipe — refuse, and leave the page intact.
    app = _api_setup(tmp_path, "adc.db")
    with TestClient(app) as client:
        sp, parent = _make_space_and_page(client, title="Parent")
        _make_space_and_page  # noqa
        client.post(
            f"/spaces/{sp['id']}/pages",
            json={"title": "Child", "parent_id": parent["id"]}, headers=_H,
        )
        r = client.delete(f"/pages/{parent['id']}", headers=_H)
        assert r.status_code == 409
        assert client.get(f"/pages/{parent['id']}").status_code == 200  # still there


def test_api_delete_requires_auth(tmp_path):
    app = _api_setup(tmp_path, "ada.db")
    with TestClient(app) as client:
        sp, pg = _make_space_and_page(client)
        assert client.delete(f"/pages/{pg['id']}").status_code == 401


def test_api_move_reparents(tmp_path):
    app = _api_setup(tmp_path, "amv.db")
    with TestClient(app) as client:
        sp, parent = _make_space_and_page(client, title="Parent")
        child = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Child"}, headers=_H
        ).json()
        r = client.put(f"/pages/{child['id']}/move", json={"parent_id": parent["id"]}, headers=_H)
        assert r.status_code == 200
        assert r.json()["parent_id"] == parent["id"]
        # and back to the top level
        r2 = client.put(f"/pages/{child['id']}/move", json={"parent_id": None}, headers=_H)
        assert r2.json()["parent_id"] is None


def test_api_move_cycle_is_422(tmp_path):
    app = _api_setup(tmp_path, "amc.db")
    with TestClient(app) as client:
        sp, a = _make_space_and_page(client, title="A")
        b = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "B", "parent_id": a["id"]}, headers=_H
        ).json()
        # Move A under its own child B -> cycle -> 422.
        assert client.put(f"/pages/{a['id']}/move", json={"parent_id": b["id"]}, headers=_H).status_code == 422


def test_api_move_missing_is_404_and_requires_auth(tmp_path):
    app = _api_setup(tmp_path, "amm.db")
    with TestClient(app) as client:
        sp, pg = _make_space_and_page(client)
        assert client.put("/pages/999/move", json={"parent_id": None}, headers=_H).status_code == 404
        assert client.put(f"/pages/{pg['id']}/move", json={"parent_id": None}).status_code == 401


# --- web --------------------------------------------------------------------


def _login(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "secret"}, headers=_H)
    client.post("/login", data={"email": "a@e.com", "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_delete_requires_login(tmp_path):
    app = create_app(tmp_path / "wdl.db")
    with TestClient(app) as client:
        _login(client)
        sp, pg = _make_space_and_page(client)
        # fresh client = no cookie
    with TestClient(app) as anon:
        assert anon.post(f"/mentor/pages/{pg['id']}/delete").status_code == 401


def test_web_delete_redirects_to_space(tmp_path):
    app = create_app(tmp_path / "wdr.db")
    with TestClient(app) as client:
        _login(client)
        sp, pg = _make_space_and_page(client)
        r = client.post(f"/mentor/pages/{pg['id']}/delete", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/mentor/spaces/{sp['id']}"
        assert client.get(f"/pages/{pg['id']}").status_code == 404


def test_web_delete_with_children_is_409(tmp_path):
    app = create_app(tmp_path / "wdc.db")
    with TestClient(app) as client:
        _login(client)
        sp, parent = _make_space_and_page(client, title="Parent")
        client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Child", "parent_id": parent["id"]}, headers=_H
        )
        r = client.post(f"/mentor/pages/{parent['id']}/delete")
        assert r.status_code == 409
        assert client.get(f"/pages/{parent['id']}").status_code == 200


def test_web_move_reparents_and_rejects_cycle(tmp_path):
    app = create_app(tmp_path / "wmv.db")
    with TestClient(app) as client:
        _login(client)
        sp, a = _make_space_and_page(client, title="A")
        b = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "B", "parent_id": a["id"]}, headers=_H
        ).json()
        # valid: move B to root
        r = client.post(f"/mentor/pages/{b['id']}/move", data={"parent_id": ""}, follow_redirects=False)
        assert r.status_code == 303
        assert client.get(f"/pages/{b['id']}").json()["parent_id"] is None
        # invalid: move A under (now-detached) B... re-nest B under A first
        client.post(f"/mentor/pages/{b['id']}/move", data={"parent_id": str(a["id"])})
        bad = client.post(f"/mentor/pages/{a['id']}/move", data={"parent_id": str(b["id"])})
        assert bad.status_code == 400
