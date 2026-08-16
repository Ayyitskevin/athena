"""Optional Buzz CLI radio for fleet assignments.

Athena does not store a Buzz secret. If ATHENA_BUZZ_CLI, ATHENA_BUZZ_KEY_FILE,
and ATHENA_BUZZ_RELAY_URL are set, this module shells out to the existing CLI.
A missing radio is a skipped ping, not a failed assignment.
"""

from __future__ import annotations

from pathlib import Path
import os
import subprocess

from athena import config


class RadioError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _load_private_key(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("SECKEY="):
            return line.split("=", 1)[1].strip()
        if line.startswith("BUZZ_PRIVATE_KEY="):
            return line.split("=", 1)[1].strip()
    raise RadioError("key file has no SECKEY or BUZZ_PRIVATE_KEY")


def assignment_message(
    *,
    seat_name: str,
    issue_key: str,
    title: str,
    url: str,
    note: str,
) -> str:
    extra = f"\n\nNote: {note.strip()}" if note.strip() else ""
    return (
        f"ATHENA_ASSIGN {issue_key}\n\n"
        f"@{seat_name} — new assignment, not a steer. "
        f"Park other work after your current reply.\n\n"
        f"{title}\n{url}\n\n"
        f"Desk → claim (If-Match from issue_etag) → work only this issue.{extra}"
    )


def send_assignment(
    *,
    seat_name: str,
    buzz_pubkey: str | None,
    issue_key: str,
    title: str,
    url: str,
    note: str = "",
    runner: object | None = None,
) -> dict:
    """Post one assignment ping. Returns {status, detail}."""
    if not config.buzz_radio_configured():
        return {"status": "skipped", "detail": "buzz radio is not configured"}
    if not buzz_pubkey:
        return {"status": "skipped", "detail": "seat has no Buzz pubkey"}
    cli = config.buzz_cli_path()
    relay = config.buzz_relay_url()
    channel = config.buzz_assign_channel()
    try:
        secret = _load_private_key(config.buzz_key_file())
    except OSError as exc:
        return {"status": "failed", "detail": f"cannot read key file: {exc}"}
    except RadioError as exc:
        return {"status": "failed", "detail": exc.detail}

    body = assignment_message(
        seat_name=seat_name,
        issue_key=issue_key,
        title=title,
        url=url,
        note=note,
    )
    argv = [
        cli,
        "messages",
        "send",
        "--channel",
        channel,
        "--mention",
        buzz_pubkey,
        "--content",
        body,
    ]
    env = os.environ.copy()
    env["BUZZ_RELAY_URL"] = relay
    env["BUZZ_PRIVATE_KEY"] = secret
    run = runner if runner is not None else subprocess.run
    try:
        completed = run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "failed", "detail": type(exc).__name__}
    if getattr(completed, "returncode", 1) != 0:
        err = (getattr(completed, "stderr", "") or "").strip()
        return {"status": "failed", "detail": err or "buzz cli failed"}
    return {"status": "sent", "detail": "posted to command-deck"}
