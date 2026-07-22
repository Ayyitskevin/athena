# Fleet Throughput Metrics

Status: accepted for the first vertical slice (2026-07-22)

This document is the contract for Athena's first production throughput view. It is intentionally narrow: it answers how much visible issue work entered and completed during a bounded UTC period, who performed the completion events, and—for a full-visibility admin—how long trustworthy completion cycles took. It is not a backlog, productivity, utilization, or performance-ranking system.

## Decision

The metric source is Athena's append-only activity trail plus a versioned, typed lifecycle fact recorded in the same transaction as each new issue creation or status transition. Current mutable issue rows, project status configuration, assignments, and current `users.is_agent` values do not reinterpret historical events.

Existing activity cannot be backfilled honestly. Historical `changed_status` rows contain display text only, terminal categories are mutable, creation rows omit their initial category, and imported activity has foreign provenance. Those visible rows are excluded and reported as coverage, not guessed into the headline metrics.

The service is transport-neutral. REST, the server-rendered page, and MCP call the same service contract; adapters do not independently calculate metrics.

## Period and request contract

- `start` and `end` are strict `YYYY-MM-DD` UTC calendar dates.
- The interval is half open: `start 00:00:00` is included and `end 00:00:00` is excluded.
- Both bounds must be supplied together. The default is the last 30 UTC calendar days including today. Presets are 7, 30, and 90 days.
- `start < end`, and the interval may not exceed 90 days.
- Optional `project_id` and `actor_id` filters are positive ASCII-decimal SQLite IDs. Project filtering uses the fact's event-time destination project scope, not the issue's current project.
- `actor_limit` defaults to 25 and is bounded to 1–100. It limits displayed actor rows, never totals.
- Unknown criteria, repeated criteria, timestamps, offsets, signs, decimals, booleans, and oversized IDs fail with a validation error. A project outside the request actor's visibility is indistinguishable from a missing project.
- The service reads one database snapshot. Its visible evidence query is capped at 20,000 relevant events. It rejects an over-cap request with guidance to narrow the interval or filters instead of returning partial headline totals.

## Metric definitions

`created`
: Count of visible, native, typed issue-creation facts whose event timestamp is in the selected interval.

`completed`
: Count of visible typed lifecycle entries in the interval where the event-time category enters `done`. A transition from `done` to `done` is not another completion. Creation directly in `done` counts once as both created and completed.

`net_flow`
: `created - completed`. This describes event flow, not backlog change: a reopened issue is not a new creation, and a reclosed issue is another completion.

`median_cycle_seconds`
: Median elapsed seconds for trustworthy completion cycles completed in the interval, available only to a full-visibility admin. The first cycle starts at native typed creation. A reopen (`done` to a non-`done` category) starts the next cycle. A start before the selected interval is valid. Direct-terminal creation has a zero-second cycle. A completion still contributes to `completed` when its start is missing, but it is excluded from the median and reported in coverage. Member, viewer, and anonymous projections return `visibility_complete: false`, a null median, and zero samples rather than letting a hidden lifecycle row toggle timing or coverage.

`completion_actor_type`
: Counts completions by the event performer's event-time snapshot: `human`, `agent`, or `unknown`. This is not the creator, assignee, run owner, or lease claimant. A later actor reclassification cannot rewrite history.

`actors`
: A deterministic, bounded table of performers derived only from already-visible completion facts, ordered by completion count descending and then immutable actor ID. Current names are presentation only. A missing current user row is labeled `Unknown actor`; hidden-only actors never appear in labels, options, counts, truncation, or coverage.

Terminal status names are deliberately not returned. Terminal means the event-time status category equals `done`; private project vocabularies remain private.

## Lifecycle facts

Each supported new `created` or lifecycle-significant `changed_status` activity event has exactly one append-only fact containing:

- fact schema version and event kind;
- issue ID and the event's activity ID;
- before/after status values and before/after categories;
- before/after immutable project activity-scope keys, with `NULL` representing the backlog;
- the performer's `human`/`agent` snapshot.

The fact is written in the issue command's existing transaction. Database constraints tie its issue, kind, verb, one root, and linear predecessor/successor topology to the referenced activity event. Facts are not mutable summaries and are not populated for imported or legacy events.

## Reopen, moves, archive, deletion, and imports

- `done -> non-done` is a reopen; the next `non-done -> done` is another completion and cycle.
- When an atomic project move remaps the status or its category, the lifecycle fact records both project scopes and categories. Project-filter attribution uses the after-project scope for the resulting event.
- Archived issues remain included because archive is not a completion and their activity remains valid.
- Athena's supported commands do not hard-delete issues. The migration reserves every issue activity ID and all native and portability writers allocate monotonically, so manual deletion cannot rebind old events to a new generation. Non-admin viewers learn nothing about an orphan because the shared gate requires a live visible target. Full-visibility admins still count a valid typed orphan fact from immutable evidence; only an admin-visible orphan candidate without a trustworthy typed fact is excluded and reported in coverage.
- Imported activity is excluded. A later native typed completion can count, but without a trustworthy visible native start it has no cycle-time sample.
- `claim_completed`, run completion, assignment, lease release, comments, and issue updates are not issue completion events.

## Visibility and disclosure

Every evidence query, and every admin cycle-history query that runs, joins `activity` and embeds Athena's shared `event_visibility_clause` before filtering, grouping, limiting, or calculating coverage. The gate combines current target visibility with immutable event-time scopes. A private project the request actor cannot see must affect no returned value, including labels, empty states, coverage, truncation, or timing.

Flow and actor metrics are calculated from the request actor's visible typed events. Cycle timing is calculated only for admins, whose role has complete project visibility. Member, viewer, and anonymous projections return a constant unavailable cycle shape for their visible completions; they do not inspect predecessor visibility, so a private detour cannot toggle a timing or coverage bit. Actor options come from visible completion facts, never the global user directory.

Anonymous browser/API requests receive public-only facts. Authenticated members and viewers receive only their normal project scope. Admins receive their normal global scope and cycle timing. Read-scoped API tokens follow the token owner's role and visibility; insufficient scopes and presented-invalid credentials are rejected rather than downgraded to anonymous.

All metric responses are private and non-cacheable. REST varies on authorization/actor headers; the HTML page varies on the session cookie.

## Coverage and no-data behavior

The response separates metric values from evidence quality:

On a project filter, factless evidence is scoped through immutable activity visibility rows. Admin-visible pre-scope evidence that cannot be attributed is retained as restricted ambiguity, so filtering cannot silently turn incomplete history into a complete claim.

- visible candidate events;
- included typed events;
- visible legacy-untyped events;
- visible imported events;
- visible restricted/unattributable evidence on an admin project-filter view;
- visible malformed/unsupported facts;
- completions excluded from cycle time because their start is absent or the
  requesting role does not have complete project visibility;
- admin-visible orphan candidates without trustworthy typed facts;
- actor rows available and whether the presentation table was truncated.

Zero visible qualifying facts returns numeric zeroes, an empty actor table, and `median_cycle_seconds: null` with a zero sample count. `null` means no trustworthy admin sample or timing withheld for partial visibility; `cycle_time.visibility_complete` distinguishes those cases. It is never silently converted to zero. Hidden evidence cannot change whether the response says “no data.”

## Response and product surfaces

The versioned response (`athena.fleet_metrics.v1`) contains:

- exact period, timezone, inclusion convention, filters, and hard limits;
- created, completed, and net flow;
- cycle visibility availability, median, sample count, and excluded completions;
- human/agent/unknown completion totals;
- bounded per-actor rows;
- coverage and truncation fields;
- stable definition text suitable for REST, HTML, and MCP consumers.

The REST endpoint is `GET /fleet/metrics`. The HTML page is `GET /aegis/fleet-metrics`, linked from Mission Control and the agent cockpit. MCP exposes the same REST representation through Athena's existing client. The page uses accessible native HTML cards and tables; it adds no external analytics, JavaScript, or chart dependency.

## Query plan and bounds

Before this slice, an actual time-window issue-activity plan selected `idx_activity_target (target_kind=?)` and built a temporary B-tree for event ordering. It therefore walked the entire issue-event population rather than the bounded time range. The migration adds a narrow partial `(target_kind, created_at, id)` index whose predicate limits it to issue creation/status events. Large-fixture `EXPLAIN QUERY PLAN` regressions prove the evidence and history queries use their intended indexes without a temporary sort; adding a broader speculative index is out of scope.

Admin prior-cycle lookup starts from the covering `(issue_id, event_id)` fact index, primary-key joins activity, is restricted to completion issues and the selected end bound, and has its own fail-closed evidence cap. Partial-visibility views do not execute it. No endpoint performs an unbounded all-time activity scan.

## Non-claims and rollout limits

- Metrics begin accruing trustworthy typed evidence after this migration; older activity remains visible in coverage only.
- No historical backfill or imported-data inference is attempted.
- Net flow is not current backlog size or backlog delta.
- Completion counts are events, not unique issues or measures of individual productivity.
- Calendar grouping beyond one bounded interval, percentile distributions, snapshots, exports, and external analytics are deferred.
- A code-only rollback must retain the monotonic issue-ID allocator while migration 0055 remains applied; an older writer can collide with a reserved deleted ID. Full rollback therefore either keeps that compatibility slice and stops new fact writes, or restores a verified pre-0055 database backup with the old code. The additive fact and index structures otherwise require no live-data rewrite.
