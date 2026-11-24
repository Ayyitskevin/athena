# Durable agent budgets

Athena's steering rules promise that every agent action is **attributable,
reversible, and bounded**. Attribution is the append-only activity trail.
Bounding used to be only the per-token rate limiter in `core/rate_limits.py` —
which lives in process memory and forgets everything on restart. That bounds a
burst; it is not a budget.

A **budget** is a durable ceiling on how many metered writes one account may make
per fixed window. The charge is folded into the metered command's own
transaction, so the mutation and its charge commit or roll back together.

## What is metered

Actions — **not tokens, not dollars, not model spend.**

Athena is the control plane. An agent's inference spend happens inside that
agent's own process, which Athena never observes, so Athena deliberately carries
no cost column it could not honestly populate. If an external meter ever reports
spend, a cost dimension can be added additively.

That condition is still not met: nothing here meters spend. What exists instead is a
separate, clearly labelled **reported-usage ledger** (below), kept apart from the action
budget so a figure an agent merely stated can never be mistaken for one Athena measured.

The metered writes today are:

| Write | Command |
|---|---|
| Issue create | `aegis.issue_commands.create_issue` |
| Issue edit (title/body/status/priority/assignee/project/sprint placement) | `aegis.issue_commands.update_issue` |
| Page create | `mentor.page_commands.create_page` |
| Page edit | `mentor.page_commands.edit_page` |
| Page create from a template | `mentor.page_commands.create_page_from_template` |
| Daily-note create | `mentor.page_commands.ensure_daily_page` — charged only when the note is actually created; revisiting an existing day's note is a read and spends nothing |
| Dispatch request | `aegis.icarus_commands.request_dispatch` |

Deliberately **not** metered:

- **Reads.** A budget must never blind an agent; it bounds what an agent changes.
- **Automation rule firings** (`update_issue_as_automation`). Rules are the
  operator's own automation, not delegated agent work — a budget must never
  silently stop a rule the operator configured.
- **Keeping and ending a claim**: a lease renewal, a yield, a completion, a
  release, a handoff resume and a declined delegation. The way out of a claim
  must stay open to an account that has spent everything.
- Other durable writes (labels, attachments, links, watches, comment edits and
  the rest). This is a bounded slice, not a claim of total coverage.

## The coordination counter

Comments, claims and delegations are counted apart, against a second, optional
ceiling on the same budget (decision D6). A budget of 3 an hour used to stop
the fourth issue edit and no comment at all.

| Write | Command |
|---|---|
| Issue comment | `aegis.comment_commands.create_comment` |
| Page comment | `mentor.page_comment_commands.create_page_comment` |
| Claim (an acquisition, by `claim_issue` or by pickup) | `aegis.lease_commands.claim_issue` |
| Delegation (when one is made; repeating one that stands spends nothing) | `aegis.issue_commands.add_contributor` |

- **Apart, not added.** A seat out of action budget can still say so in a
  comment and hand the work back; a seat that talks too much keeps the writes
  that do its work. Each counter refuses only its own writes.
- **One window.** `coordination_limit` shares the budget's `window` and
  `window_started_at`; both counters start again together.
- **Optional.** `coordination_limit` omitted or `null` leaves coordination
  unlimited, which is what every budget set before it existed means. `0`
  refuses every comment, claim and delegation in the window. A budget is
  replaced whole: setting one without `coordination_limit` removes a ceiling
  set earlier.
- **Set the same way**: `PUT /users/{id}/budget` with `coordination_limit`,
  `set_agent_budget(..., coordination_limit=N)`, or the second number on the
  agents page. The budget read, `whoami` and the desk carry
  `coordination_limit`, `coordination_used` and `coordination_remaining`.

A coordination write past the ceiling is refused `429
agent_coordination_budget_exhausted`, with `budget`, `retry_after_seconds` and
`Retry-After`. It names no leases and tells nobody on the account's behalf,
because the account can still do, renew, complete or yield what it holds. The
first refusal of a window records one `agent_coordination_budget_exhausted`
event for the operator; it is counted with the action ceiling's under "Budget
ceilings hit". Each ceiling's first refusal is recorded, whichever is spent
first.

## Opt-in by default

**No budget row means unlimited.** Applying migration 0062 changes nothing until
an admin sets a ceiling, exactly like the blocked-close policy in 0060. An
unbudgeted agent behaves precisely as it did before.

## Window semantics

A **fixed** window, not a sliding one. `window_started_at` anchors the current
period; the first charge at or after `window_started_at + length` resets the
counter and re-anchors the window. Windows are `hour` or `day`.

A fixed window permits a burst across the boundary — up to twice the limit within
one window-length, if an agent spends its ceiling at the end of one window and
again at the start of the next. That is an accepted, documented trade-off for a
counter an operator can reason about and that needs no background job, honoring
"one operator, zero ops".

Reads roll the stored row forward to now, so an elapsed window displays as fresh
rather than spent. The reset is *persisted* by the next charge, not by looking.

## Guarantees

- **Atomic.** The charge happens inside the command's `BEGIN IMMEDIATE`
  transaction. A write rejected *after* the charge — a validation failure, a
  policy refusal, a failed audit — rolls the charge back with it, so an agent is
  never drained by its own errors.
- **Serialized.** Two concurrent writes cannot both spend the last unit; SQLite's
  single-writer reservation orders them.
- **Durable.** The counter survives process restart.
- **Charged once per logical write.** An exact `Idempotency-Key` retry replays the
  stored response without reaching the command, so it is not charged again.
- **Audited.** Setting and clearing a budget each record an event
  (`agent_budget_set` / `agent_budget_cleared`) atomically with the change.
  Exhaustion records `agent_budget_exhausted` — an agent hitting its ceiling is a
  decision the operator should see, not silence. It is recorded once per budget
  window, on the first refused request: a later refusal in the same window
  records nothing, so the trail and the operator's attention list have one row
  per account and window, not one per request.
- **Preserves consumption across a limit change.** Raising a limit mid-window
  releases the agent immediately *without* gifting a fresh window; lowering it can
  leave an agent already over, in which case `remaining` clamps to 0.

## Refusal contract

A metered write past the ceiling is refused with:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 3421

{
  "detail": "agent budget exhausted: 50/50 metered actions used this day",
  "code": "agent_budget_exhausted",
  "budget": {
    "user_id": 42, "window": "day", "action_limit": 50,
    "action_used": 50, "remaining": 0,
    "window_started_at": "2026-07-25T00:00:00Z",
    "coordination_limit": null, "coordination_used": 0,
    "coordination_remaining": null
  },
  "held_leases": [
    {"issue_id": 7, "generation": "6403f3965f78a1aa9339a041f474a4f4"}
  ],
  "retry_after_seconds": 3421
}
```

`code` is the stable contract — branch on it rather than parsing prose.
`Retry-After` is the server's own seconds-until-window-reset, and
`retry_after_seconds` is the same number in the body.

### Out of budget while holding work

`held_leases` names each lease the refused account holds, at most 20. A lease
held through the rest of the window keeps its issue from every other seat until
it lapses, so the refusal's `next` is `yield_claim` with reason `capacity` for
each of them: a yield is never metered, by either counter, it records
what was attempted, and the issue returns to the ready lane. An account that
holds nothing stops writing until the window ends.

The first refusal of a window also tells the accounts that answer for that
work. For each held lease it records one `holder_budget_exhausted` event on the
issue, by the holder, whose `detail` is `generation <g>; holder_id <n>;
retry_after_seconds <n>; holder <name>`, and puts it in the inbox of the
issue's assignee and of the account that delegated the issue to the holder,
when they can still see the issue. The holder is not told again. A conductor
waiting on its outcome's events (`wait_for_events`, `mine=true`) is woken by
it. Nothing is released and nothing changes on the issue: the holder yields, or
the lease lapses and says so.

## Surfaces

```text
GET    /users/{id}/budget     # admin for anyone; any actor may read its OWN
PUT    /users/{id}/budget     # admin: {"window": "day", "action_limit": 50}
DELETE /users/{id}/budget     # admin: back to unlimited (idempotent)
GET    /users/me              # carries your own "budget" (null when unbudgeted)
```

Setting and clearing are admin-only, and no account may change its **own**
budget: `budget_commands` refuses with a 403 when the target is the actor,
whatever the actor's role or token scope, and writes no event. An agent the
operator has made an admin still cannot raise or remove the ceiling that bounds
it; the browser cockpit shares the same command, so the rule holds there too.

MCP: `get_agent_budget`, `set_agent_budget`, `clear_agent_budget`, and the
`budget` field already present in `whoami`. The browser cockpit shows each
agent's consumption on **Admin → Agents**, with set/clear controls.

An agent is expected to learn its ceiling by **asking** (`whoami`), not by being
refused — the same principle as `scopes`.

## Limitations

- A budget bounds *metered writes*, listed above — not every possible action, and
  not resource consumption of any kind.
- `action_limit: 0` freezes metered writes while leaving reads working. That is
  narrower than **pause**, which refuses every authenticated action.
- A fixed window permits the boundary burst described above.
- Budgets are per-user, not per-token: an agent holding several tokens shares one
  ceiling. That is deliberate — the budget bounds the *actor*, not the credential.
- Nothing here restores a spent budget early except another admin raising the
  limit or clearing the budget.

## Reported usage (not metered, not a budget)

An agent may add an optional `usage` object to its check-in (`PUT /agent-runs/heartbeat`,
MCP `heartbeat_agent_run`) and to `complete_claim`. Keys use the GenAI span attribute names
without their `gen_ai.usage.` prefix: `model`, `input_tokens`, `output_tokens`,
`cached_tokens`, plus `estimated_cost` with an ISO 4217 `currency` (they go together) and
`reported_at` (ISO 8601 with an offset). At least one key is required; unknown keys, negative
or non-integer counts, an unlisted currency and a credential-shaped `model` are refused.

Each report means "usage since my previous report", so totals are plain sums. Athena stores it
as a fact inside the event detail JSON (`usage_reported` for a check-in, the `usage` key of the
completion report for `complete_claim`); there is no column. A check-in that carries no usage
writes nothing new. An exact retry of a check-in is a second report.

Three read models (`aegis/reported_spend.py`) sum what the viewer may see: per run, per agent per
server-clock day, and per issue (completions only; a check-in names no issue). Costs are summed
per currency, never converted. Check-in reports target the agent and are admin-only; completion
reports follow the issue's visibility, so a private project's reports are absent for anyone who
cannot see it. Only the newest 5000 events of each kind are read.

**Admin → Agents** shows them under the label *reported by the agent, not metered by Athena*.
They enforce nothing, never touch the action budget, and carry no ceiling.

Two operational notes. Every check-in that carries `usage` is one permanent, hash-chained
activity row (nothing here prunes), so a busy fleet grows the trail by one row per report; send
usage at a coarse interval or only when it changes. The agents cockpit reads the newest 5000
events of each kind once per page and says "Recent window only" when that window is full. These
bookkeeping events are left out of an agent's own "Recent activity" and "last active" so they
cannot crowd out its real actions; the global activity trail still lists them.
