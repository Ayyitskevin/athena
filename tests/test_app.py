"""Tests for the web app: the factory builds, startup migrates, /healthz answers.

TestClient used as a context manager (`with TestClient(app)`) runs the app's
startup/shutdown — so this also proves migrate-on-startup actually fires.
"""
import os
import sqlite3

from fastapi.testclient import TestClient

from athena.main import create_app


def test_healthz_returns_ok(tmp_path):
    app = create_app(tmp_path / "app.db")
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_migrates_the_database(tmp_path):
    db_file = tmp_path / "startup.db"
    app = create_app(db_file)

    with TestClient(app):  # entering the context triggers startup -> migrate()
        pass

    # After startup, the schema should exist in the file the app was given.
    conn = sqlite3.connect(db_file)
    tables = {
        name
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"users", "issues", "schema_migrations"} <= tables


def test_app_serves_assets_from_any_cwd(tmp_path, monkeypatch):
    # WHY: static/ and templates/ are resolved from the package location, not the
    # process cwd. Launch the app from an unrelated directory and both a rendered
    # template and a static asset must still serve — otherwise the app only works
    # when started from the repo root.
    monkeypatch.chdir(tmp_path)  # a cwd with no static/ or templates/ in sight
    assert not os.path.exists("static") and not os.path.exists("templates")

    app = create_app(tmp_path / "cwd.db")
    with TestClient(app) as client:
        page = client.get("/")  # renders templates/...
        assert page.status_code == 200
        css = client.get("/static/styles.css")  # served from static/
        assert css.status_code == 200
