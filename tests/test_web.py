"""Tests for the web (HTML) layer.

Covers the foundation: GET / renders and the templates are wired.
Uses the real templates/ and static/ at project root.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app


def test_home_returns_200_and_contains_athena(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Athena" in response.text
    # Basic sanity that it's HTML and our layout is there
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text.lower()
    assert "htmx" in response.text.lower()  # CDN script present in base


def test_aegis_dashboard_renders(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        client.post("/issues", json={"title": "Dashboard test issue", "created_by": 1})
        response = client.get("/aegis")
    assert response.status_code == 200
    assert "Aegis" in response.text
    assert "Issues" in response.text
    assert "/aegis/issues" in response.text
    assert "Dashboard test issue" in response.text or "Recent Issues" in response.text
    assert "Issues (" in response.text  # count from status


def _seed_user(db_file):
    # Seed directly (no user API yet); then use /issues API to create real issue for web tests.
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("kevin@example.com", "Kevin"))
    conn.commit()
    conn.close()


def test_issues_list_renders_real_data(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        # seed one real issue via the API (actor identifies the creator)
        client.post(
            "/issues",
            json={"title": "Real issue from API", "body": "test"},
            headers={"X-Athena-Actor": "1"},
        )

        response = client.get("/aegis/issues")
    assert response.status_code == 200
    assert "Issues" in response.text
    assert "Real issue from API" in response.text
    assert 'hx-get="/aegis/issues"' in response.text


def test_issue_detail_renders_real_data(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        created = client.post(
            "/issues", json={"title": "Detail test issue"}, headers={"X-Athena-Actor": "1"}
        )
        issue_id = created.json()["id"]

        response = client.get(f"/aegis/issues/{issue_id}")
    assert response.status_code == 200
    assert f"#{issue_id}" in response.text
    assert "Detail test issue" in response.text
    assert "Back to issues" in response.text


def test_new_issue_form_renders(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/issues/new")
    assert response.status_code == 200
    assert "New Issue" in response.text
    assert 'hx-post="/aegis/issues"' in response.text
    assert 'name="title"' in response.text


def test_create_issue_shows_blocked_state(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        # POST create should show the auth blocker message (no user system yet)
        response = client.post(
            "/aegis/issues",
            data={"title": "Should not create", "body": "foo", "status": "open"},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200
    assert "issue creation needs user accounts (coming in core/auth)" in response.text


def test_boards_page_renders(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/boards")
    assert response.status_code == 200
    assert "Boards" in response.text
    assert "Kanban" in response.text or "board" in response.text.lower()
    assert "Open" in response.text


def test_issues_list_htmx_fragment_vs_full_page(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        client.post(
            "/issues",
            json={"title": "Searchable real issue", "body": "test"},
            headers={"X-Athena-Actor": "1"},
        )

        # HTMX request (fragment only)
        response = client.get(
            "/aegis/issues?search=Searchable",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "Searchable real issue" in response.text
        # Fragment: has table content but NOT page chrome (search input, header, nav etc.)
        assert "search-input" not in response.text
        assert "page-header" not in response.text.lower()
        assert '<div id="issues-table">' in response.text

        # Non-HTMX: full page
        response = client.get("/aegis/issues?search=Searchable")
        assert response.status_code == 200
        assert "Searchable real issue" in response.text
        assert "search-input" in response.text  # page chrome present
        assert "Issues" in response.text  # from <h1> or title in full page
