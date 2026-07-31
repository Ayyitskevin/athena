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
BOOTSTRAP_TOKEN = "process-smoke-bootstrap-token-000001"
STARTUP_TIMEOUT_SECONDS = 15
_LOOPBACK_OPENER = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))


def _read_json(url: str, *, headers: dict[str, str] | None = None) -> dict:
    request = Request(url, headers=headers or {})
    with _LOOPBACK_OPENER.open(request, timeout=1) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _post_json(
    url: str, payload: dict, *, headers: dict[str, str] | None = None
) -> dict:
    request_headers = dict(headers or {})
    request_headers["Content-Type"] = "application/json"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
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
        attach_dir = root / "attachments"
        attach_dir.mkdir()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        serve = Path(sys.executable).with_name("athena-serve")
        if not serve.is_file() or not os.access(serve, os.X_OK):
            raise RuntimeError(
                f"installed athena-serve console script is missing: {serve}"
            )

        base_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ATHENA_")
        }
        base_env.update(
            {
                "ATHENA_ATTACH_DIR": str(attach_dir),
                "ATHENA_AUTOMATION": "0",
                "ATHENA_DB": str(db_path),
                "ATHENA_LOG_LEVEL": "WARNING",
                "ATHENA_NETWORK_MODE": "local",
                "ATHENA_TRUST_ACTOR_HEADER": "0",
                "ATHENA_WEBHOOK_DELIVERY": "0",
                "PYTHONUNBUFFERED": "1",
            }
        )
        bootstrap_env = base_env.copy()
        bootstrap_env["ATHENA_BOOTSTRAP_TOKEN"] = BOOTSTRAP_TOKEN
        normal_env = base_env.copy()

        output_path = root / "athena-serve.log"
        output_stream = output_path.open("w", encoding="utf-8")

        def start(env: dict[str, str], *, bootstrap: bool) -> subprocess.Popen[str]:
            command = [
                str(serve),
                "--fd",
                str(listener.fileno()),
            ]
            if bootstrap:
                command.append("--bootstrap")
            return subprocess.Popen(
                command,
                env=env,
                pass_fds=(listener.fileno(),),
                stderr=subprocess.STDOUT,
                stdout=output_stream,
                text=True,
            )

        try:
            bootstrap_process = start(bootstrap_env, bootstrap=True)
        except BaseException:
            output_stream.close()
            listener.close()
            raise

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        bootstrap_error = "bootstrap server did not answer"
        bootstrap_success = False
        try:
            while time.monotonic() < deadline:
                if bootstrap_process.poll() is not None:
                    bootstrap_error = (
                        "bootstrap server exited with status "
                        f"{bootstrap_process.returncode}"
                    )
                    break
                try:
                    health = _read_json(f"http://127.0.0.1:{port}/healthz")
                    ready = _read_json(f"http://127.0.0.1:{port}/readyz")
                    admin = _post_json(
                        f"http://127.0.0.1:{port}/users",
                        {
                            "email": "smoke@example.com",
                            "name": "Smoke admin",
                            "password": "smoke-password",
                        },
                        headers={"X-Athena-Bootstrap-Token": BOOTSTRAP_TOKEN},
                    )
                except (OSError, URLError, json.JSONDecodeError) as exc:
                    bootstrap_error = str(exc)
                    time.sleep(0.1)
                    continue
                if health != EXPECTED_HEALTH or ready != EXPECTED_READY:
                    bootstrap_error = (
                        f"unexpected health payloads: {health!r}, {ready!r}"
                    )
                    break
                if admin.get("id") != 1 or admin.get("role") != "admin":
                    bootstrap_error = f"unexpected bootstrap admin: {admin!r}"
                    break
                if not db_path.is_file():
                    bootstrap_error = (
                        "ready app did not create the configured fresh database"
                    )
                    break
                bootstrap_success = True
                break
        finally:
            bootstrap_shutdown_error = _stop(bootstrap_process)

        if not bootstrap_success or bootstrap_shutdown_error is not None:
            output_stream.close()
            listener.close()
            output = output_path.read_text(encoding="utf-8")
            if output:
                print(output, file=sys.stderr)
            details = bootstrap_shutdown_error if bootstrap_success else bootstrap_error
            raise RuntimeError(f"Athena bootstrap process smoke failed: {details}")

        try:
            normal_process = start(normal_env, bootstrap=False)
        except BaseException:
            output_stream.close()
            listener.close()
            raise

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        normal_error = "normal server did not answer"
        normal_success = False
        browser_authenticated = False
        try:
            while time.monotonic() < deadline:
                if normal_process.poll() is not None:
                    normal_error = (
                        f"normal server exited with status {normal_process.returncode}"
                    )
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
                    if not browser_authenticated:
                        _post_form(
                            f"http://127.0.0.1:{port}/login",
                            {
                                "email": "smoke@example.com",
                                "password": "smoke-password",
                            },
                        )
                        browser_authenticated = True
                    mission_type, mission_page = _read_asset(
                        f"http://127.0.0.1:{port}/admin/agents/runs"
                    )
                except (OSError, URLError, json.JSONDecodeError) as exc:
                    normal_error = str(exc)
                    time.sleep(0.1)
                    continue
                if health != EXPECTED_HEALTH or ready != EXPECTED_READY:
                    normal_error = f"unexpected health payloads: {health!r}, {ready!r}"
                    break
                if home_type != "text/html" or b"<title>Athena</title>" not in home:
                    normal_error = (
                        "home page did not render the packaged Athena template"
                    )
                    break
                if css_type != "text/css" or not css.strip():
                    normal_error = "packaged stylesheet was missing or empty"
                    break
                if (
                    metrics.get("schema") != "athena.fleet_metrics.v1"
                    or metrics.get("flow") != {"created": 0, "completed": 0, "net": 0}
                    or metrics.get("cycle_time", {}).get("median_seconds") is not None
                ):
                    normal_error = (
                        "fresh database metrics did not return the exact no-data "
                        "contract"
                    )
                    break
                if (
                    metrics_type != "text/html"
                    or b"<title>Fleet Throughput" not in metrics_page
                ):
                    normal_error = (
                        "fleet metrics page did not render from packaged assets"
                    )
                    break
                if (
                    mission_type != "text/html"
                    or b"<title>Agent Mission Control" not in mission_page
                    or b"Active claimed work" not in mission_page
                ):
                    normal_error = (
                        "Mission Control did not render active work from packaged "
                        "assets"
                    )
                    break
                normal_success = True
                break
        finally:
            try:
                normal_shutdown_error = _stop(normal_process)
            finally:
                output_stream.close()
                listener.close()

        output = output_path.read_text(encoding="utf-8")
        if not normal_success or normal_shutdown_error is not None:
            if output:
                print(output, file=sys.stderr)
            details = normal_shutdown_error if normal_success else normal_error
            raise RuntimeError(f"Athena normal process smoke failed: {details}")

        print(
            "Athena process smoke passed: installed launcher bootstrapped locally, "
            "stopped, restarted without bootstrap or actor-header trust, served "
            "packaged assets, authenticated by browser session, and stopped cleanly"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
