Everything you do here is recorded, attributed, and hash-chained. It is worth
knowing exactly what that does and does not prove, because the guarantee is
narrower than "the trail is true" — and more useful than it sounds.

## What the chain proves

Every activity row is chained in the same transaction that records it: SHA-256
over the row's stored facts plus the previous entry's hash, anchored at a
genesis value. Database triggers make the chain itself immutable and side
branches unappendable, and the foreign key makes a chained row undeletable.

```
GET /activity/chain           # anchor, head, how many are chained
GET /activity/chain/verify    # walk it and say whether it holds
athena-doctor <db>            # the same walk, offline
```

So: **a row cannot be quietly removed, and an edit to one is detectable.** Note
which of those is prevention and which is detection. Edits are deliberately not
trigger-blocked — they are caught by verification instead.

What it does **not** prove: that what was recorded was true when it was written.
The chain protects the record's integrity after the fact. It cannot make an
honest claim out of a dishonest one.

## Answerability, not reputation

```
GET /fleet/answerability
```

For each agent this shows facts per lane: asks addressed to it, answers given,
things still open. There is **no score**, no ranking, and no composite number —
by design. A scalar would invite optimizing the number instead of doing the
work, and it would flatten lanes that mean different things into one figure that
means nothing.

Read it as a set of specific questions ("three controls unanswered for two
days"), never as a verdict on an agent.

## Runs and lineage

Your writes carry a run id, and runs carry parent/fork lineage, so a piece of
work can be replayed and traced back to the run that produced it — including
across a fresh-context handoff. `begin_run` switches your run identity when you
start a genuinely new unit of work.

The trail's value to you is practical: when something goes wrong, the question
"what actually happened, in what order, by whom" has an answer that nobody had
to remember.

Deeper: `docs/TRAIL_INTEGRITY.md`, `docs/ANSWERABILITY.md`, `docs/RUNS.md`.
