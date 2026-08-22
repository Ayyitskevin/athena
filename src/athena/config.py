"""Application configuration, read from the environment.

Keeping config in one place (and env-driven) means the same code runs on your
laptop and on a server without edits — you just point ATHENA_DB somewhere else.
"""

import math
import os
from pathlib import Path

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_NETWORK_MODES = frozenset({"local", "tailnet"})


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be an explicit true/false value")


def _int_env(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _float_env(name: str, default: float, *, minimum: float, inclusive: bool) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    valid_range = value >= minimum if inclusive else value > minimum
    if not math.isfinite(value) or not valid_range:
        qualifier = "at least" if inclusive else "greater than"
        raise ValueError(f"{name} must be finite and {qualifier} {minimum}")
    return value


# The SQLite file Athena stores everything in. Override with the ATHENA_DB env var.
DB_PATH = Path(os.environ.get("ATHENA_DB", "athena.db"))

# Athena supports a direct loopback listener and an explicitly declared Tailscale
# listener. It intentionally has no public mode: proxies, tunnels, NAT, container
# publication, and Tailscale Funnel are external state Athena cannot infer.
NETWORK_MODE = os.environ.get("ATHENA_NETWORK_MODE", "local").strip().lower()
if NETWORK_MODE not in _NETWORK_MODES:
    raise ValueError(
        "ATHENA_NETWORK_MODE must be one of: " + ", ".join(sorted(_NETWORK_MODES))
    )

# Exact request Host authorities, including ports. The supported launcher derives
# a strict loopback allowlist when this is empty in local mode; tailnet mode requires
# explicit values. Raw ASGI startup with no launcher and no allowlist consequently
# has no request authority and fails closed at the outer deployment boundary.
_allowed_authorities_raw = os.environ.get("ATHENA_ALLOWED_AUTHORITIES", "")
if _allowed_authorities_raw:
    _allowed_authority_parts = _allowed_authorities_raw.split(",")
    if any(not part.strip() for part in _allowed_authority_parts):
        raise ValueError(
            "ATHENA_ALLOWED_AUTHORITIES must contain nonempty comma-separated values"
        )
    ALLOWED_AUTHORITIES = tuple(part.strip() for part in _allowed_authority_parts)
    del _allowed_authority_parts
else:
    ALLOWED_AUTHORITIES = ()
del _allowed_authorities_raw

# Athena loggers fail closed on typos instead of silently falling back to INFO.
LOG_LEVEL = os.environ.get("ATHENA_LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in _LOG_LEVELS:
    raise ValueError(
        "ATHENA_LOG_LEVEL must be one of: " + ", ".join(sorted(_LOG_LEVELS))
    )


# Legacy application-factory fallback identity when no bearer token is presented.
# The header only CLAIMS an id, so it defaults off and the supported athena-serve
# entrypoint refuses to start when it is enabled. Retained direct-factory users
# must opt in explicitly and remain outside the supported deployment contract.
TRUST_ACTOR_HEADER = _bool_env("ATHENA_TRUST_ACTOR_HEADER", False)

# One-time credential for creating the first administrator through POST /users.
# Empty disables HTTP bootstrap entirely: an unconfigured instance must not grant
# administrator authority to whichever network caller arrives first. Generate a
# fresh value with ``python -c 'import secrets; print(secrets.token_urlsafe(32))'``,
# present it only for bootstrap, then remove it and restart Athena.
BOOTSTRAP_TOKEN = os.environ.get("ATHENA_BOOTSTRAP_TOKEN", "")
if BOOTSTRAP_TOKEN:
    try:
        bootstrap_token_bytes = BOOTSTRAP_TOKEN.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "ATHENA_BOOTSTRAP_TOKEN must be 32-255 visible ASCII characters"
        ) from exc
    if not 32 <= len(bootstrap_token_bytes) <= 255 or any(
        byte < 0x21 or byte > 0x7E for byte in bootstrap_token_bytes
    ):
        raise ValueError(
            "ATHENA_BOOTSTRAP_TOKEN must be 32-255 visible ASCII characters"
        )
    del bootstrap_token_bytes

# The supported launcher sets this process-local invariant while running
# ``athena-serve --bootstrap``. It is intentionally not an environment escape
# hatch: a supported first administrator must have a credential that still works
# after the one-time bootstrap token is removed.
BOOTSTRAP_PASSWORD_REQUIRED = False

# Maximum accepted request body size. This keeps accidental huge posts from
# tying up the app process. Set to 0 to disable here when a trusted reverse proxy
# enforces the limit instead.
MAX_REQUEST_BODY_BYTES = _int_env(
    "ATHENA_MAX_REQUEST_BODY_BYTES", 1024 * 1024, minimum=0
)

# Completed idempotency receipts are replayable for one day, while an executing
# owner gets a one-minute freshness lease and a concurrent retry waits briefly for
# its result. An expired owner is never taken over automatically: the route may
# have committed just before its worker disappeared, so re-running would risk a
# duplicate mutation. Response storage is bounded separately from request bodies.
IDEMPOTENCY_TTL_SECONDS = _int_env(
    "ATHENA_IDEMPOTENCY_TTL_SECONDS", 24 * 60 * 60, minimum=1
)
IDEMPOTENCY_LEASE_SECONDS = _int_env("ATHENA_IDEMPOTENCY_LEASE_SECONDS", 60, minimum=1)
IDEMPOTENCY_WAIT_SECONDS = _float_env(
    "ATHENA_IDEMPOTENCY_WAIT_SECONDS", 5.0, minimum=0.0, inclusive=True
)
IDEMPOTENCY_MAX_RESPONSE_BYTES = _int_env(
    "ATHENA_IDEMPOTENCY_MAX_RESPONSE_BYTES", 1024 * 1024, minimum=1
)

# Per-bearer-token request ceiling for agent/API traffic. This is in-process and
# fixed-window by design: enough to stop a runaway local agent loop without adding
# a durable coordination table. Set to 0 to disable when a reverse proxy enforces
# equivalent limits.
TOKEN_RATE_LIMIT_PER_MINUTE = _int_env(
    "ATHENA_TOKEN_RATE_LIMIT_PER_MINUTE", 120, minimum=0
)

# A run check-in newer than this threshold is reported as ``reporting_recently``;
# older rows are ``stale``. This is cooperative presence, not OS-process liveness:
# expiry never completes, revokes, kills, or transfers a run.
AGENT_RUN_STALE_SECONDS = _int_env("ATHENA_AGENT_RUN_STALE_SECONDS", 90, minimum=1)

# Cooperative check-ins are operational state, not an unbounded run archive. A
# compromised or looping token may refresh existing ids indefinitely, but cannot
# create more durable rows for one agent after this ceiling is reached.
AGENT_RUN_MAX_CHECKINS_PER_AGENT = _int_env(
    "ATHENA_AGENT_RUN_MAX_CHECKINS_PER_AGENT", 1000, minimum=1
)

# A worker heartbeat newer than this threshold reports as ``reporting_recently``;
# older rows are ``stale`` — the same cooperative-presence semantics as run
# check-ins, and just as deliberately NOT process liveness. A worker that stops
# heartbeating has stopped REPORTING; whether its process is alive is something
# Athena cannot observe and never claims.
WORKER_STALE_SECONDS = _int_env("ATHENA_WORKER_STALE_SECONDS", 90, minimum=1)

# How long a run control waits for the bound agent before it reads as expired,
# when the operator does not choose a lifetime explicitly. Expiry is derived at
# read time from the stored expires_at against the server clock — nothing sweeps,
# nothing is written, and an expired control never claims the agent did anything.
RUN_CONTROL_TTL_SECONDS = _int_env("ATHENA_RUN_CONTROL_TTL_SECONDS", 3600, minimum=60)

# The external execution fleet Athena may hand work to. BOTH must be set for
# dispatch to be available: a URL without a secret would mean sending unsigned work
# to an unauthenticated endpoint, and a secret without a URL means nothing. Unset is
# the default and dispatch is simply refused, which is the honest state for a
# deployment that has no executor.
ICARUS_URL = os.environ.get("ATHENA_ICARUS_URL", "").strip()
ICARUS_SECRET = os.environ.get("ATHENA_ICARUS_SECRET", "").strip()
ICARUS_TIMEOUT_SECONDS = _int_env("ATHENA_ICARUS_TIMEOUT_SECONDS", 10, minimum=1)

# Exact hostnames Athena may POST to even though they resolve to private,
# loopback, or link-local addresses. Empty (the default) keeps the SSRF policy
# absolute, which is the right default for a public deployment — but Athena's
# PRIMARY deployment is a solo operator's machine or tailnet, where the webhook
# receiver or the execution fleet lives at 127.0.0.1 or a LAN/CGNAT address by
# design. Without this, every dispatch to a local executor lands
# `undeliverable: url resolves to a disallowed (internal) address`, discovered
# the first time the dispatch loop was exercised against a real counterparty.
#
# The trust model holds because the CHANNEL differs: webhook URLs are registered
# at runtime through the API (attacker-reachable, so they stay guarded), while
# this list is set in the process environment by whoever owns the process — the
# same trust as ATHENA_ICARUS_SECRET itself. Matching is exact and
# case-insensitive; no wildcards, no CIDR ranges, so an entry names one host the
# operator chose. Delivery still pins the connection to the resolved address.
EGRESS_PRIVATE_HOSTS = tuple(
    host.strip().lower()
    for host in os.environ.get("ATHENA_EGRESS_PRIVATE_HOSTS", "").split(",")
    if host.strip()
)


def icarus_configured() -> bool:
    """Whether an executor is configured. Read at call time, not import time, so a
    test or a reconfigured process sees the current value."""
    return bool(ICARUS_URL and ICARUS_SECRET)


def buzz_relay_url() -> str:
    return os.environ.get("ATHENA_BUZZ_RELAY_URL", "").strip()


def buzz_cli_path() -> str:
    return os.environ.get("ATHENA_BUZZ_CLI", "").strip()


def buzz_key_file() -> str:
    return os.environ.get("ATHENA_BUZZ_KEY_FILE", "").strip()


def buzz_assign_channel() -> str:
    # command-deck on the mickey relay. Override if the channel is recreated.
    default = "3fc2b270-cd0b-4a6b-afcd-f10471caffb2"
    return os.environ.get("ATHENA_BUZZ_ASSIGN_CHANNEL", default).strip()


def public_base_url() -> str:
    return os.environ.get("ATHENA_PUBLIC_BASE_URL", "").strip().rstrip("/")


def buzz_radio_configured() -> bool:
    """CLI + key file + relay. Channel has a built-in default."""
    return bool(buzz_relay_url() and buzz_cli_path() and buzz_key_file())


# One agent may run several workers (a box per node, a process per capability), but
# not unboundedly many: a looping or compromised token can refresh the rows it has
# forever and still never grow the registry past this ceiling.
WORKER_MAX_PER_AGENT = _int_env("ATHENA_WORKER_MAX_PER_AGENT", 50, minimum=1)

# Requests slower than this are logged at WARNING with their route template,
# method, status, and duration. 0 disables it.
#
# Athena has never measured request latency anywhere — uvicorn's access log records
# method, path, and status but no timing, and nothing else in the tree times a
# request. That is the gap this closes: the F-0.1 read regression (229ms per page,
# on a feed that looked perfectly healthy in the access log) would have been
# invisible to an operator until someone thought to profile it by hand.
#
# 1000ms is deliberately high. This is a "something is wrong" signal, not a
# profiler: an operator who gets a warning per page load stops reading the
# warnings. Lower it when hunting something specific.
SLOW_REQUEST_LOG_MS = _int_env("ATHENA_SLOW_REQUEST_LOG_MS", 1000, minimum=0)

# Per-client-IP limit used by optional-identity REST reads and signed machine-
# inbound deliveries. It is not a global browser-request ceiling. The per-token
# limiter never runs for these paths. Defaults to 0 (OFF) for local use; tailnet
# deployment requires a positive value. Keyed by the direct peer IP, NOT
# X-Forwarded-For — account for a shared reverse proxy separately.
ANON_RATE_LIMIT_PER_MINUTE = _int_env("ATHENA_ANON_RATE_LIMIT_PER_MINUTE", 0, minimum=0)

# Per-client-IP limit on POST /login attempts. Password login is credential-free at the
# door (no token, no session yet), so neither the token nor the anonymous limiter guards
# it — without this a brute-force / credential-stuffing run is bounded only by pbkdf2's
# cost per guess, which throttles CPU but not the attempt rate. This caps attempts per IP
# per minute and is checked BEFORE the password hash, so it also protects against
# pbkdf2-CPU exhaustion. Defaults ON at 10/min (a human never types 10 logins a minute);
# keyed by the direct peer IP, so front a shared-IP proxy accordingly. Set 0 to disable.
LOGIN_RATE_LIMIT_PER_MINUTE = _int_env(
    "ATHENA_LOGIN_RATE_LIMIT_PER_MINUTE", 10, minimum=0
)

# The per-IP limit above bounds one peer; it does NOT bound one ACCOUNT. Credential
# stuffing is a distributed attack by construction — a thousand hosts guessing ten
# passwords each at one email stays under every per-IP ceiling while making ten
# thousand attempts on that account. This caps attempts per SUBMITTED EMAIL per
# minute, which is the axis the attacker cannot spread across.
#
# Keyed by the email exactly as submitted, NOT by the resolved user id: keying by the
# account would mean a real email is throttled and an unknown one is not, and the
# difference is observable — reintroducing the existence oracle that dummy_verify and
# the background-task failure recording exist to close. Keying by the submitted string
# makes locked, unknown and real behave identically. (users.email is case-SENSITIVE —
# no COLLATE NOCASE — so varying the case reaches a different account too, and cannot
# be used to reset the counter against a given one.)
#
# Defaults ON at 5/min: a human who has forgotten their password does not type five
# attempts in sixty seconds, and the window is short enough that the lockout cannot be
# used as a durable denial-of-service lever against a known address (see SECURITY.md).
# Set 0 to disable.
LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE = _int_env(
    "ATHENA_LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE", 5, minimum=0
)

# --- exposure posture ------------------------------------------------------
#
# Projects and spaces are PUBLIC by default (core/access.py), which is the right
# default for a single operator on loopback and the wrong one the moment the box is
# reachable by anyone else. An accidental tunnel, a Tailscale Funnel left on, a
# port-forward that outlived its reason — any of them expose every public container
# to a caller with no credential at all.
#
# Two switches, doing different jobs:
#
# ATHENA_ANONYMOUS_READS=0 is the FAIL-CLOSED one. It requires an authenticated actor
# for every read, regardless of any container's own visibility, so exposure stops
# being a disclosure. It is the switch to reach for when the box might be reachable;
# everything else here is defense in depth behind it.
#
# ATHENA_DEFAULT_VISIBILITY=private is ERGONOMICS. New projects and spaces are born
# private instead of public, so the safe state is the one you get by not thinking
# about it. It does nothing for containers that already exist, and nothing at all for
# an instance whose reads are already closed.
#
# Both default to today's behavior — reads open, containers born public — because
# changing them for existing deployments would silently break the loopback setup the
# product is documented around.
ANONYMOUS_READS = _bool_env("ATHENA_ANONYMOUS_READS", True)

_VISIBILITIES = frozenset({"public", "private"})


def _visibility_env(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in _VISIBILITIES:
        raise ValueError(
            f"{name} must be one of: {', '.join(sorted(_VISIBILITIES))} (got {raw!r})"
        )
    return normalized


DEFAULT_VISIBILITY = _visibility_env("ATHENA_DEFAULT_VISIBILITY", "public")


# --- cockpit liveness ------------------------------------------------------
#
# How often the three "right now" surfaces re-ask the server for their own markup:
# the dashboard's fleet-attention card, Mission Control's active-work table, and the
# open run-control requests. VISION promises the operator can see what each agent is
# doing right now, and a page that only changes when you press reload cannot keep
# that promise.
#
# 0 disables polling entirely — the pages still render, they just stop refreshing
# themselves, which is what an operator watching over a metered or battery-powered
# link wants. Any other value is held to [MIN, MAX] rather than accepted as typed,
# because the floor is a load statement and not a preference: at 10s each polling
# admin costs roughly 0.05 ms of server time per second (the rollup measures ~0.5 ms
# for a real fleet, see main.py), and a 1s interval would multiply that by ten
# without making anything more legible to a human eye.
LIVE_REFRESH_MIN_SECONDS = 5
LIVE_REFRESH_MAX_SECONDS = 3600


def _refresh_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer number of seconds") from exc
    if value == 0:
        return 0
    if not (LIVE_REFRESH_MIN_SECONDS <= value <= LIVE_REFRESH_MAX_SECONDS):
        raise ValueError(
            f"{name} must be 0 (no polling) or between {LIVE_REFRESH_MIN_SECONDS} "
            f"and {LIVE_REFRESH_MAX_SECONDS} seconds (got {value})"
        )
    return value


LIVE_REFRESH_SECONDS = _refresh_env("ATHENA_LIVE_REFRESH_SECONDS", 10)


# Browser session lifetime, and whether the session cookie carries the Secure
# flag (HTTPS-only). Secure defaults OFF so login works over plain http in local
# dev — turn it ON (ATHENA_COOKIE_SECURE=1) whenever Athena is served over HTTPS.
SESSION_TTL_DAYS = _int_env("ATHENA_SESSION_TTL_DAYS", 14, minimum=1)
COOKIE_SECURE = _bool_env("ATHENA_COOKIE_SECURE", False)
SESSION_COOKIE = "athena_session"
# The readable (non-HttpOnly) cookie carrying the session's CSRF token, so a
# same-origin script/HTMX can echo it in an X-CSRF-Token header. Server-rendered
# forms embed the token directly, so this cookie is a convenience, not required.
CSRF_COOKIE = "athena_csrf"

# Outbound webhook delivery. The app runs a single in-process background loop that
# pushes new events to registered webhooks. It defaults ON, but a deployment that
# runs MULTIPLE worker processes must run exactly one delivery worker (each loop
# would otherwise double-deliver), so disable it (ATHENA_WEBHOOK_DELIVERY=0) in the
# extra workers. Tests disable it too and drive delivery directly. The interval is
# how often the loop wakes; the timeout caps each outbound POST so one slow
# receiver can't stall the loop.
WEBHOOK_DELIVERY_ENABLED = _bool_env("ATHENA_WEBHOOK_DELIVERY", True)
WEBHOOK_DELIVERY_INTERVAL_SECONDS = _float_env(
    "ATHENA_WEBHOOK_INTERVAL", 5.0, minimum=0.0, inclusive=False
)
WEBHOOK_TIMEOUT_SECONDS = _float_env(
    "ATHENA_WEBHOOK_TIMEOUT", 5.0, minimum=0.0, inclusive=False
)

# Automation rules engine. Like the webhook loop, the app runs a single in-process
# background loop that drains new activity events and fires matching rules' actions.
# Defaults ON; disable it (ATHENA_AUTOMATION=0) in extra worker processes (only one may
# run, or rules fire twice) and in tests, which drive automation.run_pass directly.
AUTOMATION_ENABLED = _bool_env("ATHENA_AUTOMATION", True)
AUTOMATION_INTERVAL_SECONDS = _float_env(
    "ATHENA_AUTOMATION_INTERVAL", 5.0, minimum=0.0, inclusive=False
)

# Where uploaded attachment blobs are stored on disk. Keep this OUTSIDE any
# web-served directory (it is, by default — nothing serves it statically); the
# only way to read a file is the authenticated/audited download route, which maps
# an attachment id to its random stored name. ATTACH_MAX_BYTES caps a single
# upload. Note it is also bounded by MAX_REQUEST_BODY_BYTES (the whole-request cap
# the middleware enforces first) — raise that too if you need larger attachments.
ATTACH_DIR = Path(os.environ.get("ATHENA_ATTACH_DIR", "attachments"))
ATTACH_MAX_BYTES = _int_env("ATHENA_ATTACH_MAX_BYTES", 10 * 1024 * 1024, minimum=1)

# OpenID Connect single sign-on. SSO is OFF unless all four connection settings are
# present (see oidc_enabled): the IdP's issuer URL (its
# /.well-known/openid-configuration is discovered from it), the client id/secret
# registered with the IdP, and this app's own callback URL (must exactly match what
# the IdP has registered). Local email+password login is unaffected — SSO is an
# additional way to authenticate.
OIDC_ISSUER = os.environ.get("ATHENA_OIDC_ISSUER", "").strip() or None
OIDC_CLIENT_ID = os.environ.get("ATHENA_OIDC_CLIENT_ID", "").strip() or None
OIDC_CLIENT_SECRET = os.environ.get("ATHENA_OIDC_CLIENT_SECRET", "").strip() or None
OIDC_REDIRECT_URL = os.environ.get("ATHENA_OIDC_REDIRECT_URL", "").strip() or None
_OIDC_CONNECTION_SETTINGS = {
    "ATHENA_OIDC_ISSUER": OIDC_ISSUER,
    "ATHENA_OIDC_CLIENT_ID": OIDC_CLIENT_ID,
    "ATHENA_OIDC_CLIENT_SECRET": OIDC_CLIENT_SECRET,
    "ATHENA_OIDC_REDIRECT_URL": OIDC_REDIRECT_URL,
}
_oidc_configured = {name for name, value in _OIDC_CONNECTION_SETTINGS.items() if value}
if _oidc_configured and len(_oidc_configured) != len(_OIDC_CONNECTION_SETTINGS):
    missing = sorted(set(_OIDC_CONNECTION_SETTINGS) - _oidc_configured)
    raise ValueError(
        "OIDC configuration is partial; set all four connection settings or unset "
        "all of them. Missing: " + ", ".join(missing)
    )

# Optional allow-list of email domains that may auto-provision an account on first
# SSO login (comma-separated, e.g. "acme.com,acme.io"). Empty = any domain the IdP
# asserts. Set it to lock SSO to your organization.
OIDC_ALLOWED_DOMAINS = tuple(
    d.strip().lower()
    for d in os.environ.get("ATHENA_OIDC_ALLOWED_DOMAINS", "").split(",")
    if d.strip()
)


def oidc_enabled() -> bool:
    """SSO is configured only when all four connection settings are present. Until
    then the routes 404 and the login page shows no SSO button — an unconfigured
    deploy behaves exactly as it did before."""
    return all((OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_REDIRECT_URL))
