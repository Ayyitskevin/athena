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
