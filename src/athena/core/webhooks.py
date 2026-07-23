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
from datetime import datetime, timedelta
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
from urllib.parse import urlparse

from athena import config
from athena.core._util import utc_now
from athena.core import activity, db

logger = logging.getLogger(__name__)

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


class _UnsafeAddress(Exception):
    """A URL resolved to an address Athena must not connect to (SSRF guard)."""


def _address_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an IP is one we refuse to POST to: private, loopback, link-local
    (which includes the 169.254.169.254 cloud-metadata endpoint), reserved,
    multicast, or unspecified. The single owner of the address policy — both the
    registration-time is_safe_url and the delivery-time connect consult it, so the
    two can never disagree about what "internal" means.

    IPv6 forms that EMBED an IPv4 address (IPv4-mapped ::ffff:a.b.c.d, 6to4, teredo)
    are decoded to that IPv4 before classifying: the kernel routes them to the v4
    target, so an attacker could otherwise smuggle an internal v4 (e.g.
    ::ffff:169.254.169.254) past the policy. Decoding here also makes the decision
    independent of the interpreter — CPython only began delegating mapped-address
    classification to the embedded v4 in 3.12.4, and never does so for 6to4."""
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip.teredo is not None:
            embedded = ip.teredo[1]  # the (attacker-chosen) client IPv4
        if embedded is not None:
            ip = embedded
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_safe_url(url: str) -> tuple[bool, str]:
    """Whether Athena may POST to this URL. Returns (ok, reason).

    The app makes the request, so an attacker who can register a URL could probe
    the internal network or the cloud metadata endpoint (classic SSRF). We require
    http/https and reject any host that resolves to a private, loopback, link-local,
    reserved, multicast, or unspecified address. This is the first-line check, run at
    registration and before every delivery pass. It is necessary but not sufficient on
    its own: the delivery poster re-validates and PINS the connection to the vetted IP
    (see _safe_connect_target) so a record flipped to an internal IP after this check —
    or a redirect to one — still cannot be reached."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "invalid url"
    if parsed.scheme not in ("http", "https"):
        return False, "url must be http or https"
    host = parsed.hostname
    if not host:
        return False, "url has no host"
    try:
        # .port parses lazily and raises on an out-of-range / non-numeric port.
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False, "invalid port"
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        # gaierror: no such host. UnicodeError: a pathological IDNA hostname. Both
        # are "can't safely resolve" — fail closed rather than let the exception
        # escape and abort the whole delivery pass.
        return False, "host does not resolve"
    for info in infos:
        if _address_blocked(ipaddress.ip_address(info[4][0])):
            return False, "url resolves to a disallowed (internal) address"
    return True, ""


def _safe_connect_target(host: str, port: int) -> tuple[int, tuple]:
    """Resolve host and return one validated ``(family, sockaddr)`` to connect to.

    Fails closed: EVERY resolved address must be public, so a split public/internal
    DNS answer can't let the connect pick the internal one. The caller then connects
    to exactly this address with no further hostname lookup — so DNS rebinding (a
    record that flips to an internal IP between is_safe_url and delivery) is defeated:
    the address we vetted is the address we talk to. Raises _UnsafeAddress otherwise."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise _UnsafeAddress("host does not resolve") from exc
    for info in infos:
        if _address_blocked(ipaddress.ip_address(info[4][0])):
            raise _UnsafeAddress("url resolves to a disallowed (internal) address")
    return infos[0][0], infos[0][4]


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
    commit: bool = True,
) -> dict:
    """Register a webhook. Generates and returns a one-time `secret` (stored so we
    can sign, never shown again by the read paths). start_cursor is normally the
    current activity tip so only future events are delivered. URL safety is the
    boundary's job (is_safe_url); this layer only persists. ``commit=False`` lets an
    audited command fold the registration and its activity event into one transaction."""
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO webhooks (url, secret, event_kind, cursor, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (url, secret, event_kind, start_cursor, created_by),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM webhooks WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return {**_public_row(row), "secret": secret}


def list_webhooks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"SELECT {_PUBLIC_COLS} FROM webhooks ORDER BY id").fetchall()
    return [_public_row(r) for r in rows]


def get_webhook(conn: sqlite3.Connection, webhook_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM webhooks WHERE id = ?", (webhook_id,)
    ).fetchone()
    return _public_row(row) if row else None


def set_webhook_active(
    conn: sqlite3.Connection, webhook_id: int, active: bool, *, commit: bool = True
) -> dict | None:
    """Pause (active=False) or resume (active=True) a webhook WITHOUT losing its
    cursor — so a paused endpoint resumes exactly where it left off rather than
    replaying or skipping events. Resuming also clears the backoff gate
    (failure_count / last_error / next_attempt_at) so a re-enabled endpoint is
    retried promptly: an operator flips this back on after fixing the receiver, and
    shouldn't have to wait out a stale backoff. Returns the updated row, or None if
    there is no such webhook. ``commit=False`` lets an audited command fold the flip
    and its activity event into one transaction."""
    if active:
        cur = conn.execute(
            "UPDATE webhooks SET active = 1, failure_count = 0, last_error = NULL, "
            "next_attempt_at = NULL WHERE id = ?",
            (webhook_id,),
        )
    else:
        cur = conn.execute("UPDATE webhooks SET active = 0 WHERE id = ?", (webhook_id,))
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_webhook(conn, webhook_id)


def reset_cursor(
    conn: sqlite3.Connection, webhook_id: int, cursor: int, *, commit: bool = True
) -> dict | None:
    """Point a webhook's delivery cursor at `cursor` and return the updated row.

    Used by the registration command to start a fresh endpoint at the tip AFTER its
    own registration event, so it replays no history and is never notified of its own
    creation. Internal bootstrap only — not wired to any transport (rewinding a cursor
    would replay history at an endpoint). ``commit=False`` composes inside a command."""
    conn.execute("UPDATE webhooks SET cursor = ? WHERE id = ?", (cursor, webhook_id))
    if commit:
        conn.commit()
    return get_webhook(conn, webhook_id)


def delete_webhook(
    conn: sqlite3.Connection, webhook_id: int, *, commit: bool = True
) -> bool:
    """Delete a webhook. Returns True if one was removed, False if there was no such
    row. ``commit=False`` lets an audited command fold the delete and its activity
    event into one transaction."""
    cur = conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    if commit:
        conn.commit()
    return cur.rowcount > 0


# --- delivery ---------------------------------------------------------------


def _stamp(moment: datetime) -> str:
    """Format a moment as the 'YYYY-MM-DD HH:MM:SS' text the schema uses, so stored
    timestamps compare lexicographically in chronological order."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _backoff_seconds(failure_count: int) -> int:
    return min(
        _BACKOFF_BASE_SECONDS * (2 ** max(failure_count - 1, 0)), _BACKOFF_CAP_SECONDS
    )


def _event_payload(event: dict) -> dict:
    """The JSON body delivered for one event — a stable SUBSET of the row GET /events
    returns (identity, verb, target, detail, timestamp) plus its run/lineage
    coordinates, so a push consumer can mirror Mission Control (group by run, walk
    parent/child lineage, place a fork) without polling /activity/runs. The three
    lineage keys are ALWAYS present for a stable schema and are null for an untagged
    or top-level event — the same shape GET /events exposes."""
    return {
        "id": event["id"],
        "actor_id": event["actor_id"],
        "actor_name": event["actor_name"],
        "verb": event["verb"],
        "target_kind": event["target_kind"],
        "target_id": event["target_id"],
        "detail": event["detail"],
        "created_at": event["created_at"],
        "run_id": event["run_id"],
        "parent_run_id": event["parent_run_id"],
        "forked_from_event_id": event["forked_from_event_id"],
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
    now = now or utc_now()
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
            _record_failure(
                conn, wh["id"], wh["failure_count"], f"unsafe url: {reason}", now
            )
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
            try:
                ok, error = poster(wh["url"], body, headers)
            except Exception as exc:  # noqa: BLE001 — a poster that raises is one bad
                # delivery, not a reason to abort the pass for every other webhook.
                ok, error = False, str(exc)[:200]
            if ok:
                _record_success(conn, wh["id"], event["id"], now)
                failure_count = 0
                delivered += 1
            else:
                _record_failure(
                    conn, wh["id"], failure_count, error or "delivery failed", now
                )
                break  # keep this webhook's events ordered; retry from the cursor
    return delivered


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """An HTTPConnection that dials a pre-validated IP instead of re-resolving
    ``self.host``. ``self.host`` stays the hostname so the Host header is still
    correct; only the socket target is pinned."""

    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # Match http.client.HTTPConnection.connect, which disables Nagle for latency.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS twin: dial the pinned IP, but validate the certificate against the
    ORIGINAL hostname (``server_hostname=self.host``) so pinning never weakens TLS —
    the cert must still match the name the operator registered, not the raw IP."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_ip: str,
        timeout: float,
        context: ssl.SSLContext,
    ):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def urllib_poster(timeout: float) -> Poster:
    """A real Poster backed by the stdlib http.client. Bound to a per-request timeout
    so one slow receiver can't stall the delivery loop.

    Hardened against SSRF at DELIVERY time, not just registration — is_safe_url alone
    leaves two holes that this closes:
      * It resolves + validates the host and connects to that EXACT IP, so a DNS
        record that rebinds to an internal address after is_safe_url can't be reached.
      * It does NOT follow redirects — a 3xx (e.g. 302 -> http://169.254.169.254/) is a
        delivery failure, never an internal fetch. (http.client, unlike urllib's opener,
        never auto-follows, so refusing is simply the default here.)
    Any non-2xx or transport error is a failure with a short reason. Egress is DIRECT:
    unlike urllib's opener this ignores HTTP(S)_PROXY, on purpose — a forward proxy
    would re-resolve the host and break the IP pin that defeats rebinding."""

    def _post(url: str, body: bytes, headers: dict) -> tuple[bool, str | None]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "url must be http or https"
        host = parsed.hostname
        if not host:
            return False, "url has no host"
        try:
            # .port raises on an out-of-range / non-numeric port; a bad row must be a
            # clean per-delivery failure, never an exception that aborts the pass.
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return False, "invalid port"
        try:
            _family, sockaddr = _safe_connect_target(host, port)
        except _UnsafeAddress as exc:
            return False, str(exc)
        pinned_ip = sockaddr[0]
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        # Build the connection INSIDE the try so a TLS-context or construction error
        # also degrades to a normal (False, reason) delivery failure.
        conn: http.client.HTTPConnection | None = None
        try:
            if parsed.scheme == "https":
                conn = _PinnedHTTPSConnection(
                    host,
                    port,
                    pinned_ip=pinned_ip,
                    timeout=timeout,
                    context=ssl.create_default_context(),
                )
            else:
                conn = _PinnedHTTPConnection(
                    host, port, pinned_ip=pinned_ip, timeout=timeout
                )
            conn.request("POST", target, body=body, headers=headers)
            response = conn.getresponse()
            code = response.status
            response.read()  # drain the body so the connection closes cleanly
            if 200 <= code < 300:
                return True, None
            if 300 <= code < 400:
                # A redirect is refused, never followed: the Location could point at an
                # internal address the registration check never saw.
                return False, f"refused redirect (http {code})"
            return False, f"http {code}"
        except Exception as exc:  # noqa: BLE001 — any transport error is just a failed delivery
            return False, str(exc)[:200]
        finally:
            if conn is not None:
                conn.close()

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
            logger.warning(
                "webhook delivery pass failed; loop continues", exc_info=True
            )
        await asyncio.sleep(config.WEBHOOK_DELIVERY_INTERVAL_SECONDS)
