# Closeout debt: inspect, then choose one deliberate action

## Optional reported completion

`complete_claim` / `POST /issues/{id}/complete` may carry `verdict`, `findings`,
`usage`, `results`, `unfinished`, `uncertainty`, and `evidence` alongside the exact observed
`generation`. These fields record what the actor **reported**, not what Athena
verified. Omit them (or use null) for the existing release behavior and unchanged
response. Empty lists report an empty list; they do not mean every check passed.

```json
{
  "generation": "<exact observed generation>",
  "verdict": "PASS-WITH-NOTES",
  "findings": {"P1": 0, "P2": 1, "P3": 2},
  "results": [
    {"condition": "review original source", "state": "met", "note": "source read"},
    {"condition": "exercise real integration", "state": "not_assessed", "note": "unrun"}
  ],
  "unfinished": ["Integration rehearsal is still owed."],
  "uncertainty": ["The source read does not establish runtime behavior."],
  "evidence": [
    {"kind": "file", "source": "checkout@<commit>", "ref": "src/example.py:42", "scope": "source-only"},
    {"kind": "manifest", "source": "local synthetic fixture", "ref": "checks.json", "scope": "unrun"}
  ]
}
```

Each reported result has exactly `condition`, `state`, and `note`; state is
`met`, `not_met`, `not_assessed`, or `inconclusive`. There is no overall success
boolean or score. Up to 20 results are allowed, with a nonblank condition and
optional blank note, each at most 500 characters. `unfinished` and `uncertainty`
each accept up to 10 nonblank strings of at most 500 characters.

`verdict` is optional and is exactly one of `PASS`, `PASS-WITH-NOTES`, `HOLD`,
`BLOCKED` (upper case, as written; the words the fleet's review reports already
use, so a reader needs no translation). `findings` is optional: an object whose
keys are `P1`, `P2`, `P3` and whose values are non-negative integers. A key you
leave out means not stated, not zero, so report `0` when you mean it; an empty
object, an unknown key, a negative, fractional, string or boolean count is
refused, as is any other verdict word. Both are the reporter's own assertion:
Athena does not check them against the results, compute a score from them, or
let them change the issue, the lease release, an approval or a gate. A report
with only a verdict or only findings is valid, and a report with neither is
unchanged. They carry no free text, so the credential guard has nothing to scan
in them.

`not_assessed` means the condition was not evaluated; for example, an
integration check was never run. `inconclusive` means an evaluation was attempted
but did not establish `met` or `not_met`; for example, an integration check ran
but timed out before producing a result.

History and work-context summaries lead with the reported verdict and findings
when present ("reported verdict HOLD · reported findings P1 1, P2 0"), then show
counts of each reported state present, plus reported unfinished and uncertainty
counts. Every figure is labelled as reported; Athena itself states no overall
assessment. Reports open by default when a result is other than `met`, or unfinished
work or uncertainty is non-empty. A report containing only `met` stays collapsed;
its counts remain visible in the summary. The narrative repeats the same counts
as reported and advisory.

Evidence accepts up to 10 objects with exactly `kind`, `source`, `ref`, `scope`,
plus optionally `digest` (64 lowercase hex characters, SHA-256), `bytes` (a
non-negative integer) and `locator` (text of at most 500 characters, such as a
line range). Each result may also carry an optional `subject`: 1 to 10 short
texts naming what that result judged, so a verdict is bound to a subject.
Kinds are `event`, `handoff`, `doc`, `file`, `manifest`, `attachment`, or
`forge_event`. The last two point at a row Athena holds, and their `ref` is its
numeric id: an attachment, or an imported forge commit or pull-request event
(`docs/FORGE.md`).

Validation of all of these is shape only. The one thing Athena checks is an
`attachment` on the same issue that carries a `digest`: the history compares it
with the SHA-256 (and `bytes`, if given) Athena stored when it received those
bytes and says `matches stored attachment`, or `unverified reference` when it
differs, is missing, belongs to another issue, or is any other kind. A mismatch
is shown and never refused, because Athena verifies only what it holds; only the
issue's own attachments are consulted, so the check cannot confirm that a hidden
file exists. A report that uses none of these fields is shown exactly as before.

`source` qualifies where
the original record/bytes belong (for example an Athena instance or a checkout
and revision), at most 200 characters; `ref` identifies the original event,
handoff, document, file, or manifest, at most 1000 characters. Scope is explicitly
`source-only`, `synthetic`, `actual-run`, or `unrun`. Scope is the actor's report,
never a verified execution verdict. References are not resolved, fetched,
executed, or made into active URLs. Keep finite original bytes and any actual
verification outside this advisory record; a digest identifies bytes, not truth.

The complete compact JSON report is limited to 8000 characters. The evidence
reference list also uses the existing yield-evidence count, text/control, and
8000-character encoded-reference checks. Valid report strings are retained
exactly, including whitespace; unsupported controls are refused. New completion
text and new yield evidence also refuse high-confidence credential shapes before
writing. The credential refusal itself names only the field and credential kind
and never repeats the submitted text. A request that fails a different validation
first, such as an over-long or wrongly typed field, can have the submitted value
repeated back to the caller in that error. That refused input is neither stored
nor logged. Nothing is redacted: correct the text and retry. Existing stored
history is not scanned or rewritten. This is an accident guard, **not a security
boundary**: arbitrary passwords, unknown/changed formats and encoded credentials
can pass. Never submit credentials, tokens, private payloads, or secret-bearing
URLs. Use a safe reference instead. The credential refusal uses the existing
`invalid` error and leaves the lease and event unchanged. See the
[credential-refusal decision](decisions/2026-11-04-report-credential-refusal.md)
for the deliberately narrow write-only scope and limitations.

A reported `not_met` result or `unrun` reference still permits release under the
existing lease/generation rules. Reporting does not close an issue, change its
status, grant or consume approval, or satisfy a gate. A false PASS is never the
price of releasing a claim. Stale generations and open-handoff rules still
apply. Use the ordinary status command separately if appropriate.

The existing `claim_completed` event carries the report after its generation
prefix, under the versioned `reported_completion_v1` marker. With no report,
the detail remains exactly `generation <generation>`. There is no new verb,
table, migration, or status. Recent work-context items expose full validated
reports as `reported_completion` (null for legacy/non-report/imported rows),
even if their separate raw detail excerpt is clipped. Context still includes
only its 30 newest actor-visible activity rows and reports clipping. Issue
history labels these fields **reported** and escapes their text. Replay and
exports preserve the original detail; imported history cannot become a native
reported completion or actionable handoff merely by copying that text.

### Optional reported usage

`usage` records model work since your previous report, not cumulative totals.
Report each interval once: Athena sums the reports. This is **reported by the
agent, not metered by Athena**; it does not change the action budget, enforce a
spending limit, or verify a completion. Omit it or use null when not reporting.
The same shape is accepted by `heartbeat_agent_run`.

When present, it must be a non-empty object containing only these optional fields:

- `model`: 1–100 printable characters.
- `input_tokens`, `output_tokens`, `cached_tokens`: integers from 0 to 10^12;
  booleans are refused.
- `estimated_cost` and `currency`: supply both or neither. Cost is a finite number
  from 0 to 10^9, not a boolean; currency is an accepted uppercase ISO 4217 code
  such as `USD`.
- `reported_at`: an ISO 8601 timestamp with a UTC offset or `Z`, at most 40 characters.

```json
{
  "usage": {
    "model": "example-model",
    "input_tokens": 1200,
    "output_tokens": 300,
    "cached_tokens": 800,
    "estimated_cost": 0.0123,
    "currency": "USD",
    "reported_at": "2026-11-17T12:00:00Z"
  }
}
```

Missing fields mean not reported, not zero. The usage object is included in the
completion report's 8000-character limit. The write path also refuses recognized
credentials in `model`; this has the same accident-guard limitations described
above. Never submit secrets as model names.

Usage summaries retain the sum of reported values and include a per-dimension
report count (`input_tokens_reports`, `output_tokens_reports`, and
`cached_tokens_reports`). The operator page shows `not reported` when no report
contains a dimension and labels a partial sum when only some reports contain
it. Explicit zero counts as a reported value. These labels do not turn agent
reports into metering or change budget enforcement.

### What `complete_claim` tells you

Releasing a claim does not close the issue: `update_issue(issue_id, status=...)` (REST
`PATCH /issues/{id}`) is the separate, deliberate second write. The 200 body
repeats `issue_status` and `issue_still_open`, and `lapsed` says whether the row
you cleared had already expired. A lapsed row of your own is cleared under the
same rules; `my_desk()` lists its generation under `work.leases.lapsed`. While
you hold the issue actively, an open claim handoff answers 409 until this exact
leaseholder resumes it (`resume_claim_handoff`). Refs are inert data; never
submit credentials, secret URLs, or tokens. A `not_met` result or an `unrun`
reference does not block release.

## Deliberate closeout actions

Read `my_desk()` first. Keep active leases, lapsed residual rows and unread
notifications separate. The local-only `scripts/documentary_review/closeout.py`
renders an explicitly supplied desk snapshot and explicit `as_of` time; it does
not fetch, mutate, acknowledge or save state. There is no new endpoint.

| State kind | Age basis / pointer | Existing deliberate action |
|---|---|---|
| Active lease | `claimed_at` (latest acquisition/renewal, not first claim); `/issues/{id}/lease` | Re-read the lease; finish or deliberately yield your current generation. No inference of worker liveness. |
| Lapsed lease | `expires_at`; `/issues/{id}/lease` plus generation | Inspect before clearing with `complete_claim`. This releases a residual claim, NOT the issue. A newer generation must never be silently adopted. |
| Unread notification | `created_at`; `/notifications/{id}/read` | Read the specific item, then deliberately acknowledge that item. A bounded desk read does not authorize clearing unshown rows. |

Rows are active-first, then lapsed, then unread, preserving the supplied order
within each group. Ages are seconds as of the supplied offset-aware time; SQLite
timestamps are UTC. Unknown or future ages display null, not invented recency.
Displayed counts can be clipped; unshown counts remain visible. Inspect the
original desk for other asks and controls. A record being old is not a reason to
delete it or infer that its holder died. The snapshot is not a new status store.

Issue completion, lease release, notification acknowledgment, run-control replies,
and advancing the event cursor are FIVE distinct operations. Advance a cursor
only through events actually handled. No bulk acknowledgment, cleanup, takeover,
auto-close or "finish everything" action is performed or recommended here.

A successful `PATCH /issues/{id}` / `update_issue` transition from a non-done
category into a done category may include `held_lease` for the updating actor's
own lease row. It carries `generation`, `expires_at`, `active`, and an
informational message. `active=false` means a lapsed residual row, not active
possession. This describes the row at the successful transition; it can be stale
when read or replayed by an idempotency key. It is not authority or an instruction
to auto-complete. A human/agent must separately choose `complete_claim` with that
exact generation; the existing command rechecks it. Marking done changes no lease.
Other actors' rows, non-done updates, already-done updates and refused writes do
not receive the note. GET/list representations do not carry it.

PATCH's `ETag` (client `_etag`) continues to identify the canonical target issue
revision used by `If-Match`, equal to a subsequent unchanged GET's validator.
The transient advisory is excluded from that resource validator; the ETag is not
a hash of the entire advisory-bearing PATCH body. Existing null issue fields
remain present. Replayed responses retain the original observation and validator;
read the current resource before relying on present state. This keeps the two-write
contract intact and does not decide the separate OD-L2 close-and-release proposal.

The targeted MCP tool `mark_notification_read(notification_id, idempotency_key=...)`
is implemented through the official client and the owning REST operation
`POST /notifications/{id}/read` (`mcp/tools_read.py`, `mcp/client.py`,
`core/notifications_api.py`). Verify the recipient's available tool surface before
use; source availability is not proof of every installed runtime's adoption.
Proof: `tests/test_documentary_reader.py` uses the frozen three-state fixture
`scripts/documentary_review/fixtures/closeout.json`; input remains byte-equivalent
as data, pending control and cursor untouched. Operator pickup/time savings remain
unmeasured. This presentation does not diagnose why old rows remain.

## Exact-version submission and review

`complete_claim` can atomically associate one immutable candidate submission or
one exact-candidate review with the native completion event and lease release.
Both are optional; omit them or pass null for ordinary completion. They are
mutually exclusive when non-null. Neither changes issue status or approval policy.

A builder passes this additional argument on its **source build issue**:

```json
{
  "candidate_submission": {
    "identity": {
      "kind": "git",
      "source": "owner/repository",
      "revision": "0123456789abcdef0123456789abcdef01234567"
    },
    "expected_previous_candidate_id": null
  }
}
```

Include the ordinary issue ID and exact held generation too. Both nested keys
are required: null means the first submission; for replacement supply the current
candidate ID read from full work context. Each submission gets a fresh ID, even
for A→A or A→B→A; an older PASS never follows equal bytes to the new submission.
A predecessor mismatch or inaccessible predecessor returns HTTP 409 and leaves
the lease intact. Refresh the source issue's full work context. If the previous
submission is still hidden, stop retrying and tell the conductor. The operator
can restore the builder's access to the original project. Alternatively, yield
the claim so a builder with access to both projects can claim the issue, resume
the handoff, and submit using the visible predecessor ID.

Identity has exactly `kind`, `source`, and `revision`. `kind` is `git` or
`artifact`; source is inert, trimmed/nonblank UTF-8 text of 1–200 characters, with the
existing control/credential checks. Git revisions are full lowercase 40- or
64-hex hashes; artifact revisions are exactly 64 lowercase hex characters
(SHA-256). Mutable tags, abbreviated hashes, extra fields and coercion are refused.
Athena does not fetch sources or verify external bytes.

A reviewer holds a **different review issue** in the same current non-null
project, reads the source issue's full work context, actually reviews that exact
subject, then calls `complete_claim` on the review issue with
`review_candidate_id=<that candidate ID>` and its honest ordinary report. The
reviewer account must differ from the recorded submitting account. This is an
attribution guard, not proof of independent people or models. Both roots and the
original candidate event must be visible. A replaced candidate returns409 and
retains the review lease; it is never silently retargeted. Review the replacement,
or finish with an ordinary unassociated completion describing the unfinished or
historical work. HOLD, BLOCKED and missing verdicts remain valid reports.

Both modes require your **own active lease** and exact generation, current
eligibility, and any required handoff resume. Neither lapsed cleanup nor an admin
releasing someone else's lease can create an association. Source and review issues
must be non-archived and in the required project; done status itself is allowed.
Ordinary completion keeps its existing admin/lapsed behavior.

Success adds only `candidate_submission: {candidate_id, completion_event_id}` or
`candidate_review: {review_id, candidate_id, completion_event_id}` to the release
receipt. Retry an uncertain identical request with its original explicit
`idempotency_key` and run; changed work requires a fresh key. A replay can return
an older candidate receipt after a replacement exists: it does not make that
candidate current. Receipt retention is finite and uncertain postcommit outcomes
retain the existing fail-closed reconciliation contract.

Full source-issue context conditionally includes `review_tracking`, with the
current candidate and the newest five visible submissions with their guarded
latest reviews. The exact ID is
`review_tracking.current_candidate.candidate.candidate_id`; its declared subject
is `review_tracking.current_candidate.candidate.identity`. Bounds and clipping are explicit; brief context omits this
section. The conductor's `get_project_review_queue(project_id, limit=5)` shows
current visible candidates, including done issues, and their latest visible
reported state. Follow `next_cursor` as `cursor` until null to read older submissions.
`reported_pass` is advisory; `reported_hold` is HOLD/BLOCKED;
`unknown` has no usable reported verdict. `awaiting_review` also covers an
inaccessible latest review and exposes no hidden association. No older visible
PASS is substituted. The queue does not replace the compact project brief or
provide a claim ETag. See [REVIEW_QUEUE_SPEC.md](REVIEW_QUEUE_SPEC.md) for exact
visibility, history and transaction boundaries.

A requested review answers its request. On a review issue that carries a
request, only the named reviewer can complete with `review_candidate_id`, and
only for the requested candidate. Its report then shows on that request row,
in the queue and in the review issue's full context (`review_request`), as
`reported_verdict` with the report's `fulfillment_event_id`. An unrequested
review of the same candidate, by any account on another issue, moves
`review_state` and `latest_review` but never a request row. That is why the
conductor gates on the request row; see
[CONDUCTOR_RECIPE.md](CONDUCTOR_RECIPE.md#work-then-review-the-exact-artifact).

Selective portability exports omit these operational candidate/review rows;
imported completion history cannot recreate associations. Whole-database recovery
retains them. Review, acceptance, status changes and permission to merge/deploy
remain separate decisions.
