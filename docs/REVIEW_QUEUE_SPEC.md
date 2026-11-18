# Review queue tied to exact submitted versions

Date: 2026-11-17. Status: Accepted R2 after independent review on 2026-11-17.

Accepting task source: Kevin requested “build the review queue tied to exact versions next”, with specification, independent review, implementation and merge after all checks pass. Base: d11e57caa0c03e8c901ec7cb84a25de044fe3742. This specification concerns repository delivery, not live deployment or live-data migration.

## Problem and boundaries

The conductor currently matches free-form completion subjects, evidence and reported verdicts to the current build by hand. A reported PASS is historical and does not assert that a new candidate was reviewed. Add a durable, explicit relationship between an immutable submitted candidate and the native completion report that reviews it.

Reuse `complete_claim` as the atomic write boundary: finishing a builder's possession may submit a candidate; finishing a reviewer's separate possession may review one exact current candidate. Both are optional and mutually exclusive. Ordinary report JSON, issue status, approvals, dependencies, direct claims and claim handoff semantics retain their current meaning. A new bounded project read serves the conductor; full issue context supplies candidate IDs and bounded history to workers. Do not change the compact project briefing or attention/event tool contracts.

Declared repository/digest identity is not verification of external bytes, correctness, independence, readiness, acceptance, or permission to close/merge/deploy. No source fetch, artifact execution, process supervision, automatic reviewer dispatch, new permission role, generic workflow engine or UI is included. Distinct accounts are an attributable-account guard, not proof of independent people/models.

## State, feedback, coupling and timing

- State: two new canonical Aegis tables in the existing SQLite database. Candidate identity and review association are immutable. The existing native `claim_completed` activity event remains the single owner of each actor's completion report, actor/run attribution and observation time; rows reference it instead of copying verdicts or evidence.
- Feedback: typed completion receipts, source completion-event IDs, a bounded project review queue and full work-context history. Existing activity/history readers retain the report grammar. Read projections never acknowledge work or mutate domain state.
- Coupling: completion composes candidate/review persistence in the same shared command/transaction. SDK/MCP forward the extension in that one HTTP mutation. Existing auth, idempotency, run binding, generation, handoff and approval owners remain authoritative.
- Timing: acquire a clean connection's `BEGIN IMMEDIATE` before sampling the clock or checking extension authority, current candidate, project relationships or active possession. Extension calls reject an already-open caller transaction rather than pretending a deferred savepoint is a writer reservation. Ordinary completion composition remains unchanged when neither extension is supplied.

## Immutable identities and schema

`CandidateIdentity` has exactly:

```json
{"kind":"git","source":"owner/repository","revision":"<full lowercase commit hash>"}
```

`kind` is `git` or `artifact`. `source` is an inert UTF-8 string of 1–200 characters, trimmed/nonblank, with existing text/control and credential refusal. `revision` is 40 or 64 lowercase hexadecimal characters for `git`, or exactly 64 for an `artifact` SHA-256 digest. Mutable branch names/tags, abbreviated hashes, coercion and extra fields are refused. No URL/path normalization, lookup or inferred repository equivalence occurs; equality is exact declared identity after validation.

Migration 0082 adds `issue_review_candidates` and `issue_candidate_reviews` (final file name follows repo numbering). Minimum canonical fields:

| Table | Fields and constraints |
|---|---|
| `issue_review_candidates` | monotonic integer `id`; `issue_id`; unique `completion_event_id`; `kind`, `source`, `revision`; nullable `previous_candidate_id` referring to the previous submission on the same issue |
| `issue_candidate_reviews` | integer `id`; `candidate_id`; `review_issue_id`; unique `completion_event_id` |

Use existing foreign keys and shape checks; indexes support latest candidate per issue and latest review per candidate. Table rows cannot be updated or deleted. Insert triggers require matching native, complete-scope `claim_completed` activity on the declared issue; review links also require a different source/review issue and different recorded source/reviewer account. Native provenance cannot be forged through imported activity. Referenced completion activity is protected from UPDATE/DELETE, following migration0058's pattern, so later report mutation cannot reinterpret an existing review. Commands, not post-release SQL triggers, prove that the actor held the active generation before release. A privileged actor that rewrites the database/schema remains outside this guarantee; the activity hash chain does not independently cover every canonical-table byte.

Every accepted submission gets a fresh candidate ID, even when the identity equals a previous submission: A→B→A and A→A never revive an older PASS. `previous_candidate_id` is continuity/CAS state, not an acceptance chain. Candidate/current selection uses greatest submission ID across canonical rows before applying visibility or reading reports. There is no independently maintained mutable current pointer.

## Atomic completion extensions

Extend existing REST `POST /issues/{issue_id}/complete`, `AthenaClient.complete_claim` and MCP `complete_claim` with:

```json
{
  "generation":"<existing possession generation>",
  "candidate_submission": {
    "identity":{"kind":"git","source":"owner/repository","revision":"<hash>"},
    "expected_previous_candidate_id": null
  }
}
```

or `"review_candidate_id": <positive signed-64-bit integer>`. Neither supplied (including both null) is ordinary completion. Both non-null is invalid. The candidate object requires both keys; an explicit nested null means no previous candidate. Omission of that nested key, bool/noninteger/out-of-range IDs and extra keys are invalid. Preserve nested null through client/MCP serialization.

Both extension modes require the actor's own active lease with the exact generation on the route issue, after current ordinary visibility/eligibility/open-handoff checks. An admin cannot add an extension while releasing somebody else's lease; lapsed cleanup cannot create either record. These restrictions do not change ordinary admin/lapsed completion. Use the clock sampled after writer reservation to establish activity.

For a candidate submission:

1. Route issue must be non-archived and belong to a visible non-null project. Done status is allowed; submission is not a status assertion.
2. Read the latest canonical candidate before visibility filtering. If one exists, require visibility of its original native completion event before comparing any caller-supplied ID. An inaccessible/unavailable predecessor always produces the same generic409 as a CAS mismatch, independent of whether the supplied ID is correct, wrong or null; no lease release, row or audit write occurs. When visible, compare its ID (or null for no predecessor) with `expected_previous_candidate_id`. No latest ID/identity is disclosed by refusal. This protects the submission stream independently of the root issue ETag and prevents guessing a hidden historical ID after an issue move. Recovery requires an actor authorized for both current and recorded source scope to submit under its own active possession, or deliberate restoration of the necessary membership; there is no history reset or visibility bypass.
3. Normalize existing report normally. A missing/negative/unfinished report does not change the truth of submitting a candidate; no PASS is inferred.
4. Release the lease, record the existing native completion, insert the immutable candidate referencing that exact event, and construct the response before commit. No second client-side publication request.

For a review:

1. Resolve candidate and source issue; require visibility of the source issue and candidate's original native completion event as well as the route review issue. Missing/inaccessible candidate has the same404. Do not leak source/reviewer identity in refusals.
2. Both issues must be non-archived, in the same current non-null project, and distinct. The caller must differ from the candidate's recorded submitting account; no admin override. Source-issue claim eligibility is not required: the reviewer holds its own review issue, not the builder's slice.
3. Compare requested candidate ID with the newest canonical submission on its source issue under the writer lock. A stale reference is generic409 and leaves lease/report/domain/audit untouched. Review of an older candidate is not silently retargeted. Caller may refresh and actually review the new candidate, or use ordinary completion to report unfinished/historical work without a queue association.
4. Release the review lease, record its ordinary native completion, and insert one immutable review link referencing that exact event. The report may have any existing verdict or no verdict; absence becomes unknown rather than a synthesized PASS. Never retroactively attach an unrelated old completion event.

`record_claim_completed` may return its existing activity row rather than discard it; existing report detail encoding and parsers remain unchanged. No separate review verdict store or new activity verb is needed: the immutable relation belongs to the same audited completion, actor and transaction. New read projections connect that report to its canonical candidate.

Preserve ordinary completion response keys and bytes (no new null defaults). Extension success additionally returns `candidate_submission: {candidate_id, completion_event_id}` or `candidate_review: {review_id, candidate_id, completion_event_id}`. These receipts are historical and bounded. Source identity is obtained from guarded context/queue reads. If any validation, row/event insertion or response construction fails before commit, roll back release, rows and activity together. Postcommit serialization/publication uncertainty keeps existing idempotency-indeterminate/reconciliation behavior.

All extensions retain `issue:write`, pause/run ownership, original request-body/run fingerprints, credential-scoped optional idempotency and finite historical replay. Replaying a successful submission after B exists returns the historical receipt for A without reselecting A as current. Fresh work uses a fresh key; same key with changed version/run is a mismatch. No unconditional exactly-once claim.

## Reads, visibility and bounded output

New REST `GET /projects/{project_id}/review-queue?limit=5`, schema `athena.project_review_queue.v1`; MCP/SDK `get_project_review_queue(project_id, limit=5)`. Limit is strict1–20/default5; project IDs follow existing strict signed64 paths. Use existing optional-actor/read rules, missing/hidden project404, `Cache-Control: private, no-store`, authorization/actor Vary, and no top-level claim ETag.

Queue population: one latest canonical candidate per current visible, non-archived issue in the requested project, including done issues. First choose latest submission globally per issue, then require its source event's recorded scope and current issue visibility. If latest is inaccessible, omit that issue entirely; never fall back to an older visible candidate. No candidate means no row. Count and limit this visible population, sorted newest submission ID descending then issue ID ascending. SQL count cost may scale with database size; materialized rows, statements and report parsing must remain bounded for a fixed limit.

REST, SDK and MCP accept an optional `cursor`, an exclusive positive signed 64-bit candidate ID. The first call starts at the newest submission. The response carries `next_cursor`, the last returned candidate ID when another eligible row follows, or null at the end, including empty and exact-size final pages. The cursor is a numeric boundary and remains usable if its source moves or is superseded. Each call checks canonical latest submissions and current visibility in one fresh snapshot. A replacement above the cursor removes its predecessor below the cursor. Refresh from the top to see new submissions. A scan of an unchanged queue visits every visible candidate once.

Top-level packet: schema, `scope: visible_to_request_actor`, observed_at (existing UTC clock representation), bounded project metadata, `items`, `visible_total`, `returned`, `limit`, `clipped`, `next_cursor`, and explicit read-only/advisory semantics. `visible_total` counts the whole visible queue per call. `clipped` means this packet contains fewer items than that total and can be true on the final page. Use `next_cursor` to determine when traversal is complete. SQL materializes at most one lookahead row beyond the limit and enriches only returned rows. Each row includes:

- Bounded source-issue ID/key/title/status/category and work_context_url; labels/names use160-character excerpt shape (`text`, `total_chars`, `clipped`) from the project brief contract.
- Candidate ID, complete bounded identity, source completion event ID, reported_at and submitted_by ID/name.
- `review_state`: `awaiting_review`, `reported_pass`, `reported_hold`, or `unknown`.
- `latest_review`: null or bounded review ID, review issue ID/key/title/work_context_url, exact completion event ID, reported_at/by, report_available, reported_verdict and reported_findings. Never raw report/evidence/body, handoff tokens or instructions.

Select the newest canonical linked review by completion event ID before visibility or parsing. Expose it only if both current roots and both recorded native completion scopes are visible. None or inaccessible latest review has exactly the same `awaiting_review`/null shape; no hidden counts, IDs, labels, state-specific sorting or older-PASS fallback. A visible latest report with PASS/PASS-WITH-NOTES maps to `reported_pass`, HOLD/BLOCKED to `reported_hold`, and absent/malformed/missing-verdict report to `unknown`. A newer visible report without verdict never revives an older PASS. These labels describe reports, not acceptance or required actions.

Full `get_issue_work_context` conditionally adds `review_tracking` for a visible latest source candidate. It includes the same `current_candidate` summary plus `recent_candidates`, an independently bounded group of the newest5 visible candidate submissions and their guarded latest reviews, including the current candidate. Historical entries identify `is_current` against the already-visible canonical latest. If canonical latest is hidden, omit the entire section rather than expose older candidates as current. History keeps old candidate identities and their latest old reports inspectable within this bound; clipping remains explicit. All associations remain durable, but this first slice is not an exhaustive candidate/review-history browser: older submissions beyond five and earlier reviews of one submission are not all exposed by this projection. Source and review IDs are never substituted for root issue ETags. The existing context validator covers the resulting payload. Brief context remains unchanged and excludes this section, including its clipping effects: calculate brief warnings without review_tracking so a clipped new history cannot introduce a new brief context_clipped warning. Ordinary issues without visible candidate state remain byte-compatible.

Use one read snapshot and observation clock per request. Access changes, issue moves and native activity's original scope are honored at read time; a formerly same-project review is visible only when the reader can see both current roots and recorded scopes. Identity/status matching is not inferred from comments, report text, equal digests on another submission, or imported history. Existing activity/history remains the full source report trail beyond this bounded view.

## Migration, portability and rollback

One additive forward-only migration; no historical backfill or old migration edits. Existing issues/reports have no candidate association until an explicit new submission/review. SQLite in the pinned local Python3.12.13 runtime is3.53.1; re-record the actual test/CI runtime. Use established foreign-key-enabled connections, transactional migration ledger, standard CREATE TABLE/INDEX and abort-only triggers. Rehearse on populated synthetic data; validate references/indexes/immutability and migration-ledger checks. Do not claim non-blocking migration or production-volume performance.

Selective project/space portability v1 excludes these operational rows, like claim handoffs. Existing reports may travel as imported history, never recreate native candidates or review authority. Document the exclusion in OPERATIONS, glossary and recipe; test export/import with candidate history and verify imported queues remain empty. Whole-database recovery retains the canonical rows and associations.

Follow the repository's forward-only rollback contract: retain the old build and a pre-upgrade database/attachment snapshot. Test upgrading a populated synthetic baseline, new-code readiness, old-code refusal of the newer ledger, then old-code readiness against a separately restored pre-upgrade snapshot with baseline records preserved. New candidate history is absent from that old snapshot; do not call this a lossless downgrade. No live backup, migration, service restart or deployment is authorized by this task.

Official engine references: [transactions](https://www.sqlite.org/lang_transaction.html), [triggers](https://www.sqlite.org/lang_createtrigger.html), [foreign keys](https://www.sqlite.org/foreignkeys.html), [backup snapshots](https://www.sqlite.org/backup.html). Local tests establish actual behavior of this migration and packaged runtime; documentation is not a rehearsal result.

## Client, profiles and documentation

Existing complete_claim carries both typed strict optional extensions in one `_mutate` POST; do not add fallback or separate publication/binding requests. Candidate/response models are shared consistently with REST without importing transport into command owners. The queue is read-only and joins conductor plus full profile; seat/seat+knowledge tool counts remain unchanged because workers use full issue context and complete_claim. Expected conductor member36/read17, full139 before any unrelated upstream change; verify all scope/profile variants rather than assuming totals. Existing profile prefixes, scope filtering, run/idempotency forwarding and generated AGENT_API stay aligned.

Update conductor/closeout/boot/API/index/runtime/desk guidance only where behavior changes. Explain source build issue vs separate review issue, active own possession, explicit previous-candidate null/CAS, stale-review refusal, fresh A→A submissions, historical replay, queue/brief distinctions and source visibility. Add a dated decision and glossary terms while retaining completion/no-authority and approval-intent decisions.

## Acceptance and verification

1. Independent exact-spec review before application edits. Preserve review revisions/findings and accept the corrected contract first.
2. Focused baseline existing completion/context/idempotency tests. Treat missing review-queue capability as a feature gap, not a fabricated defect in the old advisory report behavior.
3. Command/HTTP tests prove candidate A→review PASS(A)→candidate B yields B awaiting; A/PASS remains historical. Repeat A→B→A and A→A; no inherited review. Missing-verdict newer review yields unknown, not old PASS.
4. Wrong generation, expired own/admin-other lease, open handoff, distinct-account/issue, null project, wrong project, malformed identity/CAS, source hidden/missing, revoked scope/pause/run conflicts all refuse with unchanged release/domain/audit state. Ordinary no-extension behavior remains compatible.
5. Separate-connection ordering tests: replacement first makes review(A) fail with lease retained; review(A) first records A then replacement B awaits. CAS competing submissions and duplicate idempotency create one accepted version; clock sampled after lock. Test historical replay and postcommit uncertainty.
6. Test current roots plus original scope after moves/revocation, hidden latest candidate and review, malformed/imported provenance, no old-visible-PASS fallback, strict bounds/clipping/no raw evidence, fixed-limit query/materialization bounds, and read-only behavior. Hidden predecessor CAS tests submit correct/wrong/null IDs and require identical generic refusals with no mutation, plus an authorized-current-and-recorded-scope positive control. Test that clipped review history does not change brief output or warnings. Trigger tests include valid positive controls and refused row/event mutation/deletion.
7. Test migration upgrade/restore rollback and selective portability boundaries. Run whole-tree consumer searches, architectural guards, profile/scope/MCP dispatch tests and generated-doc parity. Use real transport tests for MCP/schema behavior and label mocks honestly.
8. Independently review final application. Run full default scripts/gate.sh and installed scripts/smoke_app.py. Run a disposable installed real HTTP/MCP architect→builder→reviewer→conductor exercise with HOLD/rework/PASS, supersession, exact-key replay, a competing replacement, server stop/restart and fresh sessions preserving exact IDs/history. All fixtures local/synthetic; no live board/provider. Root reviews the independent harness before execution; owned processes, credentials and temporary runtime are cleaned.
9. Rebase on verified main, push draft PR, run/fix all hosted CI/container/release-candidate audit/CodeQL checks, merge only the checked/reviewed head, verify tree identity and postmerge checks, then file final report and shared handoff. No live activation follows from green delivery evidence.
