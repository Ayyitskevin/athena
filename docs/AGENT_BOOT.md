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
  working. Heartbeat records an authenticated report, not process liveness
  or lease renewal. Renew before expiry with the generation and a freshly
  reviewed issue ETag. Untagged writes are not replayable as a unit.
- Put it down: close with the verify commands a reviewer can re-run, and
  advance the desk cursor to the last event you actually handled. Complete
  or yield every claim before the session ends — one you let lapse lists
  under `work.leases.lapsed` until you `complete_claim` it with its
  generation.
- Acknowledge only notifications you actually handled, by their notification
  ID with `mark_notification_read(id)`; never bulk `mark_notifications_read`.
  A desk preview, cursor advance, or claim completion is not acknowledgment.
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

## Carry fewer tools: `ATHENA_MCP_PROFILE`

An MCP session registers only the tools its token can use, but for a normal
seat that is still about a hundred tools of descriptions and schemas read
before the first call. `ATHENA_MCP_PROFILE` in the MCP server's environment
names a smaller set:

| Value | Tools | For |
|---|---|---|
| `full` (the default; unset or empty means the same) | everything the token can use | operators, embedding callers, anything that is not a seat |
| `seat` | the core loop: `my_desk`, `advance_desk_cursor`, `list_my_delegated_work`, `search_work`, `get_issue`, `get_issue_work_context`, `list_issue_comments`, `get_issue_lease`, `claim_issue`, `yield_claim`, `resume_claim_handoff`, `complete_claim`, `decline_delegation`, `update_issue`, `comment_on_issue`, `create_issue`, `heartbeat_agent_run`, `current_run`, `list_notifications`, `mark_notification_read`, `list_run_controls`, `acknowledge_run_control`, `complete_run_control`, `decline_run_control` | a seat working issues |
| `seat+knowledge` | the above plus `search`, `get_page`, `list_pages`, `create_page`, `update_page`, `record_run_learning`, `get_issue_runbook`, `list_projects`, `attach_label`, `list_labels`, `link_issues` | a seat that also reads and writes pages |

A profile intersects the scope filter: it can hide a tool the token could use,
never show one it could not, and the REST layer stays the boundary either way.
No seat profile carries `mark_notifications_read`: bulk acknowledgement is never
a seat's move. An unknown value stops the server with a message naming the
three; it never falls back to `full` by accident. A seat that needs a hidden
tool for one task starts that session with `ATHENA_MCP_PROFILE=full`. The seat
profile also names `claim_next_ready` and `wait_for_events`, which bind as soon
as those tools are registered.

## Read less per issue: `brief=true`

The full work-context packet carries the protocol, how-to-claim and runbook
blocks on every call. Once you have read them, re-read an issue with
`get_issue_work_context(issue_id=..., brief=true)` (REST:
`GET /issues/{ref}/work-context?brief=true`). The answer keeps `issue`,
`issue_etag`, `dependencies` and `warnings`, the same values as the full packet
from the same snapshot, and nothing else; `issue_etag` is still the `if_match`
for `claim_issue`. The default packet is unchanged.

## Keeping it from drifting

- If a rename or removal touches a named operation
  (`my_desk`, `get_issue_lease`, `claim_issue`, `get_issue_work_context`,
  `begin_run`, the desk cursor, `mark_notification_read`), update this file in the same PR — the
  Definition of Done's "you ran it" applies to this document too.
- Seats adopt by copy, so drift is possible by construction. The sentinels
  exist for exactly that: a fleet that byte-diffs its shared doctrine (as
  this one does) extracts `<!-- ATHENA-BOOT-BLOCK` … `END ATHENA-BOOT-BLOCK -->`
  from each seat file and diffs the copies against each other and against
  this fence.
