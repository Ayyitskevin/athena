When a run discovers something durable — a command that actually works, a trap
in the deploy, a reason the obvious fix is wrong — record it where the next
agent will look. That is not the issue comment thread. It is the docs.

```
record_run_learning(issue_id, summary=...)
```

The first learning on an issue needs a Mentor space for the runbook. If you
can see exactly one space, Athena uses it. If you can see several, the 422
lists `suggested_spaces` — pick one. `my_desk()` and
`get_issue_work_context` already carry that hint.

The learning is promoted into the issue's runbook: quoted, attributed to the run
that found it, and left on the trail. Work writes back to docs.

## Write what you observed, not what you concluded about yourself

A good learning is a fact the next reader can check:

> `athena-doctor` refuses a restore whose attachment directory is missing, so
> restore the blobs before the database, not after.

A bad one is a claim about your own performance:

> Handled the restore correctly and improved the process.

Athena records asks, claims, and observations. It does not record whether you
did well, and there is no score anywhere in it for you to move. Writing as if
there were produces documentation nobody can act on.

## Where it lands

The runbook lives on the issue and travels with it. If the learning is bigger
than one issue — it changes how the team does something — put it in a page in
the space that owns that subject, and cite the issue with `[[issue:N]]`. The
backlink appears on the issue for free, so the doc and the work point at each
other without either one being copied.

Deeper: `docs/RUN_LEARNINGS.md`.
