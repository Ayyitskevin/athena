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

These four pairs share one property that made them safe to ship first: **the
inverse needs no prior state.** A toggle's inverse is the toggle; a label event
stores the label's *name*, and names are unique (0007, `COLLATE NOCASE`), so
resolving one back to an id is a lookup rather than an interpretation of prose.

Verbs like `changed_status` or `assigned` are *not* reversible here, and the
reason is worth stating: undoing them needs the value that was in force before,
and `activity.detail` is documented as human-readable specifics, not a structured
before/after. Driving a mutation by parsing that prose would be inference dressed
up as a feature. Reversing those verbs honestly needs structured prior state
recorded at write time — a real change, not a bigger registry.

Classified and refused today: comments and attachments (`one_way` — delete them
explicitly), destroyed rows such as `page_deleted` / `space_deleted` /
`deleted_project` (`trapdoor`), and `page_edited` (`one_way` — page history
already keeps the prior version, so restore it explicitly).

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

- Four verb pairs are reversible; everything else is refused, with a reason.
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
