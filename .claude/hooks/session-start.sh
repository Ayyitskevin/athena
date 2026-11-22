#!/bin/bash
# SessionStart hook for Claude Code cloud sessions: prepare the checkout with
# the same bootstrap every lane uses (scripts/bootstrap.sh). Synchronous, so
# the gate works from the first turn. On a contributor's own machine it does
# nothing: run scripts/bootstrap.sh by hand there.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"
exec scripts/bootstrap.sh
