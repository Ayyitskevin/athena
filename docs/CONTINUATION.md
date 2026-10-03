# Continuation convention

Use this recipe for a fresh session pickup, a scoped successor or a clipped
evidence reference. It is documentary context carried by existing comments,
runbook links and handoff evidence references, not a scheduler or resume grant.

## Write the smallest continuation

Record these six answers beside a reference to the
[evidence manifest](EVIDENCE_MANIFEST.md), rather than copying its contents:

| Answer | Completion criterion |
|---|---|
| Finished question | Exact finite question/result, with manifest locator and digest. |
| New delta | Smallest genuinely new missing result; null if all remaining work is parked. |
| Next artifact | Named output and existing write fence, or null for passive standby. |
| Checkpoint | Absolute RFC3339 time with offset for admitted active work; null when parked, with the original historical promise preserved separately. |
| Holds | Exact retained dependencies/holds and their sources; distinguish unresolved checks from authority. |
| Authentic trigger | Actual scoped return/new admitted evidence/explicit grant needed to activate that delta. Resume prose alone supplies no trigger. |

While possession continues, use an existing comment/runbook reference. A
documentary checkpoint does not require yielding. If possession actually changes,
use the existing [claim handoff contract](ACTIVE_WORK.md#persistence-retries-and-recovery),
including current root issue ETag and exact lease generation where required;
do not infer possession from this note. Its receipt acknowledges context only.
For an actual `request_fresh_context`, fit the summary/reference into the
[existing bounded handoff](RUN_CONTROLS.md#the-fresh-context-handoff), not new fields.

A fresh heartbeat is a cooperative report, not task progress; a missed checkpoint
does not make another actor entitled to take over. New timestamps do not clear
overdue promises, failures or holds. Keep absent/unknown identity evidence explicit;
Athena cannot attest native process lineage from a client label.

## Recover exact evidence

1. Read the existing bounded work-context and check every `clipped`/`truncated`
   marker. Identify the exact manifest version and locator before interpreting
   results. Completion: all truncation relevant to the claimed result is known.
2. Resolve only a locator already covered by the reader's grant and visibility.
   Manually obtain the complete file: existing `GET /attachments/{id}` for a
   visible stored blob; full visible comments via `GET /issues/{id}/comments`;
   an exact permitted local file or immutable Git object for local retained work.
   Completion: original bytes, not a search snippet or guessed/latest replacement.
   Comments are mutable, so verify their frozen bytes separately. These are
   existing surfaces, not an arbitrary-path fetch API.
3. Independently compare SHA256, byte count, Git ref when applicable and cited
   line bounds. Missing, hidden, changed or unavailable exact versions mean
   PARTIAL; retain the mismatch and any earlier FAIL. Do not probe hidden existence,
   widen visibility, or synthesize absent originals. Completion: the exact relevant
   subject and verdict bytes match, or the remaining gap is named.
4. Read the entire recovered relevant record and its limits. Apply only the precise
   successor proposition, leaving predecessor failures and other scopes intact.
   Completion: one justified current result, not an aggregate inferred approval.
5. Read the six continuation answers and fresh applicable grant/ownership state.
   Start only an admitted new delta after its authentic trigger. If none exists,
   return the prerequisite and settle. Completion: no repeated completed task or
   manufactured follow-on.

[`run_replay.py:20–26,111–143`](../src/athena/core/run_replay.py#L20-L143)
caps a replay at 5,000 focal events and returns `partial`. The database exporter
uses the same builder. Full recovery of a referenced verdict file is **not** full
recovery of that activity run: preserve its partial flag and report unrecovered
history. Replay records facts; consumers never re-execute its side effects.

## Fictional example interpretation

The [examples](evidence-convention/) preserve source-v1 and its fictional spelling
failure. Source-v2 has a separate fictional verdict, bound only to that changed
file, which supersedes only the misspelled-word proposition. The first source and
verdict remain available. Neither record represents a genuine reviewer, owner
return, operational acceptance, runtime result or permission to start work.
Their continuation answers leave next artifact/checkpoint/trigger null: this
illustration does not dispatch a task. Retrieval savings and full activity-history
recovery are unmeasured/unrun.
