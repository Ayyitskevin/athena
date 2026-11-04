#!/usr/bin/env bash
# Local gate profiles; the inventory and receipt logic live in gate.py.
set -eu
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "GATE FAILED: .venv/bin/python is unavailable; follow Development setup in CONTRIBUTING.md." >&2
  exit 1
fi
exec .venv/bin/python scripts/gate.py "$@"
