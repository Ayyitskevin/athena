# The seat-boot block — putting the desk where agents actually look

Athena ships a complete agent surface: the desk, work-context packets, leases,
runs, scoped MCP tools. It also has a measured adoption problem: agents boot
from their harness rules file (`CLAUDE.md`, `AGENTS.md`, `KIMI.md`, ...), and
if the desk is not named *there*, they fall through to yesterday's paths — raw
SQL against `athena.db` included. Forty documents in `docs/` cannot compete
with one paragraph in the file every session is guaranteed to read.

This page owns that paragraph. It is the **canonical copy** of the boot block a
seat's rules file should carry. Adopt it byte-identically; when it changes,
change it here first and re-propagate, the same way fleet doctrine files are
kept in sync.

## Before you adopt it: wire the seat

The block instructs an agent to call the desk. If the seat cannot reach it,
the block sends every session into a wall — **wire first, adopt second.**

1. Mint or locate the seat's token (scopes: `read` + `issue:write`, add
   `docs:write` if it edits Mentor pages) — see
   [QUICKSTART.md](QUICKSTART.md) path B and [OPERATIONS.md](OPERATIONS.md).
2. Configure the runtime per [RUNTIME_RECIPE.md](RUNTIME_RECIPE.md) — for MCP
   harnesses, the `athena-mcp` server with `ATHENA_BASE_URL` and
   `ATHENA_TOKEN`; for plain-HTTP agents, the same two values in the
   environment.
3. **Prove the wiring** (this is the seat's smoke test; re-run it after any
   token rotation or deploy move):

```bash
# The wiring proof — server, token, scopes, desk, in one command:
athena-seat-doctor --expect-scopes read,issue:write
# (reads $ATHENA_BASE_URL and $ATHENA_TOKEN; exit 0 = wired, exit 1 says
#  exactly which link is broken)

# Raw fallback where the entrypoint is not installed:
curl -sf -H "Authorization: Bearer $ATHENA_TOKEN" "$ATHENA_BASE_URL/desk" | head -c 400

# MCP path — ask the harness to call my_desk() and report the identity lane.
```

If either fails, stop: fix the wiring, do not paste the block yet.

## The canonical boot block

Copy everything inside the fence into the seat's rules file (the
machine-specific part, not shared doctrine). Two deliberate shape choices:
the block opens and closes with **HTML-comment sentinels** so a drift checker
can extract and byte-diff it the way fleet doctrine is diffed, and its title
is **bold text, not a `##`/`###` heading** — fleet drift checkers census
section headings across nodes, and a host-local block must not change a
file's section census.

```markdown
<!-- ATHENA-BOOT-BLOCK v1 · source: docs/AGENT_BOOT.md in the athena repo · keep byte-identical across seat files -->
**Athena — the board** (this host's seats; wire first — see docs/AGENT_BOOT.md)
- If the task touches issues, missions, or operator docs: call `my_desk()`
  (MCP) or `GET /desk` FIRST — one bounded read: who you are, what is asked
  of you, what you hold, what changed. Never reconstruct it from separate
  reads or raw SQL.
- Claim before editing: check `get_issue_lease(ref)`, then `claim_issue(...)`.
  One issue per owned work slice, claimed before files change.
- Read one issue properly with `get_issue_work_context(ref)` — the bounded
  packet (issue, neighbours, blockers, runbook) — not five separate reads.
- Tag your writes: `begin_run(run_id=...)` at the start, heartbeat while
  working. Untagged writes are attributable but not replayable as a unit.
- Put it down: close with the verify commands a reviewer can re-run, and
  advance the desk cursor to the last event you actually handled. Complete
  or yield every claim before the session ends — expired leases linger on
  the desk.
- Escape hatch: reading `athena.db` directly is last-resort triage — say so
  out loud when you do it. Writing to it directly is NEVER allowed: it
  bypasses scopes, the activity trail, undo, and the hash chain.
<!-- END ATHENA-BOOT-BLOCK -->
```

## Why the sqlite line is absolute

Every Athena write path — commands, REST, MCP — funnels through domain
authorization, validation, the append-only activity event, and (since
migration 0072) the trail hash chain. A direct `sqlite3` write skips all four:
it is unattributed, unauditable, un-undoable, and breaks the chain the
operator's trust is built on. There is no emergency that makes it correct;
the emergency path is the operator and [OPERATIONS.md](OPERATIONS.md).

Direct *reads* are tolerated as triage of last resort (a seat with no working
token still needs eyes), but they see raw rows with no visibility rules
applied — treat what they show as unconfirmed, and say in your output that the
desk was bypassed and why.

## Keeping it from drifting

- The block names six verbs. If a rename or removal touches one
  (`my_desk`, `get_issue_lease`, `claim_issue`, `get_issue_work_context`,
  `begin_run`, the desk cursor), update this file in the same PR — the
  Definition of Done's "you ran it" applies to this document too.
- Seats adopt by copy, so drift is possible by construction. The sentinels
  exist for exactly that: a fleet that byte-diffs its shared doctrine (as
  this one does) extracts `<!-- ATHENA-BOOT-BLOCK` … `END ATHENA-BOOT-BLOCK -->`
  from each seat file and diffs the copies against each other and against
  this fence.
