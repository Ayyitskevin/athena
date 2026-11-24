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
| Questions waiting on you | Blocking questions from yielded claims nobody has resumed, less `needs_input` questions a person other than the asker has answered since | standing |
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

**One number, wherever it is shown.** The rows above sum to the "needs you" total
(`fleet_attention.needs_you`). The Intervene badge in the nav shows that total and
nothing else; a page renders the badge and the card from one read, and the card's
live refresh carries the badge with it, so the two never show different numbers.
The unread inbox is not in it: an inbox row says something happened, not that a
decision waits. It is shown beside the badge as its own labelled count. Sign-in
lands on the dashboard, where this card is the first panel.

**Silence has a grace.** A tagged claim that has not checked in counts only once
its heartbeat is overdue (more than `ATHENA_AGENT_RUN_STALE_SECONDS` since the
possession was acquired or since the run's last check-in), so a claim taken a
moment ago is not counted; a renewal does not restart that grace. An untagged
claim, which can never check in, counts at once. See
[`ACTIVE_WORK.md`](ACTIVE_WORK.md#state-and-attention-semantics).

**A question waits on whoever must act next.** A `needs_input` question that a
person (not an agent account, not the asker) has answered since it was asked, by a
comment, status change, assignment change or claim, waits on the agent and leaves
the count; it stays listed, marked "answered, waiting on the agent", until the
agent resumes. The asker commenting again restates it. Another agent's reply
answers nothing here, and a `blocked` yield's question is never marked answered.

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
- There is no alerting and no escalation, and only one signal notifies anyone: a
  lapsed lease is recorded once as a `lease_lapsed` event and told to the issue's
  assignee and the account that delegated it
  ([ACTIVE_WORK.md](ACTIVE_WORK.md#a-lapsed-lease-is-an-event)). For the rest the
  operator still has to look. Pushing these signals outward is roadmap work.
