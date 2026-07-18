# CLAUDE.md — pointer, not a second contract

The contributor contract for this repo is [AGENTS.md](AGENTS.md). Read that;
it is the source of truth for how we build here. Until 2026-07-11 this path
was a symlink to it — replaced with a real file so Claude-specific notes have
somewhere to live without duplicating the contract (one fact, one place).

Claude-Code-specific notes:
- Your machine-level handbook (`~/.claude/CLAUDE.md`) loads automatically and
  carries the fleet-wide rules + the canonical Permission Boundaries block;
  AGENTS.md narrows them for Athena and wins on Athena-specific conflicts.
- Branch as `claude/<topic>`. You may merge your own PR here once green (the
  dev-project carve-out documented in AGENTS.md) — but never push `main`
  directly, and the carve-out never extends to live production services.
