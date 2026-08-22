"""Optional Buzz CLI radio for fleet assignments.

Athena does not store a Buzz secret. If ATHENA_BUZZ_CLI, ATHENA_BUZZ_KEY_FILE,
and ATHENA_BUZZ_RELAY_URL are set, this module shells out to the existing CLI.
A missing radio is a skipped ping, not a failed assignment.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import os
import subprocess
from typing import Any

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


#: The longest human-authored field allowed inside a directive frame. A title is
#: display context, not payload; a long one is truncated rather than allowed to
#: push the frame off a reader's screen.
_FIELD_MAX_CHARS = 200

#: The prefix every Athena directive frame opens with. Human text that could be
#: read as one is neutralized by ``_one_line`` below.
_DIRECTIVE_PREFIX = "ATHENA_"


def _one_line(text: str, *, limit: int = _FIELD_MAX_CHARS) -> str:
    """Flatten human-authored text so it cannot forge a directive.

    The messages this module posts are a LINE-ORIENTED protocol read by
    autonomous seats: a line beginning ``ATHENA_ASSIGN`` is an instruction to
    park other work and take a job. Issue titles are author-supplied and only
    stripped, so interpolating one verbatim would let anyone who can file an
    issue emit a second, forged directive inside a real message signed with
    Athena's own key — escalating "can file an issue" into "can direct a seat".

    Collapsing every whitespace run (newlines included) to a single space is
    what actually closes that: a forged directive can no longer START a line.
    The ``ATHENA_`` token is additionally down-cased (the protocol is
    case-sensitive) so it does not read as a directive even inline, and the
    result is capped so a huge title cannot bury the real frame.
    """
    flattened = " ".join(str(text).split())
    flattened = flattened.replace(_DIRECTIVE_PREFIX, _DIRECTIVE_PREFIX.lower())
    if len(flattened) > limit:
        flattened = flattened[: limit - 1].rstrip() + "…"
    return flattened


def assignment_message(
    *,
    seat_name: str,
    issue_key: str,
    title: str,
    url: str,
    note: str,
) -> str:
    safe_note = _one_line(note)
    extra = f"\n\nNote: {safe_note}" if safe_note else ""
    return (
        f"ATHENA_ASSIGN {issue_key}\n\n"
        f"@{_one_line(seat_name, limit=64)} — new assignment, not a steer. "
        f"Park other work after your current reply.\n\n"
        f"{_one_line(title)}\n{url}\n\n"
        f"Desk → claim (If-Match from issue_etag) → work only this issue.{extra}"
    )


def event_message(
    *,
    verb: str,
    issue_key: str,
    title: str,
    url: str,
    actor_name: str = "",
    note: str = "",
) -> str:
    """The deterministic body an automation rule posts for one issue event.

    Composed from the event and the issue alone — rules carry no template
    language, only an optional operator-authored note, so what a rule can say
    is inspectable from this one function. Every human-authored field goes
    through ``_one_line`` first: a rule fires automatically on issues anyone may
    file, so an unflattened title here would be a directive-forgery broadcast.
    """
    safe_actor = _one_line(actor_name, limit=64)
    actor = f" by {safe_actor}" if safe_actor else ""
    safe_note = _one_line(note)
    extra = f"\n\nNote: {safe_note}" if safe_note else ""
    return (
        f"ATHENA_EVENT {_one_line(verb, limit=64)} {issue_key}{actor}\n\n"
        f"{_one_line(title)}\n{url}"
        f"{extra}"
    )


def send_channel_message(
    *,
    channel: str,
    content: str,
    mention: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict:
    """Post one message to one channel over the CLI. Returns {status, detail}.

    The shared transport under every radio use: same optional-by-config skip,
    same fail-soft error shape. The CLI inherits the process environment (plus
    the relay URL and key), so a BUZZ_AUTH_TAG in the service env rides along.
    """
    if not config.buzz_radio_configured():
        return {"status": "skipped", "detail": "buzz radio is not configured"}
    cli = config.buzz_cli_path()
    relay = config.buzz_relay_url()
    try:
        secret = _load_private_key(config.buzz_key_file())
    except OSError as exc:
        return {"status": "failed", "detail": f"cannot read key file: {exc}"}
    except RadioError as exc:
        return {"status": "failed", "detail": exc.detail}

    argv = [cli, "messages", "send", "--channel", channel]
    if mention:
        argv += ["--mention", mention]
    argv += ["--content", content]
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
    return {"status": "sent", "detail": f"posted to {channel}"}


def send_assignment(
    *,
    seat_name: str,
    buzz_pubkey: str | None,
    issue_key: str,
    title: str,
    url: str,
    note: str = "",
    runner: Callable[..., Any] | None = None,
) -> dict:
    """Post one assignment ping. Returns {status, detail}."""
    if not config.buzz_radio_configured():
        return {"status": "skipped", "detail": "buzz radio is not configured"}
    if not buzz_pubkey:
        return {"status": "skipped", "detail": "seat has no Buzz pubkey"}
    body = assignment_message(
        seat_name=seat_name,
        issue_key=issue_key,
        title=title,
        url=url,
        note=note,
    )
    result = send_channel_message(
        channel=config.buzz_assign_channel(),
        content=body,
        mention=buzz_pubkey,
        runner=runner,
    )
    if result.get("status") == "sent":
        return {"status": "sent", "detail": "posted to command-deck"}
    return result
