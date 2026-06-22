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
# bearer token is presented. The header only CLAIMS an id, so this is safe only
# on a trusted local/tailnet box. Turn it OFF (ATHENA_TRUST_ACTOR_HEADER=0) the
# moment Athena is network-exposed — then only real bearer tokens authenticate.
TRUST_ACTOR_HEADER = _bool_env("ATHENA_TRUST_ACTOR_HEADER", True)
