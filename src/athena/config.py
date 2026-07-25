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

# Athena loggers fail closed on typos instead of silently falling back to INFO.
LOG_LEVEL = os.environ.get("ATHENA_LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in _LOG_LEVELS:
    raise ValueError(
        "ATHENA_LOG_LEVEL must be one of: " + ", ".join(sorted(_LOG_LEVELS))
    )


# Whether to trust the X-Athena-Actor header as a fallback identity when no
# bearer token is presented. The header only CLAIMS an id, so it is safe only on
# a trusted local/tailnet box. It defaults OFF: an unconfigured deploy that gets
# exposed to the network must NOT accept a spoofable identity header. Turn it ON
# (ATHENA_TRUST_ACTOR_HEADER=1) deliberately on a trusted box — typically just
# long enough to mint the first bearer token, then turn it back off.
TRUST_ACTOR_HEADER = _bool_env("ATHENA_TRUST_ACTOR_HEADER", False)

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

# One agent may run several workers (a box per node, a process per capability), but
# not unboundedly many: a looping or compromised token can refresh the rows it has
# forever and still never grow the registry past this ceiling.
WORKER_MAX_PER_AGENT = _int_env("ATHENA_WORKER_MAX_PER_AGENT", 50, minimum=1)

# Per-client-IP limit on ANONYMOUS reads (optional_actor endpoints reached with no
# valid credential). The per-token limiter never runs for these, so without this a
# credential-free caller can hammer public reads unbounded. Defaults to 0 (OFF): a
# local/tailnet box behind auth doesn't need it, and normal anonymous browsing (and
# the test suite) hits these paths freely. Turn it on (e.g. 120) for any deployment
# that exposes anonymous reads to an untrusted network. Keyed by the direct peer IP
# (request.client.host), NOT X-Forwarded-For — so front it with a proxy that either
# terminates anonymous traffic or is accounted for separately.
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
