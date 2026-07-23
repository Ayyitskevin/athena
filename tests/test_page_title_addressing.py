"""Title-based addressing for pages — the address an agent actually knows.

Two halves, both proven here:

  * GET /pages/by-title turns a remembered title into the page(s) it names — one for
    the common case, a LIST when a title is reused (titles aren't unique), narrowable by
    space, and gated by space visibility so a hidden page never surfaces by title;
  * the route is declared before /pages/{page_id}, so "by-title" is not swallowed as a
    numeric page id — a plain numeric fetch still works.

The [[Title]] wiki-link resolution + inline render live in test_links.py; this file is
the transport (REST) contract for the fetch-by-title lookup and the MCP tool behind it.
"""

from fastapi.testclient import TestClient

from athena.main import create_app


def _seed_user(db_file, email="a@e.com", name="A"):
    from athena.core import db

    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _h(actor="1"):
    return {"X-Athena-Actor": actor}


def test_by_title_unique_match(tmp_path):
    app = create_app(tmp_path / "u.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "u.db")
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=_h()
        ).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Design Doc"}, headers=_h()
        ).json()
        # Case-insensitive exact-title match.
        r = client.get("/pages/by-title", params={"title": "design doc"})
        assert r.status_code == 200
        body = r.json()
        assert [p["id"] for p in body] == [pg["id"]]
        assert body[0]["title"] == "Design Doc"


def test_by_title_returns_all_matches_and_narrows_by_space(tmp_path):
    app = create_app(tmp_path / "amb.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "amb.db")
        eng = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=_h()
        ).json()
        ops = client.post(
            "/spaces", json={"key": "OPS", "name": "Ops"}, headers=_h()
        ).json()
        a = client.post(
            f"/spaces/{eng['id']}/pages", json={"title": "Runbook"}, headers=_h()
        ).json()
        b = client.post(
            f"/spaces/{ops['id']}/pages", json={"title": "Runbook"}, headers=_h()
        ).json()
        # Both matches, ordered by (space_id, id) — deterministic.
        both = client.get("/pages/by-title", params={"title": "Runbook"}).json()
        assert [p["id"] for p in both] == [a["id"], b["id"]]
        # space_id narrows the ambiguity to one space.
        narrowed = client.get(
            "/pages/by-title", params={"title": "Runbook", "space_id": ops["id"]}
        ).json()
        assert [p["id"] for p in narrowed] == [b["id"]]


def test_by_title_excludes_archived(tmp_path):
    app = create_app(tmp_path / "arch.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "arch.db")
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=_h()
        ).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Old Doc"}, headers=_h()
        ).json()
        client.post(f"/pages/{pg['id']}/archive", headers=_h())
        # A soft-deleted page is not a live target — the lookup returns nothing.
        assert client.get("/pages/by-title", params={"title": "Old Doc"}).json() == []


def test_by_title_unknown_is_empty_list_not_error(tmp_path):
    app = create_app(tmp_path / "none.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "none.db")
        r = client.get("/pages/by-title", params={"title": "Nothing Here"})
        assert r.status_code == 200 and r.json() == []


def test_by_title_hides_pages_in_private_spaces(tmp_path):
    # WHY: the lookup must not become a title oracle for private spaces. A page in a
    # private space the caller can't see is dropped, so its title/existence never leaks.
    app = create_app(tmp_path / "priv.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "priv.db", email="owner@e.com", name="Owner")  # id 1
        _seed_user(tmp_path / "priv.db", email="other@e.com", name="Other")  # id 2
        sp = client.post(
            "/spaces", json={"key": "SEC", "name": "Sec"}, headers=_h("1")
        ).json()
        client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Secret Plan"}, headers=_h("1")
        )
        client.put(
            f"/spaces/{sp['id']}/visibility",
            json={"visibility": "private"},
            headers=_h("1"),
        )
        # The owner (a member) sees it; an outsider gets an empty list, not the page.
        assert client.get(
            "/pages/by-title", params={"title": "Secret Plan"}, headers=_h("1")
        ).json()
        assert (
            client.get(
                "/pages/by-title", params={"title": "Secret Plan"}, headers=_h("2")
            ).json()
            == []
        )
        # Anonymous also sees nothing.
        assert (
            client.get("/pages/by-title", params={"title": "Secret Plan"}).json() == []
        )


def test_by_title_route_does_not_shadow_numeric_page_fetch(tmp_path):
    # WHY: /pages/by-title is a literal path declared before /pages/{page_id}. Prove the
    # ordering didn't break the numeric fetch (and that "by-title" isn't parsed as an id).
    app = create_app(tmp_path / "order.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "order.db")
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=_h()
        ).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Doc"}, headers=_h()
        ).json()
        by_id = client.get(f"/pages/{pg['id']}")
        assert by_id.status_code == 200 and by_id.json()["id"] == pg["id"]
        by_title = client.get("/pages/by-title", params={"title": "Doc"})
        assert by_title.status_code == 200 and [p["id"] for p in by_title.json()] == [
            pg["id"]
        ]
