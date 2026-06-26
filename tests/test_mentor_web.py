"""The Mentor web surface — the browser thin client over the spaces/pages API.

These tests encode the contract that matters: the web layer owns NO data (every
created space/page is confirmed back through the REST API, never a stub store),
writes are gated on the browser session, and the page tree renders its nesting.
"""
from fastapi.testclient import TestClient

from athena.main import create_app


def _login(client, email="ann@e.com", name="Ann"):
    # First (bootstrap) user create is allowed; afterward act as user 1. Mirrors
    # the Aegis web tests' helper. The login cookie then rides on the client.
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


# --- spaces -----------------------------------------------------------------


def test_web_create_space_is_api_backed(tmp_path):
    # WHY: the create form must write through the real API — confirm the space
    # exists in GET /spaces (not just in a page render), proving no stub store.
    app = create_app(tmp_path / "cs.db")
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/mentor/spaces",
            data={"key": "eng", "name": "Engineering", "description": "docs"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        made = client.get("/spaces").json()
        assert [(s["key"], s["name"]) for s in made] == [("ENG", "Engineering")]
        assert made[0]["created_by"] == 1
        # and it renders on the spaces index
        assert "Engineering" in client.get("/mentor").text


def test_web_create_space_requires_login(tmp_path):
    app = create_app(tmp_path / "csl.db")
    with TestClient(app) as client:
        r = client.post("/mentor/spaces", data={"key": "ENG", "name": "Eng"})
        assert r.status_code == 401


def test_web_space_key_and_name_required(tmp_path):
    app = create_app(tmp_path / "csr.db")
    with TestClient(app) as client:
        _login(client)
        assert client.post("/mentor/spaces", data={"key": "  ", "name": "Eng"}).status_code == 400
        assert client.post("/mentor/spaces", data={"key": "ENG", "name": "  "}).status_code == 400


def test_web_duplicate_space_key_is_409_case_insensitive(tmp_path):
    app = create_app(tmp_path / "csd.db")
    with TestClient(app) as client:
        _login(client)
        client.post("/mentor/spaces", data={"key": "ENG", "name": "Eng"})
        r = client.post("/mentor/spaces", data={"key": "eng", "name": "Other"})
        assert r.status_code == 409


def test_web_missing_space_renders_404(tmp_path):
    app = create_app(tmp_path / "csm.db")
    with TestClient(app) as client:
        assert client.get("/mentor/spaces/999").status_code == 404


# --- pages: create + tree ---------------------------------------------------


def _make_space(client, key="ENG", name="Engineering"):
    client.post("/mentor/spaces", data={"key": key, "name": name})
    return client.get("/spaces").json()[0]


def test_web_create_page_is_api_backed(tmp_path):
    # WHY: page create writes through the API; confirm via GET /spaces/{id}/pages.
    app = create_app(tmp_path / "cp.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        r = client.post(
            f"/mentor/spaces/{space['id']}/pages",
            data={"title": "Runbook", "body": "steps"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        pages = client.get(f"/spaces/{space['id']}/pages").json()
        assert [p["title"] for p in pages] == ["Runbook"]
        assert pages[0]["created_by"] == 1


def test_web_create_page_requires_login(tmp_path):
    app = create_app(tmp_path / "cpl.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        # drop the session by logging out, then attempt the write
        client.post("/logout")
        r = client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "x"})
        assert r.status_code == 401


def test_web_create_page_in_missing_space_is_404(tmp_path):
    app = create_app(tmp_path / "cpm.db")
    with TestClient(app) as client:
        _login(client)
        r = client.post("/mentor/spaces/999/pages", data={"title": "x"})
        assert r.status_code == 404


def test_web_create_page_empty_title_is_400(tmp_path):
    app = create_app(tmp_path / "cpe.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        r = client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "   "})
        assert r.status_code == 400


def test_web_page_tree_renders_nesting(tmp_path):
    # WHY: the tree is assembled in the web layer from the flat list; a child must
    # render under its parent (depth marker present, both titles shown).
    app = create_app(tmp_path / "tree.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "Parent"})
        parent = client.get(f"/spaces/{space['id']}/pages").json()[0]
        client.post(
            f"/mentor/spaces/{space['id']}/pages",
            data={"title": "Child", "parent_id": str(parent["id"])},
        )
        html = client.get(f"/mentor/spaces/{space['id']}").text
        assert "Parent" in html and "Child" in html
        assert "└" in html  # the nesting marker only renders for depth > 0


def test_page_detail_shows_space_nav_tree(tmp_path):
    # WHY: a page should let you navigate the whole space without going back to its
    # index — the page renders the space's page tree, links every OTHER page, and
    # marks the current one as current (not a link).
    app = create_app(tmp_path / "nav.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "Runbooks"})
        parent = client.get(f"/spaces/{space['id']}/pages").json()[0]
        client.post(
            f"/mentor/spaces/{space['id']}/pages",
            data={"title": "Deploy guide", "parent_id": str(parent["id"])},
        )
        client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "Onboarding"})
        by_title = {p["title"]: p for p in client.get(f"/spaces/{space['id']}/pages").json()}
        child, sibling = by_title["Deploy guide"], by_title["Onboarding"]

        html = client.get(f"/mentor/pages/{child['id']}").text
        assert 'class="doc-tree"' in html  # the nav tree is present
        # other pages are reachable as links...
        assert f'href="/mentor/pages/{parent["id"]}"' in html
        assert f'href="/mentor/pages/{sibling["id"]}"' in html
        # ...and the current page is highlighted, NOT a self-link
        assert 'aria-current="page">Deploy guide<' in html
        assert f'href="/mentor/pages/{child["id"]}"' not in html


def test_web_cross_space_parent_is_rejected(tmp_path):
    # WHY: a page's parent must live in the SAME space — the tree can't span spaces.
    app = create_app(tmp_path / "xspace.db")
    with TestClient(app) as client:
        _login(client)
        a = _make_space(client, key="A", name="Alpha")
        client.post("/mentor/spaces", data={"key": "B", "name": "Beta"})
        b = client.get("/spaces").json()
        b = [s for s in b if s["key"] == "B"][0]
        client.post(f"/mentor/spaces/{a['id']}/pages", data={"title": "InA"})
        a_page = client.get(f"/spaces/{a['id']}/pages").json()[0]
        # try to nest a B-page under an A-page
        r = client.post(
            f"/mentor/spaces/{b['id']}/pages",
            data={"title": "InB", "parent_id": str(a_page["id"])},
        )
        assert r.status_code == 400


def test_web_empty_space_shows_no_pages(tmp_path):
    app = create_app(tmp_path / "empty.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        assert "No pages in this space yet." in client.get(f"/mentor/spaces/{space['id']}").text


# --- pages: view + edit + history ------------------------------------------


def test_web_page_detail_renders_body(tmp_path):
    app = create_app(tmp_path / "pd.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "Doc", "body": "hello world"})
        page = client.get(f"/spaces/{space['id']}/pages").json()[0]
        html = client.get(f"/mentor/pages/{page['id']}").text
        assert "Doc" in html and "hello world" in html


def test_web_missing_page_is_404(tmp_path):
    app = create_app(tmp_path / "pm.db")
    with TestClient(app) as client:
        assert client.get("/mentor/pages/999").status_code == 404


def test_web_edit_requires_login(tmp_path):
    app = create_app(tmp_path / "el.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "Doc"})
        page = client.get(f"/spaces/{space['id']}/pages").json()[0]
        client.post("/logout")
        assert client.get(f"/mentor/pages/{page['id']}/edit").status_code == 401
        assert client.post(f"/mentor/pages/{page['id']}/edit", data={"title": "X"}).status_code == 401


def test_web_edit_empty_title_is_400(tmp_path):
    app = create_app(tmp_path / "ee.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "Doc"})
        page = client.get(f"/spaces/{space['id']}/pages").json()[0]
        r = client.post(f"/mentor/pages/{page['id']}/edit", data={"title": "  "})
        assert r.status_code == 400


def test_web_edit_saves_and_snapshots_history(tmp_path):
    # WHY: editing through the web must go through update_page, which snapshots the
    # prior revision into history — confirm the new body shows AND a version exists.
    app = create_app(tmp_path / "es.db")
    with TestClient(app) as client:
        _login(client)
        space = _make_space(client)
        client.post(f"/mentor/spaces/{space['id']}/pages", data={"title": "Doc", "body": "v1"})
        page = client.get(f"/spaces/{space['id']}/pages").json()[0]

        r = client.post(
            f"/mentor/pages/{page['id']}/edit",
            data={"title": "Doc", "body": "v2"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        # live page now reads v2 (through the API, no stub)
        assert client.get(f"/pages/{page['id']}").json()["body"] == "v2"
        # and the prior revision was snapshotted into history (v1 preserved)
        versions = client.get(f"/pages/{page['id']}/versions").json()
        assert [v["version"] for v in versions] == [1]
        assert versions[0]["body"] == "v1"
        # the detail page surfaces that history
        assert "v1" in client.get(f"/mentor/pages/{page['id']}").text
