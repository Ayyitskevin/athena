"""Outbound webhooks — the PUSH twin of the pull event feed.

`GET /events` (events_api.py) lets a consumer drain the audit trail when it wants.
A webhook reverses that: Athena POSTs each new event to a registered URL as it
happens, HMAC-signed so the receiver can trust the sender. Delivery is CURSOR-based
over the existing `activity` log — each webhook stores the last activity id it
received, and a background loop (started in main.lifespan) advances that cursor as
it delivers. The audit log stays the single source of truth; this module only
tracks where each subscriber is and how to reach it safely.

Three concerns live here, deliberately separable for testing:
  * data access (create/list/get/delete + cursor/health updates);
  * `is_safe_url` — an SSRF guard run at registration AND before every delivery;
  * `deliver_pending` — one synchronous delivery pass with the HTTP POST injected,
    so the ordering/backoff/signing logic is unit-testable without real network or
    background-loop timing. The live loop is a thin wrapper that injects a real
    urllib poster via a worker thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import urllib.error
import urllib.request
from urllib.parse import urlparse

from athena import config
from athena.core import activity, db

# Raw secrets carry a short prefix so humans recognize them, like API tokens.
SECRET_PREFIX = "whsec_"

# Backoff for a failing endpoint: the loop skips a webhook until next_attempt_at.
# Grows geometrically with consecutive failures, capped, so a dead receiver is
# retried ever more slowly instead of being hammered every tick.
_BACKOFF_BASE_SECONDS = 10
_BACKOFF_CAP_SECONDS = 3600

# Columns safe to return to a caller — never the signing secret.
_PUBLIC_COLS = (
    "id, url, event_kind, active, cursor, failure_count, last_error, "
    "last_attempt_at, next_attempt_at, last_success_at, created_by, created_at"
)


# --- SSRF guard -------------------------------------------------------------


def is_safe_url(url: str) -> tuple[bool, str]:
    """Whether Athena may POST to this URL. Returns (ok, reason).

    The app makes the request, so an attacker who can register a URL could probe
    the internal network or the cloud metadata endpoint (classic SSRF). We require
    http/https and reject any host that resolves to a private, loopback, link-local,
    reserved, multicast, or unspecified address. We resolve here and re-check at
    delivery time, so a DNS record flipped to an internal IP after registration is
    still refused."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "invalid url"
    if parsed.scheme not in ("http", "https"):
        return False, "url must be http or https"
    host = parsed.hostname
    if not host:
        return False, "url has no host"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "host does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, "url resolves to a disallowed (internal) address"
    return True, ""


# --- signing ----------------------------------------------------------------


def sign(secret: str, body: bytes) -> str:
    """The webhook signature header value: 'sha256=<hex HMAC of the body>'.

    The receiver recomputes this with its copy of the secret and a constant-time
    compare; a mismatch means the payload wasn't sent by us (or was tampered)."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- data access ------------------------------------------------------------


def _public_row(row: sqlite3.Row) -> dict:
    return dict(row)


def current_tip(conn: sqlite3.Connection) -> int:
    """The largest activity id right now, or 0 if the log is empty. A new webhook
    starts here so it receives only events from registration onward."""
    row = conn.execute("SELECT MAX(id) AS m FROM activity").fetchone()
    return row["m"] or 0


def create_webhook(
    conn: sqlite3.Connection,
    *,
    url: str,
    created_by: int,
    event_kind: str | None = None,
    start_cursor: int = 0,
) -> dict:
    """Register a webhook. Generates and returns a one-time `secret` (stored so we
    can sign, never shown again by the read paths). start_cursor is normally the
    current activity tip so only future events are delivered. URL safety is the
    boundary's job (is_safe_url); this layer only persists."""
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO webhooks (url, secret, event_kind, cursor, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (url, secret, event_kind, start_cursor, created_by),
    )
    conn.commit()
    row = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM webhooks WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return {**_public_row(row), "secret": secret}


def list_webhooks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM webhooks ORDER BY id"
    ).fetchall()
    return [_public_row(r) for r in rows]


def get_webhook(conn: sqlite3.Connection, webhook_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM webhooks WHERE id = ?", (webhook_id,)
    ).fetchone()
    return _public_row(row) if row else None


def delete_webhook(conn: sqlite3.Connection, webhook_id: int) -> bool:
    cur = conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    conn.commit()
    return cur.rowcount > 0


# --- delivery ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """Format a moment as the 'YYYY-MM-DD HH:MM:SS' text the schema uses, so stored
    timestamps compare lexicographically in chronological order."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _backoff_seconds(failure_count: int) -> int:
    return min(_BACKOFF_BASE_SECONDS * (2 ** max(failure_count - 1, 0)), _BACKOFF_CAP_SECONDS)


def _event_payload(event: dict) -> dict:
    """The JSON body delivered for one event — the same shape GET /events returns,
    so a receiver and a poller see identical data."""
    return {
        "id": event["id"],
        "actor_id": event["actor_id"],
        "actor_name": event["actor_name"],
        "verb": event["verb"],
        "target_kind": event["target_kind"],
        "target_id": event["target_id"],
        "detail": event["detail"],
        "created_at": event["created_at"],
    }


def _record_success(
    conn: sqlite3.Connection, webhook_id: int, cursor: int, now: datetime
) -> None:
    """Advance the cursor past a delivered event and clear any failure state."""
    stamp = _stamp(now)
    conn.execute(
        "UPDATE webhooks SET cursor = ?, failure_count = 0, last_error = NULL, "
        "last_attempt_at = ?, last_success_at = ?, next_attempt_at = NULL "
        "WHERE id = ?",
        (cursor, stamp, stamp, webhook_id),
    )
    conn.commit()


def _record_failure(
    conn: sqlite3.Connection,
    webhook_id: int,
    failure_count: int,
    error: str,
    now: datetime,
) -> None:
    """Record a failed attempt and arm the backoff gate; the cursor is left where
    it was so the same event is retried (at-least-once, in order)."""
    new_count = failure_count + 1
    next_at = _stamp(now + timedelta(seconds=_backoff_seconds(new_count)))
    conn.execute(
        "UPDATE webhooks SET failure_count = ?, last_error = ?, "
        "last_attempt_at = ?, next_attempt_at = ? WHERE id = ?",
        (new_count, error[:500], _stamp(now), next_at, webhook_id),
    )
    conn.commit()


# A poster sends one delivery and reports (ok, error). Injected so deliver_pending
# is testable without real network; the live loop passes urllib_poster.
Poster = Callable[[str, bytes, dict], "tuple[bool, str | None]"]


def deliver_pending(
    conn: sqlite3.Connection,
    *,
    poster: Poster,
    now: datetime | None = None,
    max_batch: int = 20,
) -> int:
    """Run ONE delivery pass and return how many events were delivered.

    For each active webhook whose backoff gate has passed: re-check the URL is safe,
    fetch the next batch of events after its cursor (filtered to its event_kind),
    and POST them in id order. A delivered event advances the cursor and clears
    failure state; the first failure records the error, arms backoff, and stops THIS
    webhook's batch (so its events stay ordered and are retried), then moves on to
    the next webhook. Other webhooks are unaffected by one bad endpoint."""
    now = now or _now()
    gate = _stamp(now)
    due = conn.execute(
        f"SELECT {_PUBLIC_COLS}, secret FROM webhooks "
        "WHERE active = 1 AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY id",
        (gate,),
    ).fetchall()

    delivered = 0
    for wh in due:
        ok_url, reason = is_safe_url(wh["url"])
        if not ok_url:
            _record_failure(conn, wh["id"], wh["failure_count"], f"unsafe url: {reason}", now)
            continue
        events = activity.list_events(
            conn,
            after_id=wh["cursor"],
            target_kind=wh["event_kind"],
            limit=max_batch,
        )
        failure_count = wh["failure_count"]
        for event in events:
            body = json.dumps(_event_payload(event), separators=(",", ":")).encode()
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Athena-Webhook",
                "X-Athena-Event-Id": str(event["id"]),
                "X-Athena-Delivery": f"{wh['id']}-{event['id']}",
                "X-Athena-Signature": sign(wh["secret"], body),
            }
            ok, error = poster(wh["url"], body, headers)
            if ok:
                _record_success(conn, wh["id"], event["id"], now)
                failure_count = 0
                delivered += 1
            else:
                _record_failure(conn, wh["id"], failure_count, error or "delivery failed", now)
                break  # keep this webhook's events ordered; retry from the cursor
    return delivered


def urllib_poster(timeout: float) -> Poster:
    """A real Poster backed by stdlib urllib (no extra dependency). Returns a
    callable bound to a per-request timeout so one slow receiver can't stall the
    delivery loop. Any non-2xx or transport error is a failure with a short reason."""

    def _post(url: str, body: bytes, headers: dict) -> tuple[bool, str | None]:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                code = response.status
                if 200 <= code < 300:
                    return True, None
                return False, f"http {code}"
        except urllib.error.HTTPError as exc:
            return False, f"http {exc.code}"
        except Exception as exc:  # noqa: BLE001 — any transport error is just a failed delivery
            return False, str(exc)[:200]

    return _post


# --- background loop (wired into main.lifespan) -----------------------------


def run_delivery_pass(db_path: str | Path) -> int:
    """Open a short-lived connection and run one delivery pass with the real
    urllib poster. Synchronous (the live loop calls it in a worker thread) and
    self-contained, so the event loop is never blocked on network I/O."""
    conn = db.connect(db_path)
    try:
        return deliver_pending(
            conn, poster=urllib_poster(config.WEBHOOK_TIMEOUT_SECONDS)
        )
    finally:
        conn.close()


async def delivery_loop(db_path: str | Path) -> None:
    """Forever: run a delivery pass in a worker thread, then sleep the interval.
    Started/cancelled by main.lifespan. A pass that raises is swallowed so one bad
    tick never kills the loop; cancellation (shutdown) propagates out cleanly."""
    while True:
        try:
            await asyncio.to_thread(run_delivery_pass, db_path)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed pass must not stop the loop
            pass
        await asyncio.sleep(config.WEBHOOK_DELIVERY_INTERVAL_SECONDS)
