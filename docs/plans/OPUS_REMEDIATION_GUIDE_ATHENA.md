# Opus Remediation Guide — Athena

Produced from an adversarial review of the merged tree at `09d2ba1` (Stages A–P
complete). Method: seven independent reviewers — architecture/command ownership,
security, data integrity, test honesty, web-layer thinness, documentation
honesty, craft — followed by adversarial verification of every significant
finding. **All seven verified findings survived at high confidence; three were
reproduced by execution against a live app**, not by reading. Two further
findings were reproduced during synthesis spot-checks. Findings that were not
independently verified are labeled as such below and must be verified before
being fixed.

**Overall grade: 7/10.**

| Dimension | Score |
|---|---|
| Test quality and honesty | 9 |
| Architecture and command ownership | 8 |
| Data integrity and durability | 8 |
| Web layer thinness | 8 |
| Documentation honesty | 8 |
| Craft and maintainability | 8 |
| Security posture | 5 |

What earns the 8s and the 9: a coverage gate that mechanically forbids its own
narrowing, 2,518 tests with zero mocks against real SQLite behind the real app
factory, an AST-based transitive import checker that fails closed, a
checksum-bound migration runner, a single-writer activity table whose two
sanctioned writers are exactly the two claimed, and docs whose refusal codes
match the source line-for-line.

What caps it at 7: **two headline guarantees are false by execution, and both
live in the newest, proudest code.** "A forge cannot move an issue's status"
(FORGE.md, CHANGELOG, the Stage P PR) is falsified by a wildcard automation
rule firing on an imported event — reproduced: an imported `forge_commit` moved
an issue `open → in_progress` and minted a native comment. "A refusal reveals
nothing" is falsified by a one-byte probe — reproduced: a non-ASCII signature
header returns 500 for a registered source and 401 for an unknown one. Add a
token-scope bypass on the same surface (a read-scoped admin bearer token mints
event-source secrets; every parallel admin endpoint refuses) and the project's
cardinal rule — never claim a guarantee that is not implemented and tested — is
broken in the exact place a reviewer would check first. The fixes are days, not
months, and none require design work. That is why this is a 7 and not lower;
the reason it is not an 8 is that the gap sits between what the docs promise
and what executes, which is the one gap this project defines as unforgivable.

The review found the *doctrine* sound and the *enforcement* human. Every defect
below is an instance of a rule that exists, is written down, and was followed
everywhere except where it wasn't. The lasting fix (Wave H-3) is to make the
two load-bearing doctrines — command ownership and `imported_at` exclusion —
mechanically checked the way the import contract already is.

---

## Standing constraints (unchanged, non-negotiable)

- Never push `main`. Branch as `claude/<topic>`, PR, gate, merge.
- Every fix lands with the regression test that would have caught it.
- NEVER lower a coverage floor. If coverage fails, the gap is missing tests.
- Do NOT create or push a git tag. RELEASE_READINESS.md still says HOLD.
- Do not start Stage Q or R — those need the operator to ask. These waves are
  remediation, not expansion.
- MCP and REST reach the same command behavior; fixes apply to both transports.
- One wave = one PR, in order. H-0 does not wait on anything.

---

## Wave H-0 — verified security and integrity defects (stop-ship class)

### H-0.1 Forge source CRUD gates on admin role, not admin token scope
`src/athena/aegis/forge_api.py:49` — `_admin_or_403` calls only `is_admin`
(role). Every parallel admin surface (webhooks, security, approvals,
automation, activity, users) goes through `require_admin` →
`require_token_scope(ADMIN_SCOPE)` (`core/identity.py:268`). Reproduced: a
read-scoped bearer token for an admin got `201` + `evtsec_…` secret from
`POST /event-sources` while `POST /webhooks` correctly returned
`403 token scope required: admin`. This violates the headline invariant that
token scopes only narrow.

**Fix:** replace `_admin_or_403` with the standard `admin_actor` dependency on
all four event-source routes. **Test:** read-scoped admin token → 403 on all
four, mirroring the existing webhooks scope test.

### H-0.2 Non-ASCII signature header is a source-enumeration oracle
`src/athena/core/event_sources.py:121` — `hmac.compare_digest` raises
`TypeError` on non-ASCII str operands. Reproduced: `X-Hub-Signature-256:
sha256=\xff\xff` → HTTP 500 for a registered source (compare ran), clean 401
for an unknown one (short-circuited). One request per probe distinguishes
registered names — defeating the identical-401 property the endpoint documents
as its whole point.

**Fix:** in `verify_signature`, validate/encode the supplied header to ASCII
bytes before comparison (malformed → same mismatch path as a wrong signature);
compute a dummy HMAC when the source is unknown so both paths do comparable
work. **Test:** non-ASCII header byte → identical 401 body for known AND
unknown source. **Twin:** the same str-into-`compare_digest` footgun exists at
`web/csrf.py:42` (non-ASCII CSRF token → 500 instead of 403); fix and test it
in the same PR.

### H-0.3 Automation engine fires on imported history
`src/athena/aegis/automation.py:538` — `run_pass` reads
`activity.list_events` with **no `imported_at` filter**; `_matches` gates only
target kind/verb/conditions, and `trigger_verb='*'` is accepted. Reproduced
live: with only imported rows pending, three actions fired — an imported
`forge_commit` moved an issue `open → in_progress` (native `changed_status`,
attributed to Automation) and an on-created rule appended a native comment.
This falsifies "a forge cannot move an issue's status."

**Fix:** exclude imported rows from the automation event scan (filter in the
scan query or immediately after `list_events`; prefer SQL so the cursor never
even sees them). **Test:** reproduce the review's scenario — imported
`forge_commit` + wildcard rule fires nothing and moves nothing; identical
native event fires. This is the doctrine's most consequential mechanism and
currently the only one with no regression pin.

### H-0.4 Security counters and attention rollup count imported rows — and the docs claim otherwise
`src/athena/core/security_events.py:112-145` (`list_failures`,
`failure_counts`) and `src/athena/aegis/fleet_attention.py:80-85` (budget
exhaustions) filter by verb + `created_at` only. A hostile import bundle can
back-date free-text verbs (`login_failed`, `agent_budget_exhausted` pass the
import's verb bound verbatim) into the 24h window, planting fake boundary
refusals on `/admin/security` and inflating the operator's attention counts.
Meanwhile `docs/FORGE.md:104`, CHANGELOG, ROADMAP, and `forge.py`'s own
docstring claim the attention rollup filters `imported_at IS NULL` — **that
guard does not exist**.

**Fix:** add `imported_at IS NULL` to all three reads (and have
`list_failures` select `imported_at` so surfaces could label foreign rows if
ever needed). Correct every doc sentence that claimed the guard existed —
state plainly that it was added after review, in the CHANGELOG entry. **Test:**
plant imported rows carrying security verbs; assert zero contribution to
counts, list, and rollup.

### H-0.5 Browser label attach creates durable vocabulary before authorization
`src/athena/web/router.py:1400` — verified during synthesis: the handler runs
`labels.get_or_create_label` (which `conn.commit()`s the INSERT,
`core/labels.py:36`) **before** `issue_commands.attach_label` performs the
write-permission and visibility gates. A viewer-role user, or any signed-in
probe at a hidden or nonexistent issue, durably grows the shared label
vocabulary on a refused request. REST refuses the same write.

**Fix:** authorize first. Resolve the issue and the actor's write permission
through the command path before creating the label — cleanest is to move
find-or-create inside `attach_label` (one command, one transaction, one audit
event), keeping web and REST identical. **Test:** viewer POST and
hidden-issue POST both refuse AND leave `labels` unchanged.

---

## Wave H-1 — verified ownership and transaction defects

### H-1.1 Icarus callback: terminal-state guard reads outside the immediate transaction
`src/athena/aegis/dispatch_api.py:179,193,223` — the dispatch row is read
before `BEGIN IMMEDIATE`; the terminal-state check consults that stale
snapshot; `record_terminal` (`core/dispatch.py:260`) is an unconditional
UPDATE. Under multi-worker deployment (nowhere prohibited), a late conflicting
callback can flip a settled outcome — contradicting the endpoint's "accepted
and ignored" contract. This is the exact check-then-write race
`db.transaction(immediate=True)`'s own docstring exists to close.

**Fix:** move the read inside the transaction, and make `record_terminal`
predicate on non-terminal state (`UPDATE … WHERE status NOT IN (…)`), treating
a 0-row update as the accepted-and-ignored path. **Test:** two conflicting
terminal callbacks — first wins, second 2xx with no state change and no
duplicate terminal audit event.

### H-1.2 Saved filters have no command owner and an untransacted ownership check
`src/athena/aegis/saved_filters.py:245-303` — create/update/delete
self-commit, record no activity, and both transports
(`filters_api.py:175`, `web/filters.py:266`) duplicate a fetch-then-check
ownership gate outside any transaction; mutation SQL carries no `owner_id`, and
the rowid-reuse case can land a stale check on another user's row (update even
returns the victim's row).

**Fix (two parts):** (1) make every mutation owner-scoped in SQL
(`… WHERE id = ? AND owner_id = ?`, 0 rows → 404) inside one immediate
transaction — this closes the race regardless of doctrine. (2) Decide the
doctrine: either give personal state a command owner, or define an explicit
**"personal state" category** in COMMAND_MIGRATION.md (owner-scoped SQL
mandatory, no audit events, single data-module writer) and list saved filters,
watches, and notification read-marks under it. Do not leave the category
undocumented — that is how this class recurs.

### H-1.3 demo.py bypasses three command owners on a stale justification
`src/athena/demo.py:118-123` — the comment claiming project/space/page
commands are "migration debt" was true at #245 and retired by #262; the seeder
still calls bare `projects.create_project`, so the demo database — the
review-facing tour of a "load-bearing, not decorative" audit log — ships a
project with no `created_project` event.

**Fix:** route the seeder through `project_commands` / space / page command
owners; delete the stale comment. **Test:** seeded demo DB contains the
creation audit events.

### H-1.4 The only unauthenticated route has no rate limiter
`src/athena/aegis/forge_api.py:132` — every authenticated path charges the
token/anon limiter inside `current_actor`; `/forge/{source_name}` charges
nothing, so enumeration probes (H-0.2) and 512KB-body HMAC work are free.

**Fix:** charge the anonymous limiter (or a per-source-name limiter) before
reading the body. **Test:** burst → 429; a paused/unknown source burst also
429s rather than hammering the DB. Then add the limit to FORGE.md's "Limits,
stated" — its absence there is itself a doc-honesty finding.

---

## Wave H-2 — documentation reconciliation (honesty debt)

- **FORGE.md:** correct the guard table (H-0.4); add rate limiting to "Limits,
  stated" (H-1.4); resolve the deployment-shape contradiction — the documented
  github.com webhook setup requires public reachability while every other doc
  assumes tailnet-only; name the tunnel/reverse-proxy expectation explicitly.
  Soften `docs/FORGE.md:46`: event-source secrets are stored plaintext for
  HMAC (necessarily), API tokens are hashed — "same contract as an API token"
  overstates it.
- **RELEASE_READINESS.md:** four stages stale (pinned to Stage L: "135 source
  files", "68 migrations", "2,300 passed"; HEAD has 148 modules, 69
  migrations, 2,518 tests) while README calls it "the current baseline."
  Re-run the full release gate at HEAD, refresh the evidence, and add the
  Stage M–P risks — above all that the project now has its first
  unauthenticated public endpoint. The HOLD verdict stands; re-affirm it.
- **COMMAND_MIGRATION.md:** the self-described "review-facing source of truth"
  has no rows for saved filters, watches, read-marks, standalone label create
  (`POST /labels`, which also 500s on a duplicate-name race), or any Stage M–P
  surface. Add them; add the personal-state category (H-1.2); record the
  transport-side authorization debt for mentor pages/comments/event sources
  that its "None known" columns currently hide (unverified finding — verify
  the mentor claim before writing it down).
- **Small corrections:** UNDO.md says seven reversible verbs, code registers
  eleven; CHANGELOG's 0.1.0a1 section contradicts itself on status/assignee
  undo and approval kinds; AGENT_BUDGETS.md's metered-writes table omits
  dispatch and template/daily-note charges; ARCHITECTURE.md still marks
  "Phase 3 (current)" and collides with ROADMAP.md's numbering.

---

## Wave H-3 — make the doctrine self-enforcing

The review's meta-finding: every H-0/H-1 defect is a hand-enforced rule that
drifted. The import contract never drifts because a script fails the build.
Extend that treatment:

1. **`scripts/check_write_ownership.py`** (AST, same rigor as the import
   checker): transports (`web/*`, `*_api.py`, `mcp/*`) may not call
   data-module write functions or execute INSERT/UPDATE/DELETE directly;
   writers self-declare via the existing module conventions. Wire into CI
   beside the import check. This makes H-0.5/H-1.2/H-1.3 unrepeatable.
2. **`imported_at` guard check:** mechanically enumerate native-only activity
   readers (projections, rollups, counters, automation scans) and assert each
   filters `imported_at IS NULL` — a greppable allowlist check is acceptable;
   FORGE.md's guard table should be generated from or pinned to it. This makes
   H-0.3/H-0.4 unrepeatable.
3. **One command-error dialect.** Three incompatible shapes exist
   (`issue_commands` transport-neutral kind; `page_commands` runtime string
   kind; `event_source_commands` HTTP-coupled — the newest module copied the
   shape the doctrine argues against). Pick the transport-neutral kind, write
   the rule into AGENTS.md, migrate `event_source_commands` first, the rest
   opportunistically. (Unverified finding — confirm the three-dialect claim
   before migrating.)
4. **Test hygiene:** fix the always-true assertion (`tests/test_undo.py:281`);
   delete the false "SKIPPED when mcp extra missing" docstrings
   (`tests/test_mcp_client.py:8`) or implement the skip; add hardened-mode
   (`TRUST_ACTOR_HEADER=False`) refusal tests for the forge route; fix the
   UTC-midnight coupling in the daily-note tests; remove the dead constants in
   `forge_events.py:44` and `dispatch.OPEN_STATES`. Optional, operator's call:
   add `hypothesis` for the three parsers (`work_query`, `mentions`,
   `forge_events.extract_keys`) — the suite's only structural blind spot.

---

## What this review did NOT find

No invented UI data anywhere. No mock theater in the test suite. No coverage
laundering. No import-contract violation in 148 modules. No second writer to
the activity table beyond the two sanctioned ones. No `|safe` in any template.
The failures above are real and must be fixed before any release tag, but they
are failures of enforcement at the edges of an architecture whose core held up
under fourteen agents trying to break it. Fix the edges, then make the edges
check themselves.
