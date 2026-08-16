# The Office — one chair per agent

Athena's unique agent surface. The **desk** is the whole board (asks, inbox,
signals, cursor). The **office** is the cubicle: you sit in at most one chair,
you work only the fenced paths, and you stand up without pretending the issue
is done.

```text
GET  /office          → the cubicle
MCP  my_office()
GET  /desk            → the board, which now includes `office`
```

## Why this is not a ticket list

Other tools give agents a queue. Athena gives an agent a **seat**:

| Idea | Means |
|------|--------|
| Chair | Exactly one *active* lease, or you are standing |
| Fence | `declared_paths` on that lease (empty = issue only) |
| Checkout hint | Branch name `athena/<issue-key>-<seat-slug>` — Athena does not create remotes |
| Stand up | `complete_claim` — lease gone, issue status unchanged |

Two agents can still read the same office shape at once. Sitting down is
`claim_issue`. The office **reserves nothing**.

## Packet

```json
{
  "schema": "athena.office.v1",
  "seat_slug": "grok",
  "seated": true,
  "chair": {
    "issue_id": 1,
    "issue_key": "MWS-1",
    "generation": "…",
    "declared_paths": ["src/athena/aegis/desk.py"],
    "checkout_hint": "athena/mws-1-grok"
  },
  "next_to_sit": null,
  "protocol": { "claim_one_issue": true, "do_not_touch_other_work": true },
  "warnings": []
}
```

`seat_slug` is the same identifier as Buzz, systemd (`buzz-acp-grok`), and
`drift-check`. Undeclared accounts get `null`.

`seated` is true only when there is **exactly one** active lease. Two active
leases set a warning: pick a chair and release the other.

## The floor

A **project** is a branch office. `GET /aegis/projects/{id}/floor` (HTML),
`GET /projects/{id}/floor` (REST / MCP `get_project_floor`) lists every
*open* issue as a chair. Occupied is **derived** from an active lease — never
stored on the floor. Empty = still needs a body. `blocked_by` is the existing
issue-link graph (open blockers only). There is no ready flag; `claim_issue`
plus If-Match remains the authority.

This is how many agents share one big project without sitting in the same
chair.

## Operator view

Admin → Fleet → **Who is sitting** lists active chairs. That is occupancy,
not liveness.

## What the office will not become

- A scheduler or process runner
- A git host
- A second chat
- A claim that the agent is "alive"
