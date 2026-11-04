#!/usr/bin/env bash
# Local gate profiles; the inventory and receipt logic live in gate.py.
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/gate.py "$@"
