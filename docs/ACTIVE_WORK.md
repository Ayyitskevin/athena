# Active Claimed Work

Athena's active-work projection answers a narrow operator question: **which agent
accounts currently hold issue leases, what durable evidence belongs to each exact
claim run, and which recorded facts need attention?** It joins existing state; it
does not create a second run table, poll processes, or mutate work.

## Operator workflow

1. Create or assign an issue and add the agent as an assignee or contributor.
2. Read the root issue and retain the REST response `ETag` header (exposed as
   `_etag` by the official client), or read Agent Work Context and retain the JSON
   body's `issue_etag`. The context packet's top-level `_etag` is a different
   validator and cannot guard a claim.
3. Give one logical execution a stable run id. MCP clients set a run with the normal
   run context; direct REST clients send `X-Athena-Run`.
4. Claim the issue under that same run id, passing the root issue tag as exactly one
   strong `If-Match` value (REST) or `if_match` argument (MCP). Acquisition and
   same-holder renewal use the same guarded, retry-safe lease protocol.
5. While work continues, send `heartbeat_agent_run(run_id)` (or
   `PUT /agent-runs/heartbeat`) and make audited writes under the same run id.
6. Inspect **Admin → Agent Mission Control** at `/admin/agents/runs`, call
   `GET /fleet/active-work`, or use the MCP tool `get_fleet_active_work`.
7. Inspect lineage or the replay artifact for a tagged claim run. Capture the
   artifact in the handoff when needed, transition the issue through its normal
   audited workflow, and complete the claim when the work is handed off or done.
   If the holder cannot responsibly continue, yield it with `needs_input`, `blocked`,
   or `capacity` and an optional note instead of letting the lease imply progress.

The projection is read-only. It never releases or transfers a lease, resumes or
revokes an account, approves an action, or changes issue status. Existing
human-approval boundaries remain authoritative for destructive, external,
production, and security-sensitive actions.

## Surfaces and bounds

The browser, REST response, and MCP tool use the same application projection:

```text
GET /fleet/active-work
GET /fleet/active-work?agent_id=42&limit=25
GET /admin/agents/runs?agent_id=42&limit=25
get_fleet_active_work(agent_id=42, limit=25)
```

The view requires an admin-role actor. A bearer token must also have the `admin`
scope. The REST and HTML responses are private and non-cacheable. The default limit
is 100 claims and the maximum is 200. `visible_total` and `clipped` say whether
the returned operational window is complete. The window is not paginated: active
leases sort before expired rows, then by expiry and issue id. Use issue activity or
run replay for older claim history rather than treating a clipped response as an
exhaustive audit. Each blocker preview contains at most five
admin-visible open blockers and carries its own exact count and clipping flag.

One SQLite read transaction pins leases, account controls, claim events, check-ins,
blockers, and replay readiness to one snapshot. Every lease and freshness comparison
uses the same server-owned `observed_at` instant.

## State and attention semantics

Each row keeps independent facts independent:

- `lease.claim_state` is `active` or `expired`, derived from server time.
- `run.run_id` comes from the newest native claim or renewal event for the current
  lease holder at or after the lease's `claimed_at`.
- `run.reporting_state` is `untagged`, `not_reported`,
  `reporting_recently`, or `stale` for that exact agent/run pair.
- Holder facts include current role, pause timestamp, current issue eligibility
  (assignee/contributor/admin policy plus current project visibility),
  and the count of live issue-write-capable bearer tokens. Token ids, names, hashes,
  scopes, and raw values are never returned.
- Issue facts include current status/category and archive timestamp so a retained
  claim on done or hidden-from-normal-pickup work cannot look ordinary.
- `open_blockers` is the current admin-visible blocker projection.
- `run.replay_ready` means tagged activity exists. Evidence links are suppressed
  when a valid run id cannot be safely addressed by today's path-parameter routes.

`attention_state=needs_attention` is set when any recorded reason applies:
`issue_archived`, `issue_done`, `holder_paused`, `holder_read_only`, `holder_ineligible`,
`no_live_issue_write_token`, `lease_expired`, `run_untagged`,
`checkin_missing`, `checkin_stale`, or `visible_open_blockers`.
`observed` means only that none of those known reasons applied at the snapshot.
It is intentionally not called healthy or running.

Summary counts carry `scope=returned_items`: active, expired, and attention totals
describe only the bounded `items` window, never undisplayed claims when `clipped=true`.

A missing live issue-write bearer token is credential posture, not proof that every
possible local authentication path is unavailable. Conversely, a fresh check-in or
active lease never proves an external process is alive, executing, unblocked, or
authorized for a separate gated action.

## Persistence, retries, and recovery

No new mutable state is introduced. Leases, activity, account controls, check-ins,
dependencies, and token revocation already survive restart in Athena's SQLite
database. Every acquisition and same-holder renewal requires exactly one strong root
issue validator. A missing precondition returns `428 precondition_required` without
disclosing a current tag and with `Cache-Control: no-store`; a wildcard, weak, empty,
multiple, or duplicate tag returns `400 invalid_if_match`; an oversized header
returns `431`; and one stale tag returns `412 precondition_failed` with the current
root issue tag. Authentication, visibility, claimant eligibility, lease-window, and
active competing-holder failures are resolved before this guard.

Repeating an active claim by the same holder with a current tag renews the single
lease row; an expired row is reacquired. Either path records a new claim-run event,
and the projection deterministically chooses the newest event id even when timestamps
tie. An exact retry with the same `Idempotency-Key` replays its original response.
After `412`, fetch a new root issue `_etag` or work-context `issue_etag` and submit a
new logical request with a new key. Repeating a heartbeat upserts the same agent/run
row.

`POST /issues/{id}/yield` and MCP `yield_claim` are available only to the current
active holder; admin status is not an override. The strict request reason is
`needs_input`, `blocked`, or `capacity`. An optional note is limited to 500 raw
characters, trimmed, and omitted when blank. Success atomically removes the lease
and appends a `claim_yielded` activity event under the ambient run context. It leaves
assignee, contributors, status, dependencies, and every other issue field unchanged,
and it never reassigns the issue. An absent or expired lease, or a non-holder caller,
returns `409`; exact idempotent retries replay the original result.

Expired lease rows remain visible until a new eligible claim replaces them or the
holder declines its delegation; completion and yield remove only an active lease.
Athena does not perform automatic takeover or reassignment after a stale check-in or
yield. After restart, clients resume by reusing the logical run id, refreshing the
heartbeat, fetching a current root issue tag, and reacquiring, yielding, or completing
the lease through the existing commands.

## Limitations

- An untagged claim cannot be correlated to a replay/check-in run.
- Heartbeats are cooperative reports, not process supervision.
- The token count covers bearer tokens only; it does not inventory browser sessions
  or an explicitly trusted actor-header development path.
- Current eligibility is assignee, contributor, or admin policy plus current project
  visibility. The projection flags either kind of drift but does not repair it.
- Only holders currently marked as agents are projected. A lease retained by a
  reclassified human remains an issue-level fact outside this fleet view.
- Repository/worktree ownership and conflict detection are not modeled here.
- Blockers and account controls are current facts; replay remains the historical
  event artifact.
- Reserved characters in run ids can make existing path-parameter replay/lineage
  routes unaddressable. Athena keeps the evidence fact but suppresses broken links.
- The projection does not infer completion from quiet activity and does not create
  approvals or handoffs automatically.
