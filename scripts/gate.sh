#!/usr/bin/env bash
# The Definition-of-Done gate as one verb.
#
# Runs exactly the commands AGENTS.md lists, in order, stopping loudly at the
# first failure. This script exists so "run the gate" cannot be paraphrased
# into a shorter list — if the gate changes, change AGENTS.md and this file in
# the same commit (CI runs the same set; a third copy would drift).
#
# Usage: scripts/gate.sh          (from the repo root, inside the dev venv)
set -u

cd "$(dirname "$0")/.." || exit 1

steps=(
  "ruff check ."
  "ruff format --check ."
  "python scripts/check_import_contracts.py"
  "python scripts/check_write_ownership.py"
  "python scripts/check_imported_at_guards.py"
  "python scripts/check_template_styles.py"
  "python scripts/check_template_routes.py"
  "pytest -q -n 4"
)

for step in "${steps[@]}"; do
  echo "==> ${step}"
  if ! ${step}; then
    echo "GATE FAILED at: ${step}" >&2
    exit 1
  fi
done

echo "GATE PASS (${#steps[@]}/${#steps[@]} steps green)"
