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

The metered writes today are:

| Write | Command |
|---|---|
| Issue create | `aegis.issue_commands.create_issue` |
| Issue edit (title/body/status/priority/assignee/project/sprint placement) | `aegis.issue_commands.update_issue` |
| Page create | `mentor.page_commands.create_page` |
| Page edit | `mentor.page_commands.edit_page` |

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
