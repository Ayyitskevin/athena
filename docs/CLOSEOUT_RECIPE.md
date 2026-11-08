# Closeout debt: inspect, then choose one deliberate action

## Optional reported completion

`complete_claim` / `POST /issues/{id}/complete` may carry `verdict`, `findings`,
`results`, `unfinished`, `uncertainty`, and `evidence` alongside the exact observed
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
