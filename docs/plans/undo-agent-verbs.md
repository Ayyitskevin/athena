# Spec: classify the agent verbs undo still does not know

Working spec. [`UNDO.md`](../UNDO.md) stays the design of record until a slice
lands; this document says what to build, in which order, and what not to wire.

## Outcome

On the activity feed, Undo appears for a parent change, a sprint move, and a
typed issue link once that event has a structured fact. Claim, yield, complete,
and the other coordination verbs stop saying `unclassified`. They stay without
an Undo control, and the refusal names why.

`created` and `page_created` stay `unclassified`. Archive is not the inverse of
create.

## Why this shape

Undo offers a button only for `two_way` verbs (`undo.is_undoable`). Everything
else returns `undo_not_reversible` (422). An `unclassified` verb uses one
shared reason: "this action has no registered inverse." That sentence means the
engine was never taught the verb.

The verbs an agent emits while holding work are still in that bucket:

| Verb | What the event actually stores |
|---|---|
| `set_parent` | The new parent's key, as prose. The previous parent id is absent. `removed_parent` stores no parent id at all. |
| `moved_to_sprint` | The new sprint's name, as prose. The previous sprint id is absent. `removed_from_sprint` stores a name only when the recorder was allowed to, and a project move blanks it on purpose. |
| `linked` / `unlinked` | `"{relation} {key}"`. The other issue's id is not a column. |
| `claimed`, `lease_renewed` | `generation …; until …` as prose. |
| `claim_completed`, `claim_yielded`, `claim_handoff_resumed` | Generation, and for a yield the handoff token and reason, as prose. |
| `delegated`, `added_contributor`, `removed_contributor` | A display name. Names are not unique. |
| `delegation_declined` | Optional prose about a released generation. The person who left is `activity.actor_id`, because decline is self-service. |
| `changed_project` / `removed_from_project` | A project name. Out of this spec. |
| `created`, `page_created` | The birth of the row. |

[`UNDO.md`](../UNDO.md) forbids driving a mutation by parsing `activity.detail`.
Status could be reversed only because migration 0055 already stored
`before_status`. Assignee needed migration 0068 for the same reason: the event
text was not evidence. Parent, sprint, link, and delegation membership are in
that second position today. A compensator that reads the prose would be the
bug the assignee fact exists to prevent.

Two further constraints, copied from the status and assignee compensators:

- **Still in force, when the write is not idempotent.** Putting a parent or a
  sprint back is not a no-op. If the issue has moved on, restoring the old
  value would overwrite a newer one and stamp the result as a reversal.
  `undo_no_effect` (409), and nothing is written.
- **No backfill.** An event older than the fact migration has no row. That is
  `undo_not_reversible` (422), never a guess.

Link add and link remove are idempotent: repeating them records no event. The
undo engine already treats "the compensator recorded nothing" as
`undo_no_effect`. Links therefore do not need a separate still-in-force read.
Parent and sprint do.

## Slice 1 — name the verbs that will not grow an Undo button

No migration. Register each verb in `aegis/issue_undo.py` with a reason. The
activity feed still shows no Undo control. `POST /activity/{id}/undo` and
`undo_action` answer 422 with `undo_not_reversible`, and the detail says the
class and the reason below.

| Verb | Class | Reason to register |
|---|---|---|
| `claimed` | `one_way` | Taking the lease is possession. The only release command records `claim_completed`, which asserts the work was finished. Undo must not say that. |
| `lease_renewed` | `one_way` | The previous expiry is not a fact. Extending again is the explicit command. |
| `claim_completed` | `one_way` | The lease was released and the completion was published. Claim again if the work is still open. |
| `claim_yielded` | `one_way` | The handoff was published for someone to read. Resume or re-delegate explicitly. |
| `claim_handoff_resumed` | `one_way` | Receipt of a handoff is a decision on the record. |
| `delegation_declined` | `one_way` | Decline also drops a held lease. Re-adding the contributor would not restore that lease, so it is not an inverse. Delegate again explicitly. |
| `delegated`, `added_contributor`, `removed_contributor` | `one_way` | The other person's id is not stored. Add or remove them explicitly. |
| `changed_priority` | already `one_way` | No change. Listed so this slice does not reopen it. |

`created` and `page_created` are not in the table. Registering them as
`trapdoor` would claim we had decided they can never be reversed. We have not.
They stay `unclassified`.

`changed_project` and `removed_from_project` stay `unclassified` too. A project
move remaps status and clears the sprint; reversing it is a different spec.

Tests for this slice encode the refusal text, not a 200:

- Each registered verb undoes with `undo_not_reversible`, and the detail
  contains the class (`one_way`) and a stable fragment of the reason.
- `created` still undoes as `unclassified`.
- `is_undoable` is false for every verb in the table.
- Re-registering a verb with a different class still raises. Slice 1 must not
  touch verbs that are already `two_way`.

## Slice 2 — parent, then sprint, then links

Each family is its own change: one migration, the recorder writes the fact in
the same transaction as the event, one compensator, then `UNDO.md` gains the
row. The migration number is whatever is unused when the slice starts (0080 is
the latest today). No backfill. Facts are immutable, keyed 1:1 to `activity.id`,
and a trigger refuses a fact whose event is imported, visibility-restricted, or
the wrong verb — the 0068 shape.

The compensator calls the existing command as the undoing actor. It does not
write SQL of its own.

### Parent

Fact: `issue_id`, `before_parent_id`, `after_parent_id`. Null is "no parent."
A check refuses a no-op fact. `set_parent` requires `after_parent_id` not null;
`removed_parent` requires it null.

Compensator: `issue_commands.set_issue_parent` with `before_parent_id`.

Gates, before the command:

- The issue still exists. A missing issue is `undo_no_effect`.
- `issue.parent_id` equals `after_parent_id`. Otherwise `undo_no_effect`.
- No fact row: `undo_not_reversible`.

The command already refuses a missing parent, a cycle, and a parent the actor
cannot see. Those propagate as `undo_refused_by_command`.

### Sprint

Fact: `issue_id`, `before_sprint_id`, `after_sprint_id`. Null is "backlog."
`moved_to_sprint` requires `after_sprint_id` not null; `removed_from_sprint`
requires it null.

Compensator: `issue_commands.update_issue` with `sprint_id=before_sprint_id`.

Gates, before the command:

- The issue still exists.
- `issue.sprint_id` equals `after_sprint_id`. Otherwise `undo_no_effect`.
- The issue's project is still the project of the sprint being restored, or
  both sides are null. A project move clears or remaps sprint membership, and
  a sprint id from the old project is not meaningful after it. That mismatch
  is `undo_not_reversible`, same idea as status refusing a move.
- No fact row: `undo_not_reversible`.

### Links

Fact: `issue_id` (the event's target, the "from" side), `other_issue_id`,
`relation` constrained to `blocks`, `blocked_by`, `relates`. The verb is the
direction: `linked` adds that edge, `unlinked` removes it.

Compensator: `issue_commands.unlink_issues` for `linked`,
`issue_commands.link_issues` for `unlinked`, addressed by the stored numeric
id rather than the key in the prose. `link_issues` takes a ref; the
compensator passes the decimal id, which `get_by_ref` already accepts.

No separate still-in-force read. The commands are idempotent, and a second
write that records nothing is already `undo_no_effect`. A different edge on
the same issue is a different fact and is left alone.

A hidden target collapses to the command's existing "no such target issue."
That is `undo_refused_by_command`, not a reconstructed edge.

## What this does not change

- The issue history narrative. It already tells claim, yield, and handoff.
  This spec is the control on the activity row.
- Notifications, watches, and handoff text already sent. A compensator
  reverses the membership or the edge, not the inbox, which is the limitation
  `UNDO.md` already states for labels.
- Bulk undo, undo-this-run, and parsing `activity.detail`.
- `recovery.py`, `portability.py`, and the idempotency transaction. A fact
  commits inside the command's existing transaction, beside the event.

## Done for a slice

`UNDO.md`'s reversible table and the "eleven verbs" limitation match the code
after that slice. `grep` shows the new `undo.register` calls. A test undoes a
fresh event and a pre-migration event, and a test shows a newer parent or
sprint is not overwritten. The activity feed offers Undo only for the new
`two_way` rows.
