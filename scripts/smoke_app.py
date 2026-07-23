"""Boot Athena as a real process and verify its deploy-facing health contract."""

from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    ProxyHandler,
    Request,
    build_opener,
)


EXPECTED_HEALTH = {"status": "ok"}
EXPECTED_READY = {"status": "ok", "database": "ok"}
STARTUP_TIMEOUT_SECONDS = 15
_LOOPBACK_OPENER = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))


def _read_json(url: str, *, headers: dict[str, str] | None = None) -> dict:
    request = Request(url, headers=headers or {})
    with _LOOPBACK_OPENER.open(request, timeout=1) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _LOOPBACK_OPENER.open(request, timeout=1) as response:  # noqa: S310
        if response.status != 201:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _post_form(url: str, payload: dict[str, str]) -> None:
    request = Request(
        url,
        data=urlencode(payload).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with _LOOPBACK_OPENER.open(request, timeout=1) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")


def _read_asset(url: str) -> tuple[str, bytes]:
    with _LOOPBACK_OPENER.open(url, timeout=1) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.headers.get_content_type(), response.read()


def _stop(process: subprocess.Popen[str]) -> str | None:
    if process.poll() is not None:
        return f"server exited before teardown with status {process.returncode}"
    # WHY: the CLI interrupt path returns zero after a bounded Uvicorn stop,
    # letting a timeout or forced kill remain an unambiguous smoke failure.
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        return "server did not stop within 5 seconds and required a forced kill"
    if process.returncode != 0:
        return f"server exited during teardown with status {process.returncode}"
    return None


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="athena-process-smoke-") as temp_dir:
        root = Path(temp_dir)
        db_path = root / "athena.db"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        env = os.environ.copy()
        env.update(
            {
                "ATHENA_ATTACH_DIR": str(root / "attachments"),
                "ATHENA_AUTOMATION": "0",
                "ATHENA_DB": str(db_path),
                "ATHENA_LOG_LEVEL": "WARNING",
                "ATHENA_TRUST_ACTOR_HEADER": "1",
                "ATHENA_WEBHOOK_DELIVERY": "0",
                "PYTHONUNBUFFERED": "1",
            }
        )
        output_path = root / "uvicorn.log"
        output_stream = output_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "athena.main:app",
                    "--fd",
                    str(listener.fileno()),
                    "--log-level",
                    "warning",
                ],
                env=env,
                pass_fds=(listener.fileno(),),
                stderr=subprocess.STDOUT,
                stdout=output_stream,
                text=True,
            )
        except BaseException:
            output_stream.close()
            listener.close()
            raise
        listener.close()

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        last_error = "server did not answer"
        success = False
        admin_bootstrapped = False
        browser_authenticated = False
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    last_error = f"server exited with status {process.returncode}"
                    break
                try:
                    health = _read_json(f"http://127.0.0.1:{port}/healthz")
                    ready = _read_json(f"http://127.0.0.1:{port}/readyz")
                    home_type, home = _read_asset(f"http://127.0.0.1:{port}/")
                    css_type, css = _read_asset(
                        f"http://127.0.0.1:{port}/static/styles.css"
                    )
                    metrics = _read_json(f"http://127.0.0.1:{port}/fleet/metrics")
                    metrics_type, metrics_page = _read_asset(
                        f"http://127.0.0.1:{port}/aegis/fleet-metrics"
                    )
                    if not admin_bootstrapped:
                        admin = _post_json(
                            f"http://127.0.0.1:{port}/users",
                            {
                                "email": "smoke@example.com",
                                "name": "Smoke admin",
                                "password": "smoke-password",
                            },
                        )
                        if admin.get("id") != 1 or admin.get("role") != "admin":
                            raise RuntimeError(f"unexpected bootstrap admin: {admin!r}")
                        admin_bootstrapped = True
                    if not browser_authenticated:
                        _post_form(
                            f"http://127.0.0.1:{port}/login",
                            {
                                "email": "smoke@example.com",
                                "password": "smoke-password",
                            },
                        )
                        browser_authenticated = True
                    active_work = _read_json(
                        f"http://127.0.0.1:{port}/fleet/active-work",
                        headers={"X-Athena-Actor": "1"},
                    )
                    mission_type, mission_page = _read_asset(
                        f"http://127.0.0.1:{port}/admin/agents/runs"
                    )
                except (OSError, URLError, json.JSONDecodeError) as exc:
                    last_error = str(exc)
                    time.sleep(0.1)
                    continue
                if health != EXPECTED_HEALTH or ready != EXPECTED_READY:
                    last_error = f"unexpected health payloads: {health!r}, {ready!r}"
                    break
                if not db_path.is_file():
                    last_error = (
                        "ready app did not create the configured fresh database"
                    )
                    break
                if home_type != "text/html" or b"<title>Athena</title>" not in home:
                    last_error = "home page did not render the packaged Athena template"
                    break
                if css_type != "text/css" or not css.strip():
                    last_error = "packaged stylesheet was missing or empty"
                    break
                if (
                    metrics.get("schema") != "athena.fleet_metrics.v1"
                    or metrics.get("flow") != {"created": 0, "completed": 0, "net": 0}
                    or metrics.get("cycle_time", {}).get("median_seconds") is not None
                ):
                    last_error = "fresh database metrics did not return the exact no-data contract"
                    break
                if (
                    metrics_type != "text/html"
                    or b"<title>Fleet Throughput" not in metrics_page
                ):
                    last_error = (
                        "fleet metrics page did not render from packaged assets"
                    )
                    break
                if (
                    active_work.get("schema") != "athena.fleet_active_work.v1"
                    or active_work.get("scope") != "admin_fleet_view"
                    or active_work.get("items") != []
                    or active_work.get("visible_total") != 0
                    or active_work.get("limit") != 100
                    or active_work.get("clipped") is not False
                    or active_work.get("summary")
                    != {
                        "scope": "returned_items",
                        "returned_count": 0,
                        "active_claim_count": 0,
                        "expired_claim_count": 0,
                        "needs_attention_count": 0,
                    }
                ):
                    last_error = (
                        "fresh database active work did not return the exact "
                        "no-data contract"
                    )
                    break
                if (
                    mission_type != "text/html"
                    or b"<title>Agent Mission Control" not in mission_page
                    or b"Active claimed work" not in mission_page
                ):
                    last_error = "Mission Control did not render active work from packaged assets"
                    break
                success = True
                break
        finally:
            try:
                shutdown_error = _stop(process)
            finally:
                output_stream.close()

        output = output_path.read_text(encoding="utf-8")
        if not success or shutdown_error is not None:
            if output:
                print(output, file=sys.stderr)
            details = shutdown_error if success else last_error
            raise RuntimeError(f"Athena process smoke failed: {details}")

        print(
            "Athena process smoke passed: fresh database, no-data metrics and active "
            "work ready, packaged web assets served, and bounded stop"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
