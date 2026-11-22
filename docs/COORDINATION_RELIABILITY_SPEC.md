# Coordination reliability: review and implementation specification

Date: 2026-11-17. Review baseline: `857fced670f8a8336b3c0de866221e3269126801`.
Status: accepted for implementation after independent review on 2026-11-17.
Review corrections preserve backlog visibility and latest-renewal run attribution.

## Purpose

Make the operator and agent coordination views trustworthy at revocation,
expiry, concurrent writes and bounded-list edges. Preserve Athena's existing
ownership model and single-process SQLite architecture.

The review covered the architecture, roadmap, domain glossary, command
boundaries, REST/MCP retry plumbing, leases, completion/yield/resume, run state,
work context, desk/office/ready projections and reported usage. It combined
source inspection, existing tests and disposable-database counterexamples.
This is a focused review of the coordination loop, not an exhaustive audit of
Mentor, authentication, every migration or every integration.

## Architecture assessment

The core direction fits the product: commands own state changes and audit
events in one SQLite transaction; REST and browser adapters share them; MCP
uses REST. The append-only activity trail is evidence of recorded actions,
not independent proof that an agent's work or spend was verified.

State lives in SQLite (including lease generations, handoffs and activity),
with attachment bytes in the configured filesystem store. Feedback lives in
typed command refusals, activity and derived desk/run views. REST, MCP and
operator pages consume these shared readers, so a reader defect propagates
across transports. Timing matters at transaction snapshots, lease expiry,
client read-then-claim calls and exact retry boundaries.

Completion, yield, resume and heartbeat/usage fault-injection probes rolled
back the interrupted write and its dependent audit/state changes together.
Existing generation guards also prevent an expired possession from being
resurrected. These owners should be retained. A broad service split, new queue,
new state store or framework migration would add complexity without fixing
the demonstrated problems.

## Findings and required behavior

| ID | Priority | Reproduced baseline behavior | Required behavior |
| --- | --- | --- | --- |
| C1 | P1 | A former private-project member holding a lease sees a newly edited private issue title in `/office` and `/desk.office`, while the ordinary issue and work-context routes return 404. | Apply current project visibility before enriching a self-held lease with issue content. Retain honest self-lease accounting. |
| C2 | P2 | A writer between ready COUNT and SELECT produces total 1 with no items; a lease released mid-desk appears both held and ready. | Compose each desk, ready and office response from one read transaction and one captured lease clock. |
| C3 | P2 | 21 held issues with open handoffs return 20 handoffs with `clipped=false`. | Query visible open handoffs across all live leases held by the actor before applying the handoff limit; detect an omitted item explicitly. |
| C4 | P2 | 20 newer lapsed leases hide an older active lease in office; 21 active leases report a count of 20. | Select live leases before limiting, and compute the exact live total separately in the same snapshot. |
| C5 | P2 | Completing one run and reclaiming in another run within the same second changes the old run from complete to active. | Attribute a live possession to a run by the exact lease generation in its native claim/renew event, not a timestamp coincidence. |
| C6 | P2 | A model-only usage report and an explicit all-zero token report produce identical UI totals. | Preserve existing numeric sums; add per-dimension reporting coverage and render missing/partial coverage explicitly. |
| C7 | P2 | A renewal crosses expiry after the command's read; the SQL trigger correctly refuses it but its IntegrityError becomes HTTP 500. | Return the existing stale-generation conflict for this specific refusal, preserving rollback and every SQL guard. |

### C1: visibility without hiding ownership

In `aegis/office.py`, issue key, title, project id and dependency enrichment
must pass the existing `access.can_see_project_or_backlog` rule. Unprojected
backlog issues remain visible; this fix must not redact them. An actor may continue to
see its own lease id, generation, expiry and its own fence metadata after
access is revoked. Hidden issue enrichment is null/empty; the checkout hint
uses the numeric issue id rather than a hidden key. Do not re-grant access,
delete a lease, reveal hidden blocker metadata, or change role/scope policy.

The desk handoff query must apply that same current visibility gate before
returning advisory content. Test both membership revocation and a project
visibility change, with authorized and unprojected-backlog positive controls. Existing
private-project visibility is the policy; this work repairs a bypass.

### C2-C4: coherent, bounded read models

Use the existing nestable `db.transaction` helper for read snapshots; no
`BEGIN IMMEDIATE` for readers. Public `build_desk`, `build_ready` and
`build_office` own a snapshot when called alone and compose safely when nested.
Capture the clock once at the public boundary and pass it through nested
lease readers. A response may be stale immediately after it is returned,
but its lanes and totals must describe one observed state.

Amended on 2026-11-22 by
[A read that observes a lapsed lease records it](decisions/2026-11-22-lease-lapse-events.md)
(D11): the event feed and the attention ranking may record a lapsed lease in a
separate immediate transaction, outside their read snapshot. No reader holds the
writer lock while it reads.

Add one bounded owning reader for open handoffs on the actor's live, visible
claims. Join/filter first, order deterministically, fetch limit plus one,
then return at most the declared limit and an accurate clipped flag. Avoid
fetching an unbounded list of issue ids or reusing the separately capped
held-work list. No new endpoint or handoff schema is required.

Office uses the existing active-lease reader and active count. Its selected
chair is deterministic; `seated` is true only for exactly one live lease;
multiple live leases retain the existing warning. Lapsed rows remain visible
in their existing desk lane.

### C5: generation-bound run state

Use the canonical native `claimed` / `lease_renewed` activity detail format
already written by `issue_activity.record_issue_claimed`. Match a complete
generation token with its delimiter, not a substring or timestamp. Select the
newest native claim/renew event for the current holder, issue and generation
by activity id before comparing its run to the queried run. This preserves
latest-renewal attribution when a generation is renewed under another run,
including within the same second. Keep `imported_at IS NULL`; missing,
malformed, foreign-run or imported evidence must not prove ownership. No
history rewrite or schema migration is required. A genuine latest renewal in
the queried run still proves that it holds the current generation.

### C6: honest reported usage

`reported_spend._Totals` keeps the existing numeric sums and cost grouping.
Add `input_tokens_reports`, `output_tokens_reports` and
`cached_tokens_reports`: counts of reports containing each dimension.
Absent dimension means zero coverage; explicit numeric zero increases
coverage. Apply the shared result to day, run and issue summaries.

The admin template renders `not reported` when coverage is zero and labels
partial sums with coverage when fewer than all reports contain the dimension.
Full coverage renders the ordinary sum, including zero. Do not change usage
validation, cost arithmetic, currencies, budgets or enforcement. These remain
agent-reported figures, never metering or verified spend.

### C7: expiry refusal remains a domain result

Recognize only the existing reactivation-without-new-generation guard when
the command is in explicit renewal mode. Translate that known refusal to
`lease_generation_mismatch` and its existing 409 adapter mapping. Unrelated
integrity failures must propagate. Do not broaden an exception catch into
success, rotate a generation silently, extend expiry after it elapsed, or
write a renewal audit event when the renewal failed.

## Compatibility and deliberate exclusions

No database migration, new credential behavior, new authority, dependency
upgrade, service deployment or schema-history edit is included. Existing
response fields remain; coverage counters are additive. No new MCP tool is
needed because existing readers already flow through REST.

The full work-context ETag is an opaque validator over the internal
projection before optional null fields are serialized; existing tests pin
that compatibility. The issue ETag remains the root-write precondition and
the brief context tag hashes its wire body. A hash mismatch with serialized
optional nulls alone is not evidence of a stale-write defect.

Idempotency intentionally leaves an ambiguous committed/crashed operation
indeterminate rather than replaying the mutation. This spec preserves that
fail-closed behavior and the authentication-revision replay fence.

**Historical exclusion, superseded by [ATOMIC_PICKUP_SPEC.md](ATOMIC_PICKUP_SPEC.md):**
This coordination-reliability increment left `claim_next_ready` as a client
read-then-claim helper. Its review reproduced stale candidate metadata/status and
two overlapping same-account calls acquiring different issues. Direct claims
intentionally permitted several leases, so this increment deferred server-owned
pickup and its concurrency policy. The accepted atomic-pickup follow-up now owns
that separate change; it preserves ordinary direct-claim multiple possessions.

## Verification and delivery

1. Independently review this specification against the pinned source and
   reproductions before changing runtime code.
2. Add regression tests that fail on the baseline for each finding, with
   positive controls for permitted access, normal renewals, same-generation
   run attribution and explicit zero usage. Preserve the failing evidence.
3. Use deterministic two-connection WAL interleavings for snapshot tests and
   a controlled real-SQL expiry boundary for renewal. Never use live data.
4. Test nested read transactions, exact 20/21 boundaries, hidden handoffs,
   lapsed-plus-active office rows, and unchanged state/audit after refusal.
5. Run the full default `scripts/gate.sh` product profile. Run the installed
   `athena-serve` lifecycle and changed routes over real HTTP with a temporary
   database and attachment directory. Local tests do not substitute for the
   separately required hosted container and release-candidate audit checks.
6. Obtain non-author implementation review and resolve material findings.
   Deliver through a branch and PR under the repository's shipping rules;
   deployment and live-data changes are outside this task.

Success means the demonstrated failures are replaced by passing regression
tests, supported behaviors remain intact, the spec and implementation agree,
and every gate is reported as executed, deferred or blocked with evidence.
