# Undo by compensation

`VISION.md` promises the operator can **undo** what an agent did.
`ARCHITECTURE.md` promises the activity trail is **append-only**. Those are only
compatible one way.

**Undo never deletes or edits a row.** Undoing event *N* runs the inverse as a
new, fully audited forward command, and the event that command records carries
`reverses_event_id = N` (migration 0064). History gains two rows — the action and
its reversal — which is the truthful record of what happened.

## What follows from that

- **Authorization is re-evaluated, never inherited.** The compensator runs as the
  *undoing* actor through the ordinary command owner, so the same role, scope, and
  visibility gates apply as if they had made the change by hand. Undo is not a
  back door into someone else's write.
- **Replay stays exact.** A reversal is an ordinary event of the run that
  performed it, so `run_replay` and lineage show the action *and* its later
  reversal rather than a rewritten past.
- **An undo that changes nothing is a refusal.** Every compensator is idempotent
  at the domain layer (re-archiving an archived issue records no event), so
  "nothing was recorded" is exactly the signal that the effect is no longer in
  force — someone already reversed it by hand, or the world moved on. Reporting
  success there would be a lie.

## Reversibility classes

| Class | Meaning | Undo offered? |
|---|---|---|
| `two_way` | A direct inverse command exists | Yes |
| `one_way` | A real side effect that cannot be silently retracted | No — with a reason |
| `trapdoor` | No undo exists at any price | No — with a reason |
| `unclassified` | Nobody has taught undo about this verb yet | No — and it says so |

`unclassified` is deliberately distinct from `trapdoor`: "we have not taught undo
about this" is honest, and claiming irreversibility we have not established would
not be.

## What is reversible today

| Verb | Inverse |
|---|---|
| `archived` / `unarchived` (issue) | restore / archive the issue |
| `labeled` / `unlabeled` (issue) | detach / attach that label |
| `page_archived` / `page_unarchived` | restore / archive the page |
| `page_labeled` / `page_unlabeled` | detach / attach that label |
| `changed_status` (issue) | put the issue back in its previous status |
| `assigned` / `unassigned` (issue) | put the issue back in its previous hands |

The first four pairs share one property that made them safe to ship first: **the
inverse needs no prior state.** A toggle's inverse is the toggle; a label event
stores the label's *name*, and names are unique (0007, `COLLATE NOCASE`), so
resolving one back to an id is a lookup rather than an interpretation of prose.

`changed_status` is the first verb whose inverse *does* need prior state, and it
needed no new storage to get it. **This document previously said otherwise** —
that reversing status "needs structured prior state recorded at write time — a
real change". That was wrong: migration 0055 has recorded `before_status`,
`before_category`, and `before_project_scope_key` into `issue_lifecycle_facts`
since well before undo existed, keyed 1:1 to the activity event, immutable, and
written in the same transaction. The compensator reads that row. It never parses
`activity.detail`, which says the same thing as prose — driving a mutation from a
human-readable string would be inference dressed up as a feature.

Prior state alone is not enough, though, and the difference is worth stating
because it generalizes to every scalar field. Archive and label inverses are
protected by **domain idempotency**: re-archiving an archived issue records
nothing, and "recorded nothing" is how the engine detects that the effect it was
asked to reverse is no longer in force. Writing a status is never a no-op, so
that net does not catch anything. Undoing a stale status event would cheerfully
overwrite a *newer* value and stamp the result as a reversal — the trail would
assert it reversed `open → in_progress` while actually discarding `done`. So the
compensator gates it itself:

- **Still in force.** The issue's current status must be the one this event
  produced. Otherwise: `undo_no_effect` (409), and nothing is written.
- **Same access envelope.** Statuses are per-project (0024) and a project move
  *remaps* an issue's status, so a `before_status` captured under one project is
  not meaningful under another. A move since the event refuses.
- **A fact must exist.** 0055 is deliberately not backfilled, so an event older
  than it has no fact. That is `undo_not_reversible` (422), never a guess.

`assigned` / `unassigned` needed the storage that status already had: the event
records only the *new* assignee's display name (names are not unique, and a
re-assign reads identically to a first assign), so there was genuinely nothing
to restore from. Migration 0068 (`issue_assignee_facts`) records the typed
before/after ids in the same transaction as the event — the assignee twin of
0055 — and the compensator applies the same scalar discipline as status: a
still-in-force gate (the issue's current assignee must be the one this event
produced), refusal for a pre-0068 event rather than a guess parsed from prose,
and the ordinary command as the undoing actor. No scope gate: statuses are
per-project and remapped by a move; an assignee is not.

Classified and refused today: comments and attachments (`one_way` — delete them
explicitly), destroyed rows such as `page_deleted` / `space_deleted` /
`deleted_project` (`trapdoor`), `page_edited` (`one_way` — page history
already keeps the prior version, so restore it explicitly), and
`overrode_blocked_issue_close` (`one_way` — a policy override is a decision on
the record, not a state to flip).

## Guarantees

- **Atomic.** The compensating command and the reversal link commit together; a
  refusal rolls back whole and leaves no half-undo.
- **Single-use, enforced by the database.** A partial unique index on
  `reverses_event_id` means two concurrent undos of the same event cannot both
  compensate. The read-then-write check in the engine races; the constraint does
  not.
- **Imported history is never undoable.** An imported row (0041) describes
  something that happened in another system; compensating it here would invent a
  local write nobody made.
- **Hidden events are unreachable.** Undoing by id uses the same visibility
  predicate the feeds use, and missing and hidden collapse to one 404 — an event
  id must not become an oracle for private work.
- **Undoing an undo is a redo.** A reversal is an ordinary forward event, so it
  can itself be reversed. Each step is a new row.
- **Every refusal has a stable code.** Branch on `code`, never on prose.

## Refusal contract

| Code | Status | Means |
|---|---|---|
| `undo_event_not_found` | 404 | No such event, or not visible to you |
| `undo_not_reversible` | 422 | The verb is one-way, a trapdoor, or unclassified |
| `undo_already_reversed` | 409 | Another event already reversed this one |
| `undo_imported_event` | 422 | Foreign history from a portability import |
| `undo_no_effect` | 409 | Nothing left to reverse; the effect is not in force |
| `undo_refused_by_command` | (the command's own) | The inverse itself was refused — role, scope, visibility, a budget, an approval gate |

That last one matters: undo is an **ordinary write** and obeys every rule ordinary
writes obey. An undo can be refused by a budget or wait on an approval gate
exactly as the original action could.

## Surfaces

```text
POST /activity/{event_id}/undo   → 201 {"reversal": {...}, "reversed_event_id": N}
GET  /activity                    # each event carries "reverses_event_id"
```

MCP: `undo_action(event_id)`. The browser cockpit shows an **Undo** control on
reversible rows of the activity feed, marks already-undone rows, and annotates a
reversal with the event it undid.

## Limitations

- Eleven verbs are reversible (the four archive/label pairs, `changed_status`,
  and `assigned`/`unassigned`); everything else is refused, with a reason.
- There is no bulk undo, and no "undo this whole run" — each event is its own
  decision.
- A compensator reverses the *effect*, not the world around it. Undoing a label
  removal re-attaches the label; it cannot un-send the notification the removal
  produced.
- Undo is not versioned rollback. For page content, page history and
  `restore_version` are the honest mechanism, which is why `page_edited` is
  classified one-way rather than wired to a content-diff compensator.
- A registered inverse is *possible*, not *permitted*: the affordance a surface
  renders is a hint, and the engine remains the only authority.
