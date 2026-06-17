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
