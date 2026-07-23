"""End-to-end coverage for Athena's response cache privacy policy."""

from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from athena.main import create_app


def _vary(response: httpx2.Response) -> set[str]:
    return {
        dimension.strip().casefold()
        for dimension in response.headers.get("vary", "").split(",")
        if dimension.strip()
    }


def _app_with_probes(db_path: Path) -> FastAPI:
    app = create_app(db_path)

    @app.get("/cache-probe")
    def cache_probe() -> PlainTextResponse:
        return PlainTextResponse(
            "public",
            headers={
                "Cache-Control": "public, max-age=60",
                "Vary": "Accept-Encoding",
            },
        )

    @app.post("/mutation-probe")
    def mutation_probe() -> PlainTextResponse:
        return PlainTextResponse("changed")

    @app.get("/cookie-probe")
    def cookie_probe() -> PlainTextResponse:
        response = PlainTextResponse("cookie", headers={"Vary": "Accept-Language"})
        response.set_cookie("probe", "value")
        return response

    @app.get("/download-probe")
    def download_probe() -> PlainTextResponse:
        return PlainTextResponse(
            "download",
            headers={
                "Content-Disposition": 'attachment; filename="probe.txt"',
                "Vary": "Accept-Encoding",
            },
        )

    return app


def test_anonymous_public_get_and_health_cache_behavior_is_unchanged(
    tmp_path: Path,
) -> None:
    app = _app_with_probes(tmp_path / "athena.db")

    with TestClient(app) as client:
        public = client.get("/cache-probe")
        health = client.get("/healthz")

    assert public.headers["cache-control"] == "public, max-age=60"
    assert public.headers["vary"] == "Accept-Encoding"
    assert "cache-control" not in health.headers
    assert "vary" not in health.headers


@pytest.mark.parametrize(
    "identity_headers",
    [
        {"Authorization": "Bearer opaque"},
        {"X-Athena-Actor": "17"},
    ],
)
def test_api_identity_requests_are_private_and_vary_on_both_identity_inputs(
    tmp_path: Path, identity_headers: dict[str, str]
) -> None:
    app = _app_with_probes(tmp_path / "athena.db")

    with TestClient(app) as client:
        response = client.get("/cache-probe", headers=identity_headers)

    assert response.headers["cache-control"] == "private, no-store"
    assert _vary(response) == {
        "accept-encoding",
        "authorization",
        "x-athena-actor",
    }


def test_session_and_sensitive_routes_are_private_with_relevant_vary(
    tmp_path: Path,
) -> None:
    app = _app_with_probes(tmp_path / "athena.db")

    with TestClient(app) as client:
        session = client.get("/cache-probe", headers={"Cookie": "probe=value"})
        session_health = client.get("/healthz", headers={"Cookie": "probe=value"})
        login = client.get("/login")
        attachment = client.get("/attachments/999999")
        mutation = client.post("/mutation-probe")

    assert session.headers["cache-control"] == "private, no-store"
    assert _vary(session) == {"accept-encoding", "cookie"}
    assert session_health.headers["cache-control"] == "private, no-store"
    assert _vary(session_health) == {"cookie"}
    assert login.headers["cache-control"] == "private, no-store"
    assert _vary(login) == {"cookie"}
    assert attachment.headers["cache-control"] == "private, no-store"
    assert _vary(attachment) == {"authorization", "x-athena-actor"}
    assert mutation.headers["cache-control"] == "private, no-store"


def test_downloads_and_cookie_setting_responses_are_never_stored(
    tmp_path: Path,
) -> None:
    app = _app_with_probes(tmp_path / "athena.db")

    with TestClient(app) as client:
        download = client.get("/download-probe")
        cookie = client.get("/cookie-probe")

    assert download.headers["cache-control"] == "private, no-store"
    assert _vary(download) == {"accept-encoding"}
    assert cookie.headers["cache-control"] == "private, no-store"
    assert _vary(cookie) == {"accept-language", "cookie"}
