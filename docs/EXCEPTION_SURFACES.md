# Steering by exception: the attention rollup and security signals

`VISION.md`'s third steering rule is "the human steers by exception" — surface
*decisions*, not noise. Athena grew the decisions one at a time, and each landed on
its own page: claims needing attention in Mission Control, failing automation rules
beside them, failing webhooks elsewhere, pending approvals on the agents page,
unanswered kill requests on the worker list, and refused logins nowhere at all. An
operator expected to steer by exception had to already know six places to look.

Two surfaces close that: one rollup that counts everything asking for a human, and
one page for the refusals that were recorded but never rendered.

## The fleet-attention rollup

On the dashboard, admin-only, above everything else:

| Signal | Counts | Window |
|---|---|---|
| Claims needing attention | Rows the active-work projection marked `needs_attention` | the examined window |
| Approvals waiting on you | Pending approval requests | standing |
| Workers told to stop | Kill requests not yet confirmed | standing |
| Run controls awaiting an agent | Live control requests — unsettled, unexpired | standing |
| Failing automation rules | Rules with a recorded failure | standing |
| Failing webhooks | Endpoints with a delivery failure | standing |
| Budget ceilings hit | `agent_budget_exhausted` events | last 24h |
| Boundary refusals | Failed logins, revoked tokens, scope denials, paused refusals | last 24h |

**The card computes nothing.** Every number comes from the surface that owns it,
and each links there, so the rollup can never disagree with the page it sends you
to. That is the whole design constraint: an aggregate that re-derives state is an
aggregate that eventually lies.

**Every count says what it counts.** Standing state — an approval nobody answered,
a worker that never confirmed — is *not* window-bounded, because a month-old
unanswered request is still unanswered. Event-counted signals *are*, because
"someone probed the boundary once, months ago" is not the alarm that "someone is
probing now" is. And the claims count carries its own scope line
(`of N claims examined`, plus `more exist` when the window clipped), so it never
implies a fleet-wide total it did not compute.

**A quiet fleet says so, carefully.** The empty state reads "nothing is asking for
you right now — that is what the last N hours recorded, not a promise that every
agent is healthy." Athena does not observe agent processes; see
[`WORKERS.md`](WORKERS.md) and [`ACTIVE_WORK.md`](ACTIVE_WORK.md) for why.

## Security signals

`core/security_events.py` has recorded four refusals since it was added: a failed
login against a real account, a revoked token still being presented, a scope-capped
actor probing past its grant, and a paused account that keeps trying. Each is
exactly the signal an operator wants *before* something goes wrong — and each was
readable only by someone who already knew the four verb names and thought to filter
the activity feed for them.

```text
GET /security/events?verb=scope_denied&since=2026-07-25%2000:00:00&limit=50
GET /security/counts
GET /admin/security
list_security_events(verb="scope_denied", since="2026-07-25 00:00:00")
```

- **Admin-only.** A list of who has been probing is operator intelligence, not
  general history.
- **The verb vocabulary is closed.** An unknown verb is a 422, so this surface
  cannot quietly become a general activity reader wearing a security name.
- **Attribution is to the account involved** — the user whose password was guessed
  at, the owner of the revoked token, the actor whose scope ran out. That is the
  best available truth for a refusal, and it is what the recorder already stored.
- **Counts are zero-filled**, so a quiet fleet reads as an explicit zero rather
  than a missing key.

## What these surfaces do not claim

- A refusal is evidence of an **attempt**, not of a compromise, and not of who was
  behind it. Athena records that a boundary was hit.
- Absence of refusals is not evidence of absence of probing: only the four recorded
  verbs are counted, and recording is best-effort by design (a failure event must
  never turn a clean 401 into a 500).
- The rollup is a count of *what is recorded*, not a health check. Nothing here
  observes an agent process, and `ACTIVE_WORK.md`'s rule stands: `observed` means
  "no known reason applied at the snapshot", never "healthy".
- There is no alerting, no notification, and no escalation — the operator still has
  to look. Pushing these signals outward is roadmap work.
