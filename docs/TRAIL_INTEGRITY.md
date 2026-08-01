# Trail integrity: the hash chain over the activity log

`VISION.md`'s fifth loop phase is **Trust / Learn**: "the append-only activity
log + run replay/lineage prove exactly what happened." Until migration 0072
that proof rested on convention — `activity.record()` only inserts, and a
handful of rows referenced by other tables are DB-frozen (0058) — but nothing
let an operator *verify* that the trail they are reading is the trail that was
written. After a restore, on a copied database file, or just on a bad day,
"the log says so" was still an honor-system claim.

The chain closes that gap: every activity row recorded after adoption gets a
sidecar `activity_chain` entry whose `entry_hash` is the SHA-256 of the row's
stored facts plus the previous entry's hash. Recomputing the chain re-derives
history. An edited row, a vanished row, or an out-of-band insert breaks every
hash after it, and verification points at the first broken link.

Adapted from Buzz's "every message, reaction, workflow step, review approval,
and git event is a signed event in one log" — deliberately minus the
signatures. Buzz members hold their own keys, so a signature there attests
*who*. Athena is a single-operator tool whose server holds every credential; a
server-side signature would attest nothing a hash does not. What transfers is
**verifiability**: a bounded, deterministic recomputation any reader can run,
with zero new dependencies and one SHA-256 per write.

## What is chained, and when

- Every row `activity.record()` writes — native events, security refusals,
  automation firings — and every row the portability import replays. Imported
  rows are chained *as imported rows*: `imported_at` is part of the hashed
  facts, so the chain also attests that foreign history was labeled foreign.
- The chain entry lands in the same transaction as its activity row. There is
  no async signer, no queue, and no window where an event exists unchained.
- **The chain starts at adoption.** Migrations are pure SQL and SQLite cannot
  compute SHA-256, so rows recorded before 0072 are *below the anchor* (the
  first chained row). They stay readable everywhere; they are counted as
  `unchained_count` and never folded into a verified claim. A fresh database
  is covered from its first row.

## The recipe (schema_version 1)

`entry_hash = SHA-256(canonical JSON of {v, prev, id, actor_id, verb,
target_kind, target_id, detail, created_at, run_id, parent_run_id,
forked_from_event_id, visibility_restricted, reverses_event_id, imported_at})`
— sorted keys, compact separators, ASCII escapes. Only stored columns enter
the hash; joined display names are mutable presentation and never do. The
first entry's `prev` is sixty-four zeros. Changing the recipe is a migration:
the `schema_version` CHECK pins the only recipe the schema knows how to
verify.

The database enforces what SQL can usefully express (0072): chain entries are
immutable and undeletable; a new entry must extend the head — in id order,
naming the head's `entry_hash` as its `prev_hash` (genesis only while the
chain is empty) — so raw SQL cannot append a plausible side branch; and a
chained activity row cannot be deleted (its chain entry's foreign key holds
it, and the entry itself is undeletable). In-place *edits* of activity rows
are deliberately detected rather than trigger-blocked: prevention at the SQL
layer would be theater against the actual threat — a writer holding the file
bypasses triggers entirely — and detection is the claim verification makes
good on.

## Verifying

```text
GET /activity/chain                    → anchor, head, coverage counts (admin)
GET /activity/chain/verify?limit=1000  → recompute one bounded window (admin)
GET /activity/chain/verify?after_id=N  → resume where the last window stopped
```

MCP: `activity_chain_status`, `verify_activity_chain` — the same two reads.

Every verify call does at most `limit` (≤ 5000) rows of work and returns
`next_after` while `has_more` is true, so verification can never hold the
database for an unbounded walk. On a break it reports the **first**
mismatching event and the reason (`entry_hash_mismatch`,
`prev_hash_mismatch`, `missing_chain_row`) — everything after a broken link
inherits the break, so one exact spot beats a thousand echoes.

`athena-doctor <db>` runs the full walk on every check-up and fails loudly on
a break — the verification an operator runs after a restore or before
trusting a copied database file. `/admin/security` shows where the chain
stands and re-verifies the newest 200 entries per render, labeled as exactly
that: a tail check, not the full claim.

## What this does not claim

- **A verified chain is not a signed chain.** A writer who can rewrite *both*
  tables — someone with the SQLite file and this document — can rebuild the
  whole chain to match a forged trail. The chain makes tampering evident
  against everything short of that deliberate rebuild.
- **Tip truncation is self-consistent.** Deleting the newest rows of both
  tables together leaves a shorter chain that still verifies. Both residual
  risks have the same mitigation, and it is the reason the head hash is
  surfaced everywhere: **note the head outside Athena** (in your backup log,
  a notebook, anywhere the database cannot reach) and a rebuild or truncation
  becomes detectable against a value Athena could not have rewritten.
- **The chain attests the trail, not the world.** It proves the recorded
  history has not been rewritten; it proves nothing about what an agent's
  process actually did off the record. Epistemics are unchanged: completion
  events are still claims (`RUN_CONTROLS.md`), heartbeats still prove
  reporting, not health (`WORKERS.md`).
- **Rows below the anchor are not claimed.** Verification says "before the
  chain" about them, and `unchained_count` keeps that boundary visible rather
  than letting old history borrow the chain's authority.
- A broken chain is a **finding, not a repair target**. There is no
  "re-chain" command; the answer to a break is an investigation and, if
  needed, a restore from a backup whose head hash you noted.
