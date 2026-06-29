"""Application configuration, read from the environment.

Keeping config in one place (and env-driven) means the same code runs on your
laptop and on a server without edits — you just point ATHENA_DB somewhere else.
"""
import os
from pathlib import Path

# The SQLite file Athena stores everything in. Override with the ATHENA_DB env var.
DB_PATH = Path(os.environ.get("ATHENA_DB", "athena.db"))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


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
MAX_REQUEST_BODY_BYTES = int(
    os.environ.get("ATHENA_MAX_REQUEST_BODY_BYTES", str(1024 * 1024))
)

# Browser session lifetime, and whether the session cookie carries the Secure
# flag (HTTPS-only). Secure defaults OFF so login works over plain http in local
# dev — turn it ON (ATHENA_COOKIE_SECURE=1) whenever Athena is served over HTTPS.
SESSION_TTL_DAYS = int(os.environ.get("ATHENA_SESSION_TTL_DAYS", "14"))
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
WEBHOOK_DELIVERY_INTERVAL_SECONDS = float(
    os.environ.get("ATHENA_WEBHOOK_INTERVAL", "5")
)
WEBHOOK_TIMEOUT_SECONDS = float(os.environ.get("ATHENA_WEBHOOK_TIMEOUT", "5"))

# Automation rules engine. Like the webhook loop, the app runs a single in-process
# background loop that drains new activity events and fires matching rules' actions.
# Defaults ON; disable it (ATHENA_AUTOMATION=0) in extra worker processes (only one may
# run, or rules fire twice) and in tests, which drive automation.run_pass directly.
AUTOMATION_ENABLED = _bool_env("ATHENA_AUTOMATION", True)
AUTOMATION_INTERVAL_SECONDS = float(os.environ.get("ATHENA_AUTOMATION_INTERVAL", "5"))

# Where uploaded attachment blobs are stored on disk. Keep this OUTSIDE any
# web-served directory (it is, by default — nothing serves it statically); the
# only way to read a file is the authenticated/audited download route, which maps
# an attachment id to its random stored name. ATTACH_MAX_BYTES caps a single
# upload. Note it is also bounded by MAX_REQUEST_BODY_BYTES (the whole-request cap
# the middleware enforces first) — raise that too if you need larger attachments.
ATTACH_DIR = Path(os.environ.get("ATHENA_ATTACH_DIR", "attachments"))
ATTACH_MAX_BYTES = int(
    os.environ.get("ATHENA_ATTACH_MAX_BYTES", str(10 * 1024 * 1024))
)

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
    return all(
        (OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_REDIRECT_URL)
    )
