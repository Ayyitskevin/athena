"""Tests for the web (HTML) layer.

Covers the foundation: GET / renders and the templates are wired.
Uses the real templates/ and static/ at project root.
"""
from fastapi.testclient import TestClient

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
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis")
    assert response.status_code == 200
    assert "Aegis" in response.text
    assert "Issues" in response.text
    assert "/aegis/issues" in response.text


def test_issues_list_renders_with_stub_data(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/issues")
    assert response.status_code == 200
    assert "Issues" in response.text
    assert "Bootstrap the web foundation" in response.text
    assert "status" in response.text.lower() or "open" in response.text  # status pills or data
    # HTMX refresh button present
    assert 'hx-get="/aegis/issues"' in response.text


def test_issue_detail_renders(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/issues/1")
    assert response.status_code == 200
    assert "#1" in response.text
    assert "Bootstrap the web foundation" in response.text
    assert "Back to issues" in response.text


def test_new_issue_form_renders(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/issues/new")
    assert response.status_code == 200
    assert "New Issue" in response.text
    assert 'hx-post="/aegis/issues"' in response.text
    assert 'name="title"' in response.text


def test_create_issue_via_htmx(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        # Submit the create form
        response = client.post(
            "/aegis/issues",
            data={"title": "Test created issue via form", "body": "Some details", "status": "open"},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200
    assert "Issue created (demo)" in response.text
    assert "Test created issue via form" in response.text


def test_boards_page_renders(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/boards")
    assert response.status_code == 200
    assert "Boards" in response.text
    assert "Kanban" in response.text or "board" in response.text.lower()
    assert "Open" in response.text
