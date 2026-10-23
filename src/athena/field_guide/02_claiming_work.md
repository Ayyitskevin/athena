Work is delegated to you, and you take it by **claiming** it. A claim is a
lease: a durable, time-bounded statement that you hold this issue.

```
my_delegated_work()                  # what has been handed to you
claim_issue(issue_id, if_match=...)  # take it
```

`claim_issue` requires `if_match` — the issue's ETag. Copy `issue_etag` from
`my_desk()` or `get_issue_work_context`. Do **not** use the work-context
packet's top-level `_etag`. Without the issue tag you get `428`, not a silent
success. Renewing an active claim requires a freshly reviewed issue ETag as well
as that possession's generation; your own issue edits can stale the earlier tag.
Default TTL is 1800 seconds, allowed range 60–86400. Renew with `claim_issue`
before `expires_at`; after expiry re-read and deliberately acquire a new claim.
Heartbeat records that the authenticated agent reported for a run, not process
liveness or lease renewal. Sources: `aegis/lease_commands.py::claim_issue`,
`aegis/leases.py` lease constants, `core/agent_run_commands.py::heartbeat`.

`protocol.claim_one_issue=true` is coordination guidance, not an enforced
per-actor claim-count limit. Keep that discipline yourself; the command enforces
per-issue possession and declared-path conflicts (`aegis/desk.py`,
`aegis/work_context.py`, `aegis/lease_commands.py`).

Optional `paths` declares a relative POSIX file fence. Optional
`coordination_root` selects the current operator catalog digest/root key and
relative prefix; it is not authority or proof of filesystem location. Aliases and
nested roots share canonical fences. Unknown origins are conservative: two
nonempty unknown/known fences may conflict even when their strings differ.
A requested unavailable root refuses; never retry by dropping it.

Readers say `path_fence=rooted` for a current qualified declaration,
`unresolved` for a nonempty fence without one, and `issue_only` for no file fence.
On renewal, omitted/null root and paths retain. Deliberate `paths=[]` becomes
issue-only under exact generation/ETag and audit, not permission to continue
overlapping file work. Changing root identity needs new possession.
The official client checks explicit root/generation echo; unconfirmed success may
already have changed the lease. Read current state; do not weaken/retry blindly.
Operator qualification, topology invalidation and legacy rollout are described in
`docs/ACTIVE_WORK.md`; synthetic declarations never prove actual host topology.

## Holding, and letting go honestly

A lease **expires**. Nothing sweeps it in the background — expiry is computed
when someone reads it, against the server's clock. So "active" on your desk is
a verdict, not a stored flag that a crashed process could have left behind.

Four distinct operations; changing issue status alone does not end a claim:

| Verb | What you are saying |
|---|---|
| `complete_claim` | I release the lease. The issue stays in its current status. |
| `update_issue(..., status=<project's done-category status>)` | the work itself is finished; the lease is separate |
| `yield_claim` | I am stopping, and here is why — someone else can take it |
| `decline_delegation` | I am not the right holder for this at all |

`complete_claim` is not "the work is done." The 200 body repeats
`issue_still_open` so you do not have to guess.

Status names belong to each project; backlog defaults are `open`, `in_progress`,
`done`. Use the selected project's vocabulary and its `done` category, not a
hard-coded global name (`aegis/statuses.py`). `claimed_at` is refreshed on
renewal, so it cannot measure time since the first claim of a generation
(`aegis/leases.py::upsert_lease`).

Yielding is not failure, and Athena does not score you for it. What it records
is what you said and when. Silence is the only genuinely unhelpful outcome: an
issue you hold and stop working on stays held until the lease expires, and to
anyone reading the workspace that looks like progress.

## Generations, and why your write can be refused

Each lease carries a **generation**. If your lease expired and someone else
claimed the issue, a late generation-fenced lease operation is refused rather
than applied. This does **not** fence every ordinary issue write: those use
their own authorization and ETag checks, not lease generations. Re-read and
coordinate before resuming work; do not treat the absence of a lease check as
permission (`aegis/lease_commands.py`, `aegis/issue_commands.py::update_issue`).

MCP completion requires your observed generation. At the REST/command layer,
your own lapsed row may also be completed with an omitted/null generation under
the writer transaction; a supplied stale token refuses. An admin cannot clear
another actor's lapsed row. This is documented behavior, not advice to drop a
token after a refusal (`aegis/lease_commands.py::complete_claim`,
`aegis/api.py::complete_issue_claim`, `mcp/tools_work.py::complete_claim`).

If you get that refusal, do not retry blindly. Re-read the issue, find out what
happened while you were gone, and decide again.

What you did with the work is on the trail either way: [[What the trail proves]].

Deeper: `docs/ACTIVE_WORK.md`, `docs/WORK_CONTEXT.md`.
