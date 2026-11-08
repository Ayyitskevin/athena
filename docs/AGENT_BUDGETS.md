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
- Other durable writes (comments, labels, leases, attachments, and the rest).
  This is a bounded first slice, not a claim of total coverage.

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
  decision the operator should see, not silence.
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
    "window_started_at": "2026-07-25T00:00:00Z"
  }
}
```

`code` is the stable contract — branch on it rather than parsing prose.
`Retry-After` is the server's own seconds-until-window-reset.

## Surfaces

```text
GET    /users/{id}/budget     # admin for anyone; any actor may read its OWN
PUT    /users/{id}/budget     # admin: {"window": "day", "action_limit": 50}
DELETE /users/{id}/budget     # admin: back to unlimited (idempotent)
GET    /users/me              # carries your own "budget" (null when unbudgeted)
```

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
- Nothing here restores a spent budget early except an admin raising the limit or
  clearing the budget.

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
