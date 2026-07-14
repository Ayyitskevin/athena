"""Boot Athena as a real process and verify its deploy-facing health contract."""

from __future__ import annotations

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
from urllib.request import ProxyHandler, build_opener


EXPECTED_HEALTH = {"status": "ok"}
EXPECTED_READY = {"status": "ok", "database": "ok"}
STARTUP_TIMEOUT_SECONDS = 15
_LOOPBACK_OPENER = build_opener(ProxyHandler({}))


def _read_json(url: str) -> dict:
    with _LOOPBACK_OPENER.open(url, timeout=1) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


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
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    last_error = f"server exited with status {process.returncode}"
                    break
                try:
                    health = _read_json(f"http://127.0.0.1:{port}/healthz")
                    ready = _read_json(f"http://127.0.0.1:{port}/readyz")
                except (OSError, URLError, json.JSONDecodeError) as exc:
                    last_error = str(exc)
                    time.sleep(0.1)
                    continue
                if health != EXPECTED_HEALTH or ready != EXPECTED_READY:
                    last_error = f"unexpected health payloads: {health!r}, {ready!r}"
                    break
                if not db_path.is_file():
                    last_error = "ready app did not create the configured fresh database"
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
            "Athena process smoke passed: fresh database ready and bounded stop"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
