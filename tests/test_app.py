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


def test_readyz_checks_database(tmp_path):
    # WHY: /healthz is intentionally cheap. /readyz is the deploy-facing check
    # that proves SQLite is reachable and migrations have run.
    db_file = tmp_path / "ready.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_readyz_fails_when_schema_is_missing(tmp_path):
    db_file = tmp_path / "ready_fail.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        conn = sqlite3.connect(db_file)
        conn.execute("DROP TABLE schema_migrations")
        conn.commit()
        conn.close()

        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}


def test_security_headers_are_attached(tmp_path):
    app = create_app(tmp_path / "headers.db")
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["permissions-policy"] == (
        "camera=(), geolocation=(), microphone=()"
    )


def test_request_body_limit_rejects_large_payload(tmp_path):
    # WHY: Athena is local-first, but a single oversized POST should still be
    # rejected before route parsing or database work.
    app = create_app(tmp_path / "body_limit.db", max_request_body_bytes=64)
    with TestClient(app) as client:
        response = client.post(
            "/users",
            json={"email": "kevin@example.com", "name": "K" * 100, "password": "pw"},
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}
    assert response.headers["x-content-type-options"] == "nosniff"


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
