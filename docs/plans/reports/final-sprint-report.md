# Run Report — The Final Sprint

Guide: [`docs/OPUS_FINAL_SPRINT_ATHENA.md`](../../OPUS_FINAL_SPRINT_ATHENA.md)
(written against `eafd039`). Branch: `claude/athena-buzz-integration-4hkyx8`.
Starting SHA: `e8200d1` (origin/main at session start — the guide's own merge
commit, so the tree is one commit ahead of what the guide was written against;
no drift affecting any stage was found beyond the corrections recorded below).

This report is the handoff artifact. Every stage appends its claims table,
design, deviations, and validation evidence before the next stage begins.

---

## Stage F-1 — The Desk

### Phase 0 — claims verified against the tree

| Guide claim | Verdict | Evidence |
|---|---|---|
| Next migration number is 0073 | CONFIRMED | `src/athena/core/migrations/` ends at `0072_activity_chain.sql` |
| `whoami` shape exists in MCP | CONFIRMED | `mcp/server.py:479` — returns identity + scopes + budget + `approval_required` + run. The desk reuses the same underlying reads rather than re-deriving them |
| `delegations.list_delegations(conn, subject, *, viewer, include_closed, limit, offset)` | CONFIRMED | `aegis/delegations.py:52`; returns a dict (not a bare list) — desk reads its items/total |
| `budgets.observed(conn, user_id)` | CONFIRMED | `core/budgets.py:149`; rolled forward at read time, `None` = unbudgeted |
| `approvals.gated_kinds(conn, user_id)` | CONFIRMED | `core/approvals.py:464` |
| `notifications.unread_count` + `list_notifications` | CONFIRMED | `core/notifications.py:240` / `:211`, both visibility-gated by `actor` |
| `run_control_commands.readable_controls(conn, *, actor, run_id, state, limit, now)` | CONFIRMED | `core/run_control_commands.py:739`; a non-admin gets its own inbox — exactly the desk's need, with `state="open"` |
| `workers.list_workers(conn, *, agent_id, limit, stale_seconds, now)` | CONFIRMED | `core/workers.py:212`; derived `kill_state` per row |
| "leases I hold" reader exists | **CORRECTED** | `aegis/leases.py` has only `get_lease(issue_id)` — there is NO holder-scoped reader. `fleet_work.build_active_work` has a holder-filtered query but it is an admin fleet projection, not a self-read. Deviation D-1 below |
| `claim_handoffs` has a "my pending acknowledgments" reader | **CORRECTED** | `aegis/claim_handoffs.py:146` offers `open_handoffs_for_issues(issue_ids)` only. The desk composes: my leases → their issue ids → open handoffs. No new reader needed |
| `GET /events` exposes `after`/`next_after`/`has_more` | CONFIRMED | `core/events_api.py:50-57` |
| `watches` table is polymorphic with no `target_kind` CHECK | CONFIRMED | migration 0023: PK `(user_id, target_kind, target_id)`, no CHECK — F-3 needs no migration |
| Personal state is a documented category the cursor can use | CONFIRMED, with a limit | `COMMAND_MIGRATION.md:41` — owner-scoped mutation SQL, **no audit events**, exactly one data-module writer each; and explicitly *"an exception, not a lane"*. `check_write_ownership.py:62-68` allowlists `saved_filters.*` and `notifications.mark_read/watch/unwatch` as transport-callable |

### Phase 1 — design

**Deviation D-1 (new reader, table-owning module).** `leases.py` gains
`leases_held_by(conn, *, holder_id, limit, now)` — the holder-scoped read the
desk needs, living in the module that owns `issue_leases` (one table, one
owner). It derives `active`/`expired` from `expires_at` vs an injectable clock
rather than storing state, matching the lease doctrine, and carries the 0057
`generation` so a desk reader can fence a delayed command.

**Decision D-2 (cursor gets a command module, and no activity event).** The
cursor write qualifies as personal state on every criterion in
`COMMAND_MIGRATION.md:41`, and that is the citation for recording **no
activity event**: a read receipt is the reader's own state, not fleet history.
But the category is documented as an exception rather than a lane, and this
write has a genuine refusal to map (`conflict` on rewind), so it still gets a
command owner (`core/desk_commands.py`) with the transport-neutral kind
dialect. Strictly more conservative than the exception allows; needs no
`check_write_ownership` allowlist entry because the path is
transport → command → data module, the ordinary shape.

**Decision D-3 (advance-only, enforced twice).** The command refuses a smaller
`after_id` with `conflict` for a clean 409, and migration 0073 carries a
BEFORE UPDATE trigger refusing the same at the database. Rewinding a read
receipt would let an agent claim it never saw an event; the trigger is the
backstop against a writer that bypasses the command.

**Decision D-4 (`events_since_cursor` is capped and says so).** Counting is
bounded at 500 (`COUNT(*)` over a `LIMIT 501` subquery) and the projection
carries `events_since_cursor_capped: true` when it bites. An exact five-figure
backlog is noise; "500+, drain from here" is the actionable form. With no
cursor set the count is `null`, never `0` — "you have never read" and "nothing
new" are different facts.

**Decision D-5 (the desk is for every authenticated actor).** Humans get a
desk too; agents are the audience. Visibility is the actor's own throughout —
every lane composes an existing gated reader with `actor=` rather than a new
query, so the desk cannot widen what any surface already shows.

**Schema `athena.agent_desk.v1`** — `identity` · `asks` · `work` · `signals` ·
`cursor`, each list carrying its own `limit`/`total`.

**Bounds:** controls 20, delegations 20, leases 20, handoffs 20,
notifications 10, `events_since_cursor` cap 500, `after_id` bounded by
`MAX_SQLITE_INTEGER` (`core/ids.py`).

**Error kinds:** `invalid` (non-integer/non-positive `after_id`), `conflict`
(rewind). No `not_found` — a desk always exists for an authenticated actor.

### Phase 2..n — implementation and validation

**Deviation D-6 (module placement — the guide said `core/`, the tree said no).**
The guide specifies `core/desk.py` and `core/desk_commands.py`. That is
impossible: the desk composes `aegis` readers (delegations, leases, claim
handoffs) and `scripts/check_import_contracts.py` fails the build on
`core → aegis`. Caught by the gate, not by review. Resolved along the house's
own seam, with the precedent being every other cross-module projection
(`aegis/fleet_work.py`, `aegis/fleet_attention.py`, `aegis/work_context.py`,
all in aegis and importing core freely):

- `core/desk_cursors.py` — the `agent_cursors` table's only writer/reader.
  Core owns it because the position is about the **activity trail**, which is
  core's, and it imports no aegis.
- `aegis/desk.py` — the composition.
- `aegis/desk_commands.py`, `aegis/desk_api.py` — command owner and transport.

**Deviation D-7 (two activity readers registered with the guard script).**
`_events_since` and `_latest_event_id` are general readers: the desk's count
must equal what `GET /events` would hand the same reader, and that feed serves
imported rows (labeled). Excluding foreign history would make the count
disagree with the drain it exists to size. Both are recorded in
`scripts/check_imported_at_guards.py`'s exempt list with that reason.

**Three guide claims the tree corrected during implementation** (each found by
a failing test, each a case of the code being right):

1. `claim_handoffs.open_handoffs_for_issues` returns a **dict keyed by issue
   id**, not a list. The desk sorts it into a list.
2. Delegation inbox items **nest the issue** under `item["issue"]`, and the
   inbox pages by `has_more`/`next_offset` rather than reporting a total — the
   desk repeats that honesty instead of inventing a count.
3. Claiming an issue **requires `If-Match`** with the issue's strong ETag
   (`428 precondition_required` without it). The test now carries it; this is
   the optimistic-concurrency contract working as designed.

**One design fact discovered by test, worth stating:** a brand-new agent can
see *none* of the bootstrap/onboarding trail, so its
`latest_visible_event_id` is legitimately `null`. That is the honest answer —
there is nothing for it to acknowledge — and it reinforces why the unset
cursor is `null` rather than `0`.

### Validation (Stage F-1)

| Check | Result |
|---|---|
| `ruff check .` | passed |
| `ruff format --check .` | passed (405 files) |
| `mypy src/athena` | no issues, 165 source files |
| `scripts/check_import_contracts.py` | passed, 165 modules (after D-6) |
| `scripts/check_write_ownership.py` | passed |
| `scripts/check_imported_at_guards.py` | passed (after D-7) |
| `pytest tests/test_desk.py` | **18 passed** |
| Real-HTTP proof | `athena-serve` bootstrap → login → mint bearer → `GET /desk` (null cursor, null since-count) → advance to `latest_visible_event_id` → since-count 0 → idempotent re-ack 200 → rewind refused 409 → `athena-doctor` reports 73 migrations, latest `0073_agent_cursors.sql`, chain verified |

Limitations recorded: the desk has no web surface by design (agent-facing; the
operator's dashboard already exists); `asks.worker_kill_requests` scans the
caller's newest 50 workers rather than paging, which is a bound worth revisiting
only if a single agent ever registers more than 50 processes.
