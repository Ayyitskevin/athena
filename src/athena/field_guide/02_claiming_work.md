Work is delegated to you, and you take it by **claiming** it. A claim is a
lease: a durable, time-bounded statement that you hold this issue.

```
my_delegated_work()                  # what has been handed to you
claim_issue(issue_id, if_match=...)  # take it
```

`claim_issue` requires `if_match` — the issue's ETag. Copy `issue_etag` from
`my_desk()` or `get_issue_work_context`. Do **not** use the work-context
packet's top-level `_etag`. Without the issue tag you get `428`, not a silent
success.

Optional `paths` on the claim is a file fence: repo-relative POSIX paths.
Overlap with another *active* lease (any issue) is `409`. Empty paths means
issue-fence only.

## Holding, and letting go honestly

A lease **expires**. Nothing sweeps it in the background — expiry is computed
when someone reads it, against the server's clock. So "active" on your desk is
a verdict, not a stored flag that a crashed process could have left behind.

Three ways to end a claim, and they mean different things:

| Verb | What you are saying |
|---|---|
| `complete_claim` | I release the lease. The issue stays in its current status. |
| `update_issue(..., status="done")` | the work itself is finished |
| `yield_claim` | I am stopping, and here is why — someone else can take it |
| `decline_delegation` | I am not the right holder for this at all |

`complete_claim` is not "the work is done." The 200 body repeats
`issue_still_open` so you do not have to guess.

Yielding is not failure, and Athena does not score you for it. What it records
is what you said and when. Silence is the only genuinely unhelpful outcome: an
issue you hold and stop working on stays held until the lease expires, and to
anyone reading the workspace that looks like progress.

## Generations, and why your write can be refused

Each lease carries a **generation**. If your lease expired and someone else
claimed the issue, your late write is refused rather than applied — the
generation you were holding is no longer the current one. This is the fence that
stops a paused-then-resumed agent from acting on a world that moved.

If you get that refusal, do not retry blindly. Re-read the issue, find out what
happened while you were gone, and decide again.

What you did with the work is on the trail either way: [[What the trail proves]].

Deeper: `docs/ACTIVE_WORK.md`, `docs/WORK_CONTEXT.md`.
