"""Tests for the web app: the factory builds, startup migrates, /healthz answers.

TestClient used as a context manager (`with TestClient(app)`) runs the app's
startup/shutdown — so this also proves migrate-on-startup actually fires.
"""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess

from fastapi.testclient import TestClient

from athena import __version__, config
from athena.core import db
from athena.main import create_app


async def _raw_asgi_request(
    app,
    *,
    method: str,
    path: str,
    body: bytes,
    headers: list[tuple[bytes, bytes]],
    server: tuple[str, int] = ("testserver", 80),
    scheme: str = "http",
    ensure_host: bool = True,
    receive_observed: list[bool] | None = None,
):
    messages = []
    sent = False
    request_headers = list(headers)
    if ensure_host and not any(name.lower() == b"host" for name, _ in request_headers):
        request_headers.append((b"host", b"testserver"))

    async def receive():
        nonlocal sent
        if receive_observed is not None:
            receive_observed.append(True)
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": request_headers,
        "client": ("testclient", 50000),
        "server": server,
        "scheme": scheme,
        "root_path": "",
    }
    await app(scope, receive, send)
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, response_body


def test_version_reports_the_running_checkout_without_opening_the_database(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    db_path = tmp_path / "must-not-exist.db"
    repo_root = Path(__file__).resolve().parents[1]
    expected_commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    git_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    expected_tree_state = "dirty" if git_status else "clean"
    app = create_app(db_path)

    status, response_body = asyncio.run(
        _raw_asgi_request(
            app,
            method="GET",
            path="/version",
            body=b"",
            headers=[],
        )
    )

    assert status == 200
    assert json.loads(response_body) == {
        "version": __version__,
        "commit": expected_commit,
        "tree_state": expected_tree_state,
        "source": "git-checkout",
    }
    assert not db_path.exists()


def test_deployment_boundary_allows_exact_local_socket_and_host(tmp_path):
    app = create_app(
        tmp_path / "allowed.db",
        network_mode="local",
        allowed_authorities=("localhost:80",),
    )
    status, response_body = asyncio.run(
        _raw_asgi_request(
            app,
            method="GET",
            path="/healthz",
            body=b"",
            headers=[(b"host", b"LOCALHOST")],
            server=("127.0.0.1", 80),
        )
    )

    assert status == 200
    assert json.loads(response_body) == {"status": "ok"}


def test_deployment_boundary_rejects_socket_before_body_or_database_work(tmp_path):
    db_path = tmp_path / "must-not-exist.db"
    app = create_app(
        db_path,
        network_mode="local",
        allowed_authorities=("localhost:80",),
    )
    received = []
    status, response_body = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/users",
            body=b"attacker body",
            headers=[(b"host", b"localhost")],
            server=("192.168.1.8", 80),
            receive_observed=received,
        )
    )

    assert status == 503
    assert json.loads(response_body) == {
        "detail": "request rejected by deployment boundary"
    }
    assert received == []
    assert not db_path.exists()


def test_deployment_boundary_rejects_socket_port_before_body_or_database_work(tmp_path):
    db_path = tmp_path / "must-not-exist.db"
    app = create_app(
        db_path,
        network_mode="local",
        allowed_authorities=("localhost:80",),
    )
    received = []
    status, response_body = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/users",
            body=b"attacker body",
            headers=[(b"host", b"localhost")],
            server=("127.0.0.1", 8000),
            receive_observed=received,
        )
    )

    assert status == 503
    assert json.loads(response_body) == {
        "detail": "request rejected by deployment boundary"
    }
    assert received == []
    assert not db_path.exists()


def test_supported_deployment_boundary_requires_the_exact_preflighted_listener(
    tmp_path,
):
    db_path = tmp_path / "must-not-exist.db"
    app = create_app(
        db_path,
        network_mode="tailnet",
        allowed_authorities=("athena.tailnet:8443",),
        expected_server=("100.64.1.2", 8443),
    )
    received = []
    status, response_body = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/users",
            body=b"attacker body",
            headers=[(b"host", b"athena.tailnet:8443")],
            server=("127.0.0.1", 8443),
            receive_observed=received,
        )
    )

    assert status == 503
    assert json.loads(response_body) == {
        "detail": "request rejected by deployment boundary"
    }
    assert received == []
    assert not db_path.exists()


def test_deployment_boundary_rejects_host_before_body_or_database_work(tmp_path):
    db_path = tmp_path / "must-not-exist.db"
    app = create_app(
        db_path,
        network_mode="local",
        allowed_authorities=("localhost:80",),
    )
    received = []
    status, response_body = asyncio.run(
        _raw_asgi_request(
            app,
            method="POST",
            path="/users",
            body=b"attacker body",
            headers=[
                (b"host", b"evil.example"),
                (b"x-forwarded-host", b"localhost"),
            ],
            server=("127.0.0.1", 80),
            receive_observed=received,
        )
    )

    assert status == 421
    assert json.loads(response_body) == {
        "detail": "request rejected by deployment boundary"
    }
    assert received == []
    assert not db_path.exists()


def test_deployment_boundary_rejects_missing_and_duplicate_host(tmp_path):
    app = create_app(
        tmp_path / "host.db",
        network_mode="local",
        allowed_authorities=("localhost:80",),
    )
    for headers in (
        [],
        [(b"host", b"localhost"), (b"host", b"localhost")],
    ):
        status, _ = asyncio.run(
            _raw_asgi_request(
                app,
                method="GET",
                path="/healthz",
                body=b"",
                headers=headers,
                server=("127.0.0.1", 80),
                ensure_host=False,
            )
        )
        assert status == 421


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
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self';" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
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


def test_request_body_limit_rejects_before_session_cookie_database_work(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path / "body_limit_session.db", max_request_body_bytes=64)
    with TestClient(app) as client:
        opened = []
        original_connect = db.connect

        def tracked_connect(*args, **kwargs):
            opened.append((args, kwargs))
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(db, "connect", tracked_connect)
        response = client.post(
            "/users",
            content=b"x" * 65,
            headers={"Cookie": f"{config.SESSION_COOKIE}=attacker-controlled"},
        )

    assert response.status_code == 413
    assert opened == []


def test_request_body_limit_counts_streamed_payload_without_content_length(tmp_path):
    # WHY: clients can stream a body without Content-Length. The app limit must
    # count bytes actually received, not only trust the optional header.
    app = create_app(tmp_path / "body_limit_streamed.db", max_request_body_bytes=8)
    body = json.dumps(
        {"email": "kevin@example.com", "name": "Kevin", "password": "secret"}
    ).encode()
    with TestClient(app):
        status, response_body = asyncio.run(
            _raw_asgi_request(
                app,
                method="POST",
                path="/users",
                body=body,
                headers=[(b"content-type", b"application/json")],
            )
        )

    assert status == 413
    assert json.loads(response_body) == {"detail": "request body too large"}


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
    # WHY: packaged static/ and templates/ are resolved from the module location,
    # not the process cwd. Launch the app from an unrelated directory and both a
    # rendered template and a static asset must still serve.
    monkeypatch.chdir(tmp_path)  # a cwd with no static/ or templates/ in sight
    assert not os.path.exists("static") and not os.path.exists("templates")

    app = create_app(tmp_path / "cwd.db")
    with TestClient(app) as client:
        page = client.get("/")  # renders templates/...
        assert page.status_code == 200
        css = client.get("/static/styles.css")  # served from static/
        assert css.status_code == 200
