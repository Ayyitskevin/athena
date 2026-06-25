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
