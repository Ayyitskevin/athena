- **The gate has an inner loop, and its floors ratchet.**
  `scripts/gate.sh --profile changed` runs the static stages, the type check
  and the tests a diff can affect, with a receipt marked PARTIAL; `--json`
  writes any receipt as JSON. The coverage floors rise to 93.86% line, 85.31%
  branch and 91.90% combined, and the check now fails when a measurement is
  more than half a point above its floor. `commit` is a required keyword on
  `activity.record` and the other writers. `scripts/bootstrap.sh` prepares a
  fresh checkout, and a committed `.claude/` hook runs it in a Claude Code
  cloud session.
