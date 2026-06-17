"""Application configuration, read from the environment.

Keeping config in one place (and env-driven) means the same code runs on your
laptop and on a server without edits — you just point ATHENA_DB somewhere else.
"""
import os
from pathlib import Path

# The SQLite file Athena stores everything in. Override with the ATHENA_DB env var.
DB_PATH = Path(os.environ.get("ATHENA_DB", "athena.db"))
