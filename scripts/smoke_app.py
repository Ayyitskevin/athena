"""Boot Athena as a real process and verify its deploy-facing health contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen


EXPECTED_HEALTH = {"status": "ok"}
EXPECTED_READY = {"status": "ok", "database": "ok"}
STARTUP_TIMEOUT_SECONDS = 15


def _read_json(url: str) -> dict:
    with urlopen(url, timeout=1) as response:  # noqa: S310 - loopback smoke only
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="athena-process-smoke-") as temp_dir:
        root = Path(temp_dir)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        env = os.environ.copy()
        env.update(
            {
                "ATHENA_ATTACH_DIR": str(root / "attachments"),
                "ATHENA_AUTOMATION": "0",
                "ATHENA_DB": str(root / "athena.db"),
                "ATHENA_LOG_LEVEL": "WARNING",
                "ATHENA_WEBHOOK_DELIVERY": "0",
                "PYTHONUNBUFFERED": "1",
            }
        )
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
            stdout=subprocess.PIPE,
            text=True,
        )
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
                success = True
                break
        finally:
            _stop(process)

        output = process.stdout.read() if process.stdout is not None else ""
        if not success:
            if output:
                print(output, file=sys.stderr)
            raise RuntimeError(f"Athena process smoke failed: {last_error}")

        print("Athena process smoke passed: /healthz and /readyz are ready")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
