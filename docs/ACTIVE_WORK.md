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
   strong `If-Match` value (REST) or `if_match` argument (MCP). Omit `generation`
   only for a free/expired acquisition; pass the current lease generation for an
   active same-holder renewal.
5. While work continues, send `heartbeat_agent_run(run_id)` (or
   `PUT /agent-runs/heartbeat`) and make audited writes under the same run id.
6. Inspect **Admin → Agent Mission Control** at `/admin/agents/runs`, call
   `GET /fleet/active-work`, or use the MCP tool `get_fleet_active_work`.
7. Inspect lineage or the replay artifact for a tagged claim run. Transition the
   issue through its normal audited workflow and complete the exact lease generation
   when done. If the holder cannot responsibly continue, yield it with structured
   attempted work, evidence, a blocking question, and resume instructions. A later
   claimant reviews the returned `open_claim_handoff` and explicitly resumes it
   before completing that new possession.

The projection is read-only. It never releases or transfers a lease, resumes or
revokes an account, approves an action, or changes issue status. Existing
human-approval boundaries remain authoritative for destructive, external,
production, and security-sensitive actions.

## Surfaces and bounds

The browser, REST response, and MCP tool use the same application projection:

```text
GET /fleet/active-work
GET /fleet/active-work?agent_id=42&limit=25
GET /fleet/active-work?attention_state=needs_attention
GET /admin/agents/runs?agent_id=42&attention_state=needs_attention
get_fleet_active_work(agent_id=42, limit=25, attention_state="needs_attention")
```

The view requires an admin-role actor. A bearer token must also have the `admin`
scope. The REST and HTML responses are private and non-cacheable. The default limit
is 100 claims and the maximum is 200. `visible_total` and `clipped` say whether
the returned operational window is complete. Use issue activity or run replay for
older claim history rather than treating a clipped response as an exhaustive audit.
Each blocker preview contains at most five admin-visible open blockers and carries
its own exact count and clipping flag.

**Attention-bearing rows come first.** The window used to sort active leases before
expired ones, which meant that on a busy fleet the expired — attention-bearing —
claims were exactly the rows the limit dropped, while a returned-items summary
could truthfully report "0 need attention" about a page it had filtered clean of
them. The window is still bounded, but it is now filled from the attention-prone
end: SQL orders the cheap signals (expired lease, archived or done issue, paused or
read-only holder, untagged run) first, and the exact per-row attention state — which
is decided in exactly one place, in the application projection — sorts the returned
page. `attention_state` filters that exact state, so a caller can ask for precisely
the rows needing a human.

`examined_count` reports how many rows the attention decision actually saw. Without
it, "0 need attention" is ambiguous between "none do" and "none of the ones we
looked at do", and on a clipped fleet those are very different statements. The SQL
ordering predicate is a bias, not a second definition of attention: it deliberately
does not reproduce the check-in, blocker, or token-count reasons, because two
implementations of the same predicate would eventually disagree.

One SQLite read transaction pins leases, open claim handoffs, account controls,
claim events, check-ins, blockers, and replay readiness to one snapshot. Every lease
and freshness comparison uses the same server-owned `observed_at` instant.

## State and attention semantics

Each row keeps independent facts independent:

- `lease.claim_state` is `active` or `expired`, derived from server time;
  `lease.generation` identifies only that exact possession.
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
- `open_claim_handoff` is the exact typed handoff awaiting acknowledgment, or
  `null`. Its text is untrusted advisory input and must never be auto-executed.
- `run.replay_ready` means tagged activity exists. Evidence links are suppressed
  when a valid run id cannot be safely addressed by today's path-parameter routes.

`attention_state=needs_attention` is set when any recorded reason applies:
`issue_archived`, `issue_done`, `holder_paused`, `holder_read_only`, `holder_ineligible`,
`no_live_issue_write_token`, `lease_expired`, `run_untagged`,
`checkin_missing`, `checkin_stale`, `visible_open_blockers`, or
`open_claim_handoff`.
`observed` means only that none of those known reasons applied at the snapshot.
It is intentionally not called healthy or running.

Summary counts carry `scope=returned_items`: active, expired, and attention totals
describe only the bounded `items` window, never undisplayed claims when `clipped=true`.

A missing live issue-write bearer token is credential posture, not proof that every
possible local authentication path is unavailable. Conversely, a fresh check-in or
active lease never proves an external process is alive, executing, unblocked, or
authorized for a separate gated action.

## Persistence, retries, and recovery

Leases, typed claim handoffs, activity, account controls, check-ins, dependencies,
and token revocation survive restart in Athena's SQLite database. Every acquisition
and same-holder renewal requires exactly one strong root issue validator. A missing
precondition returns `428 precondition_required` without disclosing a current tag;
malformed input returns `400 invalid_if_match`, oversized input returns `431`, and a
stale tag returns `412 precondition_failed` with the current root tag.

A free/expired acquisition omits `generation` and receives a fresh opaque value. An
active same-holder renewal must supply that exact value and preserves it. Supplying a
generation always selects renewal mode, so a delayed renewal can never acquire a new
possession. Yield, complete, active-held decline, and handoff resume also require the
exact current generation. Missing, malformed, and stale generation values return
stable `428 lease_generation_required`, `422 invalid_lease_generation`, and
`409 lease_generation_mismatch` responses without disclosing a replacement token.
Heartbeats remain generation-free because they report a run rather than mutate a
lease. Exact `Idempotency-Key` retries replay their original response without touching
a later generation.

`POST /issues/{id}/yield` and MCP `yield_claim` are holder-only. The reason is
`needs_input`, `blocked`, or `capacity`; attempted work, bounded evidence, a blocking
question, and resume instructions are required, while the bounded note is optional.
Success returns the new handoff with `201` after atomically recording the native yield
event, persisting the typed handoff, processing question mentions, and deleting only
the matching lease generation. Assignment, contributors, status, dependencies, and
all other issue state remain unchanged.

At most one handoff may await acknowledgment per issue. A later successful claim and
`GET /issues/{id}/lease` both return it as `open_claim_handoff`. The exact current
holder acknowledges it through
`POST /issues/{id}/claim-handoffs/{handoff_token}/resume` or MCP
`resume_claim_handoff`; this records receipt, not resolution, completion, or approval.
Completion returns `409` until acknowledgment. Decline may leave the handoff for a
later claimant. Another structured yield cannot replace an open handoff.

Handoff text is untrusted advisory context. Clients must inspect it and must not
auto-execute commands, fetch links, expose secrets, or infer approval from it. Selective
portability V1 deliberately excludes operational handoff rows; imported activity can
never create actionable handoffs. A full SQLite backup/restore preserves them.

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
  approvals or handoffs automatically; only explicit holder commands do so.
- Attention ordering fills a bounded window from the urgent end; it does not make
  the window unbounded. A fleet with more attention rows than the limit still
  clips, and says so with `clipped` and `examined_count`.
- Delegation still has an unavoidable pre-claim race because a generation does not
  exist before acquisition. The lease interlock, not a delegation preview, decides
  which claimant wins.
