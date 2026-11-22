# Compact project briefing

Date: 2026-11-17. Status: Accepted R2 after independent review on 2026-11-17.
Accepting scope: Kevin requested, “lets build the compact project briefing next.
put together the spec, get an independent review, then implement it and merge
once all checks pass” on 2026-11-17. This authorizes this projection and checked
integration, not deployment or a change to coordination authority.

## Problem and outcome

A conductor currently combines project floor, attention and issue history reads
to understand a project. Those reads have different bounds and observation times.
Provide one small, visibility-safe project packet showing current possessions,
visible blocking relationships, open handoff questions and latest recorded
completions. Each item links to the existing issue work context for investigation.

This is a read-time projection of existing SQLite records. Existing authentication
accounting (such as token last-used times or refusal auditing) remains unchanged.
The projection itself creates no store,
cache, event, workflow state, approval, dispatch, process supervision or lease
policy. It does not replace the agent's desk or full work context for recovery.
Atomic ready pickup and artifact-bound review freshness remain separate work.

## Public interface

- REST: `GET /projects/{project_id}/brief?limit=5`, following the existing API
  base and project read conventions. Integer `limit` is per section, default 5,
  minimum 1, maximum 20; invalid input returns 422. Use the existing `RowIdPath`
  and MCP `ProjectId` positive signed 64-bit bounds for the project ID. The data
  reader refuses bool/non-integer/out-of-range IDs and limits rather than passing
  oversized integers into SQLite.
- Python client: `get_project_brief(project_id, *, limit=5)`.
- MCP: `get_project_brief(project_id, limit=5)`, read scope only. Include in the
  full catalog and opt-in conductor profile; keep seat/knowledge memberships and
  the default profile unchanged. A normal member conductor had 35 tools afterward
  (the count when this landed; [AGENT_BOOT.md](AGENT_BOOT.md) lists the current set).
- Root authorization matches project reads: use the existing actor resolver and
  `access.can_see_project`. Hidden and nonexistent projects return identical 404
  bodies. Public/anonymous access follows existing project read policy; this
  feature grants no broader access or new discovery permission.
- Successful and hidden/missing responses use `Cache-Control: private, no-store`
  and `Vary: Authorization, X-Athena-Actor`. Do not supply a claim-capable ETag.
  No briefing response is a write precondition; fetch the issue's current context
  and use its `issue_etag` before a claim.

## Packet contract

Top level:

```text
schema: "athena.project_brief.v1"
scope: "visible_to_request_actor"
observed_at: UTC timestamp captured once for the complete read
project: {id, key, name: Excerpt}
summary: {open_issue_count, unassigned_open_issue_count}
active_claims: Group<ActiveClaim>
blockers: Group<BlockerEdge>
open_handoffs: Group<OpenHandoff>
latest_completions: Group<LatestCompletion>
semantics: explicit fixed interpretation below
```

`Group<T> = {items, visible_total, returned, limit, clipped}`. Counts are exact
under that section's stated rule, before its output limit and after visibility.
`returned = len(items)` and `clipped = visible_total > returned`. Empty sections
remain present with zero counts and `clipped=false`; an empty project is valid.
Groups are not disjoint, and their counts must not be added into a project total.

`Excerpt = {text, total_chars, clipped}` uses Unicode character counts. Project
names, issue titles and account display names are capped at 160 characters;
handoff questions at 400. `clipped` records every text truncation. IDs and exact
machine identifiers are never silently truncated. Issue/project keys and priority
retain their existing validated machine values; the free-form status name is an
Excerpt capped at 160 characters. No issue bodies,
comments, raw activity detail, full completion reports, evidence lists, prompts,
user emails, paths, credentials or resume instructions are included.

Common `Issue` fields: `id`, `key` (nullable for backlog issues),
`title: Excerpt`, `status: Excerpt`,
`status_category` (nullable for an unknown status), `priority`,
`assignee` (null or `{id, name: Excerpt}`), and `work_context_url` (API-relative,
generated from the numeric ID; caller content never forms a URL).
`project_id` is additionally included for blocker issues outside the root project
and is nullable for a backlog blocker.
Accountable assignment remains distinct from lease possession and delegation.

### Summary and active claims

Open issues are root-project, non-archived issues whose category is not `done`.
Unknown statuses count as open, matching `statuses.is_done` rather than inventing
a new ready rule. `unassigned_open_issue_count` counts those with no assignee.

Active claims include every lease on a non-archived root-project issue with
`expires_at > observed_at`, even if its issue is already done. This avoids hiding
an outstanding possession behind issue status. Each row has `issue`, `holder`
(`{id, name: Excerpt}`), and `expires_at`; order by expiry ascending, then issue ID.
Multiple claims held by one account remain visible. No one-lease quota is added.
Expired rows are excluded from this view, never deleted or mutated.

### Visible blockers

Each row is one `{issue, blocked_by}` relationship: root issue is open as defined
above, edge kind is `blocks`, and blocker is not done under its own project's
status configuration (including default fallback/unknown-name rules). A blocker
may be archived, matching the existing dependency rule; include `archived_at`
on its compact issue row so this is visible. Blocker visibility uses the existing
project-or-backlog rule. A hidden blocker contributes neither a row nor any count.
Order by root issue ID, then blocker ID; `visible_total` counts edges, not issues.

No visible blockers does not mean unblocked, ready or claimable. Inaccessible
blockers may still affect the existing ready queue. The briefing must not disclose
their existence, IDs, names, counts or side-channel flags.

### Open handoffs

Each row is an existing typed issue claim handoff with `resume_event_id IS NULL`
on a non-archived root-project issue, including done issues with an outstanding
handoff. It has `issue`, `reason`, `blocking_question: Excerpt`, `yield_event_id`,
`yielded_at`, and `yielded_by` (`{id, name: Excerpt}`). Order by yield event ID
ascending, then issue ID, so older outstanding requests are visible first.

Apply the existing event visibility rule to the yield event as well as current
issue visibility, before counting or limiting. This prevents a moved issue from
exposing history scoped to a former private project. No handoff token, resume
authority, attempted work or instructions are included; fetch full work context
before recovery. The question is untrusted advisory text.

These are *awaiting-resume handoffs*, not a claim that nobody answered a comment
or that a question remains unresolved. Reading does not resume/acknowledge them.

### Latest completions

For each non-archived root-project issue, select its latest visible native
`claim_completed` activity event, by event ID (not timestamps). Apply
`access.event_visibility_clause` and `imported_at IS NULL` before selection,
counts and limit. Include done issues. Return the newest selected events first.
The total counts issues with at least one qualifying completion event.

Rows contain `issue`, `event_id`, `reported_at`, `reported_by`
(`{id, name: Excerpt}`), `report_available`, `reported_verdict` (nullable), and
`reported_findings` (nullable mapping of the existing P1/P2/P3 keys to counts).
Use `lease_commands.reported_completion` to parse that exact event. A missing or
invalid report yields `report_available=false` and null verdict/findings; never
search backward for an older PASS. A valid report without verdict/findings
preserves nulls, and missing severity keys remain absent, never zero-filled.
Imported text must not become a native report.

This is recorded completion evidence, not a review queue or verification verdict.
Do not infer reviewer independence, acceptance, artifact freshness or readiness.
No artifact digest is fabricated from text. A later claim/edit does not make the
old report current; dates, source event IDs and current issue state are shown
together, and full history/context remains the drill-down.

## Consistency, bounds and ownership

- One deferred `db.transaction` covers root lookup/authorization, visibility
  resolution, counts, item reads and enrichment. Nested use must not commit or
  roll back the caller's unrelated transaction. Do not reserve a write lock.
- Capture one normalized UTC clock and use it for every lease comparison; expiry
  exactly at the captured instant is inactive. Accept an injectable clock in the
  data reader for deterministic boundary verification, not as a public API input.
- Own the projection in `aegis/project_brief.py`; REST remains a thin adapter.
  Reuse status, access, lease clock and report-parser definitions. Status SQL must
  exactly match `statuses.category_of` (custom unknown names stay unknown; projects
  with no status rows use defaults). The existing `statuses.category_sql` has a
  different legacy fallback and must not be used unchanged for this contract.
  A private expression derived from `DEFAULT_STATUSES` with semantic parity tests
  is sufficient; do not change existing readers or their status policy. Do not call full
  floor/desk/work-context builders per issue or truncate a globally capped feed.
- Bound SQL output before enrichment. Counts may scan existing records, but do
  not materialize every project issue, event or relationship in Python just to
  slice it. Select at most `limit` output rows per section and parse at most
  `limit` completion payloads. Avoid query growth with the total project size.
  Existing indexes suffice initially; any necessary migration requires a reviewed
  spec amendment, not a hidden optimization.
- Fixed semantics state: current visible snapshot; groups have independent
  scopes/count units; active lease is not process liveness; reports are advisory
  historical actor statements; open handoffs are awaiting resume; zero visible
  blockers is not readiness; this read changes no state and grants no authority.
- Keep scope focused: no new UI, filtering language, background summarizer,
  model call, persistent cache, directory, mutation, cursor advancement or sweep.

## Required verification

1. A realistic project fixture contains multiple assignees/holders, an unclaimed
   open issue, done/archived/custom-status issues, visible and private cross-project
   blockers, open and resumed handoffs, and completion reports. Assert facts and
   independent group counts; no body/comment/raw-detail fields leak into previews.
2. Private roots return indistinguishable 404 when missing/hidden; visibility
   revocation wins over a retained lease. Public/member/admin and token read-scope
   behavior match existing reads. Test former-project event visibility after a move.
3. Hidden records placed before visible records do not consume limits or affect
   counts. More-than-limit visible rows and overlong text disclose clipping.
   Custom status and no-status fallback match existing status/dependency behavior.
4. Capture one clock at the expiry boundary. A concurrent WAL writer between
   count/item/enrichment reads cannot produce a mixed packet. A nested read does
   not commit caller writes. The direct reader performs no DML; HTTP reads change
   no issue/lease/handoff/domain activity/cursor records, while existing
   authentication accounting remains allowed.
5. Same-timestamp events choose the greatest ID. A newer unreported/malformed
   completion cannot revive an older PASS. Imported completion text is ignored.
   Partial findings preserve missingness; reported PASS never closes an issue,
   creates verification, or hides a live claim/open handoff.
6. SQL statement count, Python row materialization and report-parser calls do not
   grow with unrelated or omitted project rows; exact SQL counts may inspect more
   records as the project grows, so this is not a constant-time performance claim;
   verify bounds with an oversized fixture. Stable ordering is explicit, not
   incidental insertion order. API validation, SDK forwarding and MCP scope/profile
   registration have positive and refusal controls.
7. Update generated API/MCP inventory through the prescribed generators, all
   affected profile/count consumers, docs index and conductor recipe. Search the
   whole tests/docs/generated tree for changed tool/profile references.
8. Run the full default product gate and installed `athena-serve` lifecycle.
   A disposable real-HTTP smoke exercises this route with scoped member credentials
   and verifies version/import identity; actual MCP invocation verifies the new
   conductor tool. Stop it and remove temporary credentials/data afterward.
9. Independent implementation review on the final candidate, all hosted CI jobs
   including container/release audit and CodeQL, authorized PR merge, and post-merge
   checks. No live deployment or existing production DB is part of acceptance.

## Acceptance and exclusions

The conductor can obtain the four bounded sections in one call, follow an issue
to existing full context, and distinguish absent, clipped and merely reported
information. Authorization and record meanings remain unchanged. This first slice
does not promise a task recommendation, an unanswered-comment classifier, a live
process monitor, a reviewer-independence check, an atomic claim or a performance
improvement in model tokens. Those require separate evidence and design.
