"""Declared fleet seats plus what this host can actually observe.

The 8→9 gap was not missing agents — it was missing a single page that
answers: who did we say is on this box, is their systemd unit up, and do
they have an Athena account? This module is that read. It does not start
units, send Buzz messages, or claim a process is alive.
"""

from __future__ import annotations

from datetime import UTC, datetime
import socket
import sqlite3
import subprocess
from typing import Callable

from athena.core import users, workers

SCHEMA = "athena.fleet_roster.v1"
DEFAULT_ASSIGN_CHANNEL = "3fc2b270-cd0b-4a6b-afcd-f10471caffb2"  # command-deck

# Public Buzz identities and unit names. These are not secrets.
# email is the Athena handle when the seat has been onboarded.
DECLARED_SEATS: tuple[dict[str, str | None], ...] = (
    {
        "slug": "kevin",
        "name": "Kevin",
        "email": "kevin@athena.local",
        "unit": None,
        "buzz_pubkey": "2bee90a820486a2b2e8ea97e8b058f4201cd6eac6ac15e98d9727bccd8f6f580",
        "kind": "operator",
    },
    {
        "slug": "claude",
        "name": "Claude",
        "email": "claude@agents.local",
        "unit": "buzz-acp-claude.service",
        "buzz_pubkey": "c55e70289ba2cb6f5acb2ab0c9c39a6f20d23000ff57c58d568193dbaab20041",
        "kind": "seat",
    },
    {
        "slug": "codex",
        "name": "Codex",
        "email": "codex@agents.local",
        "unit": "buzz-acp-codex.service",
        "buzz_pubkey": "c92269929d4b5d48282f39858a43d90737edfce526bf6a3a6899b5e6f5abe3f8",
        "kind": "seat",
    },
    {
        "slug": "kimi",
        "name": "Kimi",
        "email": "kimi@agents.local",
        "unit": "buzz-acp-kimi.service",
        "buzz_pubkey": "537d7a475446bb89f0429068a7810f8f9d550371e7ab6be5ae89b3b94b75220e",
        "kind": "seat",
    },
    {
        "slug": "grok",
        "name": "Grok",
        "email": "grok@agents.local",
        "unit": "buzz-acp-grok.service",
        "buzz_pubkey": "f6f7c025959f0a215184e8e92e623dd5a798f86024df191c00c1674fbb191c8e",
        "kind": "seat",
    },
    {
        "slug": "glm",
        "name": "GLM",
        "email": None,
        "unit": "buzz-acp-glm.service",
        "buzz_pubkey": "6c390afaff47b4aaacda404a60694fae43e86f071c15d7eae3abf5fe009fdc6f",
        "kind": "seat",
    },
    {
        "slug": "muse",
        "name": "Muse",
        "email": None,
        "unit": "buzz-acp-muse.service",
        "buzz_pubkey": None,
        "kind": "local_ollama",
    },
    {
        "slug": "nemotron",
        "name": "Nemotron",
        "email": None,
        "unit": "buzz-acp-nemotron.service",
        "buzz_pubkey": None,
        "kind": "local_ollama",
    },
    {
        "slug": "qwen",
        "name": "Qwen",
        "email": None,
        "unit": "buzz-acp-qwen.service",
        "buzz_pubkey": None,
        "kind": "local_ollama",
    },
)

UnitProbe = Callable[[str], dict]


def find_declared_seat(slug: str) -> dict[str, str | None] | None:
    wanted = slug.strip().lower()
    for spec in DECLARED_SEATS:
        if str(spec["slug"]).lower() == wanted:
            return spec
    return None


def seat_slug_for_email(email: str | None) -> str | None:
    """Map an Athena email to the declared fleet slug, or None if undeclared."""
    if not email:
        return None
    wanted = email.strip().lower()
    for spec in DECLARED_SEATS:
        listed = spec.get("email")
        if listed and str(listed).lower() == wanted:
            return str(spec["slug"])
    return None


def assignable_seat_slugs() -> tuple[str, ...]:
    return tuple(
        str(spec["slug"])
        for spec in DECLARED_SEATS
        if spec.get("kind") != "operator" and spec.get("email")
    )


def probe_systemd_user_unit(unit: str) -> dict:
    """Ask this host's user systemd about one unit.

    Returns LoadState / ActiveState / SubState as systemd printed them, or
    ``unobserved`` if we could not ask. Never invents 'running' from silence.
    """
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,SubState",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {
            "load_state": "unobserved",
            "active_state": "unobserved",
            "sub_state": "unobserved",
            "detail": type(exc).__name__,
        }
    parsed = {
        "load_state": "unknown",
        "active_state": "unknown",
        "sub_state": "unknown",
    }
    for line in (completed.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "LoadState":
            parsed["load_state"] = value.strip() or "unknown"
        elif key == "ActiveState":
            parsed["active_state"] = value.strip() or "unknown"
        elif key == "SubState":
            parsed["sub_state"] = value.strip() or "unknown"
    if completed.returncode != 0 and parsed["load_state"] == "unknown":
        parsed["detail"] = (
            completed.stderr or ""
        ).strip() or f"exit {completed.returncode}"
    return parsed


def unit_verdict(probe: dict) -> str:
    """One word the page can chip. Not 'alive'."""
    if probe.get("load_state") == "not-found":
        return "missing"
    if probe.get("active_state") == "active":
        return "active"
    if probe.get("active_state") == "unobserved":
        return "unobserved"
    if probe.get("unit") is None and "active_state" not in probe:
        return "none"
    return "inactive"


def _athena_account(conn: sqlite3.Connection, email: str | None) -> dict | None:
    if not email:
        return None
    found = users.get_user_by_email(conn, email)
    if found is None:
        return None
    return {
        "id": found["id"],
        "name": found["name"],
        "email": found["email"],
        "role": found["role"],
        "is_agent": bool(found.get("is_agent")),
        "paused": found.get("paused_at") is not None,
    }


def _latest_worker(conn: sqlite3.Connection, agent_id: int) -> dict | None:
    listed = workers.list_workers(conn, agent_id=agent_id, limit=1)
    if not listed:
        return None
    row = listed[0]
    return {
        "id": row["id"],
        "worker_key": row["worker_key"],
        "node_label": row["node_label"],
        "last_seen_at": row["last_seen_at"],
        "reporting_state": row["reporting_state"],
        "kill_state": row["kill_state"],
    }


def build_roster(
    conn: sqlite3.Connection,
    *,
    probe: UnitProbe | None = None,
    now: datetime | None = None,
    declared: tuple[dict[str, str | None], ...] | None = None,
) -> dict:
    """Compose the operator roster. ``probe`` is injectable so tests do not
    talk to systemd."""
    probe_fn = probe_systemd_user_unit if probe is None else probe
    clock = datetime.now(UTC) if now is None else now
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    seats_in = declared if declared is not None else DECLARED_SEATS

    seats: list[dict] = []
    claimed_emails: set[str] = set()
    for spec in seats_in:
        email = spec.get("email")
        if email:
            claimed_emails.add(str(email).lower())
        unit = spec.get("unit")
        unit_probe = (
            {"load_state": "none", "active_state": "none", "sub_state": "none"}
            if not unit
            else probe_fn(str(unit))
        )
        verdict = "none" if not unit else unit_verdict(unit_probe)
        account = _athena_account(conn, email if isinstance(email, str) else None)
        worker = (
            _latest_worker(conn, int(account["id"])) if account is not None else None
        )
        seats.append(
            {
                "slug": spec["slug"],
                "name": spec["name"],
                "kind": spec["kind"],
                "email": email,
                "unit": unit,
                "buzz_pubkey": spec.get("buzz_pubkey"),
                "unit_probe": unit_probe,
                "unit_verdict": verdict,
                "athena": account,
                "worker": worker,
            }
        )

    undeclared: list[dict] = []
    for user in users.list_users(conn):
        if not user.get("is_agent"):
            continue
        if str(user.get("email") or "").lower() in claimed_emails:
            continue
        undeclared.append(
            {
                "id": user["id"],
                "name": user["name"],
                "email": user["email"],
                "paused": user.get("paused_at") is not None,
            }
        )

    return {
        "schema": SCHEMA,
        "observed_at": clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
        "semantics": {
            "snapshot": "this_host_plus_athena_rows",
            "does_not_assert": [
                "alive",
                "killed",
                "process_exists",
                "buzz_presence",
            ],
        },
        "seats": seats,
        "undeclared_athena_agents": undeclared,
        "summary": {
            "declared": len(seats),
            "unit_active": sum(1 for seat in seats if seat["unit_verdict"] == "active"),
            "unit_inactive": sum(
                1 for seat in seats if seat["unit_verdict"] == "inactive"
            ),
            "unit_missing": sum(
                1 for seat in seats if seat["unit_verdict"] == "missing"
            ),
            "athena_linked": sum(1 for seat in seats if seat["athena"] is not None),
            "undeclared_agents": len(undeclared),
        },
    }
