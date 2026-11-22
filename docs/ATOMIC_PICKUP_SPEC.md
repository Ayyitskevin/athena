# Atomic ready-work pickup

Status: R2 independently accepted, 2026-11-17. Implementation follows this contract.
Base: `d44a72e8e0a455e6f968d8e7f288d229245cc1e2`.
Accepting task source: Kevin requested “build atomic ready-work pickup next”, with
specification, independent review, implementation and merge after all checks pass.
This specification settles implementation choices within that requested increment.

## Problem and outcome

`AthenaClient.claim_next_ready` currently reads a ready lane, fetches a candidate's
root revision, claims it, and fetches work context. Individual claims are exclusive,
but readiness and the account's existing-possession precheck are separate snapshots.
Overlapping calls can acquire different issues for the same account; dependencies,
placement, status and returned metadata can change between selection and acquisition.
A committed claim followed by a failed context request also obscures the outcome.

Move selection and acquisition into one server-owned command. A caller chooses
“take the next ready task” rather than endorsing an issue revision it already read.
The server checks the current candidate under its write reservation and composes
the existing claim owner. Workers still read full context before doing work, and
explicitly resume open handoffs. Picking a task neither launches an agent nor
changes assignment, issue status, dependency state, or completion/approval policy.

## Ownership and invariants

- State remains in the existing issue, contributor, dependency, lease, handoff,
  activity and idempotency tables. No migration, separate queue, broker or cache.
- A new Aegis application command owns the selection transaction. Existing
  `lease_commands.claim_issue` remains the only lease acquisition/audit owner.
  REST and MCP are thin adapters. No auth/idempotency framework rewrite.
- Feedback is the structured response and existing `claimed` audit event,
  under the request's existing run identity. One acquisition means one event.
  Refused/no-work pickup adds no lease or claim event; existing authentication,
  idempotency and boundary-refusal accounting retains its ordinary meaning.
- The command starts `BEGIN IMMEDIATE` on a connection with no transaction, then
  samples its clock. A pre-existing transaction is explicitly refused before
  mutation rather than pretending a nested savepoint upgrades a deferred reader.
  Nested ordinary claim/context commands use their existing savepoints; no helper
  commits the outer transaction. All result construction occurs before commit.
- Competing writers serialize: a blocker/status/membership change committed before
  pickup's reservation is observed; a change after reservation cannot interleave
  selection and acquisition. Readiness is true at acquisition, not a promise that
  later changes cannot block the task. Lease expiry continues to use the existing
  clock/SQL boundaries; expired rows are retained and new acquisitions get new
  generations. A waiting pickup samples time after acquiring its reservation.

## Queue-specific concurrency policy

Before candidate selection, count this authenticated account's active leases
across all projects, including hidden, archived and done issues. If any exist,
return `already_holding` with the count only. Do not disclose hidden issue IDs,
titles, holders, or project details. Project selection does not narrow this check.

This policy applies only to atomic queue pickup. Ordinary `claim_issue` remains
able to acquire several tasks, and may deliberately acquire another after a queue
pickup. Thus simultaneous pickup calls on different credentials/runs of one account
can cause at most one new possession while that possession remains active; this is
not a global account quota. A later call after release/expiry is a new acquisition.
Do not infer worker/process identity from an account, run name or lease.

## Selection and compatibility

Use the existing ready-queue owner and predicate: actor-visible, non-archived,
to-do/doing according to the current `statuses.category_sql` query semantics,
without a live lease or any non-done incoming blocker, and claimable by the actor.
Hidden blockers still block without explicit blocker identifiers, metadata or
reason disclosure. Absence from a ready queue can permit inference; this is not
a guarantee against every side channel.
Keep the exact current blocker semantics, including archived blockers and custom
statuses. Do not silently change `category_sql` fallback behavior to `category_of`
as part of this feature; existing readiness/query surfaces remain consistent.

Amended 2026-11-22 by
[the awaiting-answer decision](decisions/2026-11-22-ready-rule-awaiting-decision.md):
the shared ready predicate also leaves out an issue whose newest native settlement
is a `needs_input` yield (for every account) or the caller's own completion (for
the caller), until a newer comment, status change, assignment change or claim by
another account that is not Automation. Pickup selects through that predicate, so
such an issue yields `nothing_ready` when it is the only candidate. Receipts,
replay, the account guard and direct `claim_issue` are unchanged.

Order remains delegated-to-me first, then descending priority, creation time and
issue ID (both ascending). `project_id` optionally restricts candidates to one visible existing
project. Omitted/null means all eligible projects plus backlog; it does not mean
backlog-only. Missing and hidden explicit projects return the same404, even when
the account is already holding work. Validation precedes selection. There is no
directory or broader project visibility grant.

Retain the existing `limit` argument (1–5, default1) as the maximum number of
ordered candidates attempted, never a number of leases acquired. The common path
acquires the first candidate. If ordinary claim refuses a candidate with the
existing `conflict` kind, its savepoint rolls
back and the next candidate may be tried within the bound. Return only that
visible candidate's ID and a generic `conflict` outcome, never another holder or
hidden path data. Other errors propagate and roll back the outer transaction.
If all attempted candidates conflict, return `candidates_conflicted`; do not call
it a selection race after a serialized selection. Record this replacement of the
old client-only `lost_race` outcome explicitly in consumer docs/tests. The current
issue-only pickup does not submit declared paths, so path-fence refusal is not a
normal reachable case. Conflict fallback is a bounded compatibility/composition
behavior; fault-injected coverage must be labelled as such, not a reproduced race.

## REST, Python and MCP contract

REST: `POST /issues/claim-next`, attached through the existing issue router.
Require the ordinary `issue_write_actor`/command writer gates; an issue-write-only
token can use pickup. Read-only or docs-only credentials cannot. Existing run
binding, pause, token revocation, budgets and visibility rules remain enforced.
This path is already covered by durable idempotency middleware's `/issues` root.

JSON body (empty object/default body permitted):

```json
{"limit": 1, "lease_seconds": null, "project_id": null}
```

Reject unknown fields, bool/non-integer numeric values, out-of-range limits,
non-positive/overflow project IDs, and lease duration outside the existing
lease owner's bounds. Null lease duration selects the existing default. Project
IDs are positive signed64 integers. No `generation`, paths or coordination-root
selector is introduced; pickup retains the existing helper's issue-only claim.
No caller `If-Match` is required: the server supplies the selected issue's current
strong root tag to the ordinary command inside the same transaction. Reject an
explicit `If-Match` on this route instead of silently accepting an unsupported
precondition; deliberate claims against a reviewed revision use `claim_issue`.

Python/MCP retain `claim_next_ready(limit=1, lease_seconds=None)` and add optional
`project_id` and `idempotency_key`. Python makes exactly one `_mutate` REST POST;
MCP uses existing strict aliases and the mutation decorator. Remove the client
selection loop and its now-orphaned private race mappings. Keep the tool's
`issue:write` scope and all profile membership/counts unchanged. No new MCP tool.

All successful responses are200 with `schema: athena.ready_claim.v1`, server
`observed_at`, `claimed`, and `attempts`. Set existing private/no-store lease
headers; do not set a top-level ETag. These are acquisition-time observations,
including `lease.active`; they may be historical on replay.

- Acquisition: `claimed:true`, selected issue metadata in the existing helper's
  shape, `lease` (existing generation/holder/expiry and full open handoff),
  `issue_etag`, `work_context_etag`, `previous_lease`, and compact `open_handoff`
  labels. Obtain metadata, current root/context tags and handoff state inside the
  acquisition transaction. Tags use existing owners, never reimplemented hashes.
  The context tag does not prove the caller received or processed that context.
- Existing possession: `claimed:false`, `reason:already_holding`, positive
  `held_by_you`, empty `attempts`.
- Empty eligible lane: `claimed:false`, `reason:nothing_ready`, `ready_total:0`,
  empty `attempts`. This does not enumerate reasons inaccessible work was omitted.
- Candidate conflicts: `claimed:false`, `reason:candidates_conflicted`, bounded
  `attempts` with visible issue IDs and generic `outcome:conflict`. No HTTP status
  codes in application-command errors/results. The adapter owns error translation.

Each attempted refusal must leave no lease/event; an unexpected in-command
payload/context construction failure before commit rolls back a successful nested
claim. Transport response-model validation/serialization and middleware publication
after command return cannot undo a committed claim; failures there retain the
existing indeterminate/reconciliation behavior. Current reads can
become stale immediately after return; callers inspect the existing lease/context
before renewal or work, using the exact returned generation for possession writes.

## Retry and crash semantics

Reuse the existing optional `Idempotency-Key` contract without a new receipt store.
For a retryable operation, choose a stable non-secret key before the first call
and reuse the same credential, exact body and run/lineage headers. Existing
middleware keys by credential, fingerprints the payload/headers, serializes an
identical keyed request and detects mismatches. Account-level concurrency safety
comes from the command, not from those credential-scoped keys.

Successful empty/already-holding/conflict outcomes are also cached. A new poll or
pickup intent uses a new key; replaying an old no-work result is not a fresh poll.
Receipts are historical: replay may return a now-expired/released generation,
unchanged expiry and acquisition-time `active:true`. It never renews or reacquires.
The existing HTTP replay header need not be surfaced by the Python client for
this rule to hold. Inspect current lease/full context before acting on a replay.

Retain authorization-revision fences, revoked/paused-token refusal, and run/header
fingerprint rules. A crash after domain commit but before response publication
remains indeterminate and fails closed; never silently execute again. Receipt
retention is finite (existing configurable TTL, default24h); after expiry a reused
key is a new request. Unkeyed calls are serialized pickup attempts but do not gain
durable replay guarantees. Document these boundaries rather than promising
unconditional exactly-once execution or permanent deduplication.

## Verification and delivery

1. Independently review this exact spec before application edits. Retain revision
   hashes and findings. Record the queue-only concurrency decision and deliberate
   `lost_race` replacement in a dated decision with this task as accepting source.
2. Retain a green focused baseline and behavioral RED demonstrating current
   same-account over-acquisition and/or stale-ready pickup, using isolated fixtures.
   A missing route/setup error alone is not behavioral RED. Port the relevant
   assertions to the server command; do not preserve obsolete client interception
   tests as if they still exercise the new implementation.
3. Positive and negative controls: normal/default/custom status and ordering parity;
   project/backlog selection; creator-only ineligibility; explicit hidden/missing
   project404; hidden/archived blockers; private member visibility; read-only and
   docs-only denial, issue-write-only success; strict input limits; no-work no-event;
   done/archived/hidden held leases block queue pickup; direct multi-claim still works.
4. Deterministic separate-connection WAL races: distinct accounts contend for one
   issue; same account/different runs and credentials contend for multiple issues;
   writer changes dependency/status/assignment/visibility before pickup obtains its
   lock; a waiting pickup observes lease expiry after lock acquisition. Check exact
   lease and claim-event counts, account-wide guard, current metadata and no stranded
   transactions. Reject caller-owned transactions without changing their prior work.
5. Retry tests: same key executes once (including concurrent callers), new key can
   make a new poll, same key/different body/project/run refuses, replay after release
   or expiry cannot reacquire, paused/revoked/membership-change replay refuses,
   restarted app replays, ambiguous finalization fails closed. Retain tests for
   existing receipt TTL boundaries; do not claim they changed here.
6. Generation and handoff controls: expired acquisition gets new generation;
   prior-holder labels and open handoff survive; completion of an active possession requires explicit
   resume (existing expired-possession cleanup stays unchanged); repeated pickup never renews; release/expiry permits a later new pickup.
   Inject claim-event/in-command payload failure and prove lease+event rollback; bounded
   candidate conflict/refusal cases retain honest outcome and private metadata.
7. Independently review implementation; run full default `scripts/gate.sh`.
   Run installed `athena-serve` lifecycle and actual changed HTTP route plus MCP
   against a fresh private synthetic database, with concurrency and replay cases.
   Retain commands, exit codes, source/import identity and cleanup receipts.
8. Search every consumer/test/generated inventory for the changed helper contract,
   update conductor/runtime guidance and generated API docs, rebase onto fetched
   main, push a draft PR, resolve every failed check, and merge at the exact green
   head. Verify hosted CI including container/release audit, CodeQL, and merged-main
   checks. Preserve all task worktrees and raw evidence. No live rollout.

## Deferred

Review freshness, teammate discovery, default tool-profile changes, skill-library
rewrites, live deployment, process supervision, assignment/dispatch automation,
path planning, global possession quotas and general idempotency redesign are
separate tasks. No new model call or UI is needed for this operation.
