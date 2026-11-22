#!/usr/bin/env bash
# Prepare a fresh checkout for work: full git history, and the .venv built
# exactly as CONTRIBUTING.md ("Development setup") builds it.
#
# For every lane: run it by hand on a new machine, or let a session hook run it
# (.claude/hooks/session-start.sh does, in a Claude Code cloud session). It is
# idempotent: the install is skipped while the files that decide what gets
# installed are unchanged since it last succeeded.
#
# It does not run the gate. Run scripts/gate.sh afterwards.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

say() { printf 'bootstrap: %s\n' "$*"; }

# --- history -----------------------------------------------------------------
# A shallow clone has no merge base with main, so `scripts/gate.sh --profile
# changed` would fall back to the full suite, and blame and log stop short. A
# failed fetch is reported, not fatal: the checkout still works offline.
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null || echo false)" = "true" ]; then
  say "shallow clone: fetching full history"
  git fetch --quiet --unshallow origin \
    || say "WARNING: could not fetch full history; --profile changed will run the full suite"
fi
if ! git rev-parse --verify --quiet origin/main >/dev/null; then
  say "fetching origin/main (the default base of --profile changed)"
  git fetch --quiet origin main:refs/remotes/origin/main \
    || say "WARNING: could not fetch origin/main; pass --base to --profile changed"
fi

# --- the virtual environment ---------------------------------------------------
# Athena supports Python 3.12 only (pyproject.toml: >=3.12,<3.13).
if ! command -v python3.12 >/dev/null 2>&1; then
  say "ERROR: python3.12 not found on PATH. Athena needs Python 3.12; install it first."
  exit 1
fi

# The hash file lives inside .venv (ignored by git), so it disappears with the
# packages it describes. It lists this script and every file that decides what
# gets installed.
hashes=.venv/.bootstrap.sha256
inputs=(scripts/bootstrap.sh pyproject.toml constraints/bootstrap-py312.txt constraints/ci-py312.txt)

if [ -x .venv/bin/python ] && sha256sum --check --status "$hashes" 2>/dev/null; then
  say ".venv is current; nothing to install"
else
  # Delete the old hashes first, so a failed install runs again next time.
  rm -f "$hashes"
  say "building .venv as CONTRIBUTING.md does"
  python3.12 -m venv .venv
  .venv/bin/python -I -m pip install --quiet \
    --only-binary :all: \
    --require-hashes -r constraints/bootstrap-py312.txt
  .venv/bin/python -I -m pip install --quiet \
    -c constraints/ci-py312.txt -e ".[dev,mcp]"
  .venv/bin/python -I -m pip check
  .venv/bin/python -I -m pip freeze --exclude-editable \
    | diff -u constraints/ci-py312.txt -
  sha256sum "${inputs[@]}" > "$hashes"
  say ".venv built and verified against constraints/ci-py312.txt"
fi

say "ready. Inner loop: scripts/gate.sh --profile changed. Done: scripts/gate.sh"
