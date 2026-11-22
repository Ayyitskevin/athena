# Repository review — Athena at `99a0286`

Date: 2026-11-21. Reviewed tree: `main` at
`99a0286ba12c7b9250348a61819827ce943f01b9`.
Prior graded review: [OPUS_REMEDIATION_GUIDE_ATHENA.md](../OPUS_REMEDIATION_GUIDE_ATHENA.md),
which graded the tree at `09d2ba1` (2026-07-27) 7/10. It is called "July"
below. 450 commits separate the two trees.
Status: review evidence. Every recommendation here is a proposal until the
owner accepts it. The improvement plan that follows from this review is
[AGENTIC_USE_SPEC_ATHENA.md](../AGENTIC_USE_SPEC_ATHENA.md).

## Verdict

**As a repository: 7 / 10.** Unchanged from July, for different reasons.
**For agentic use: 6.5 / 10.** First measurement.

| Dimension | July | Now | What sets the score now |
|---|---|---|---|
| Test quality and honesty | 9 | 8 | Honesty intact: no skip or xfail, no `MagicMock`, a coverage gate that refuses its own narrowing. Loses a point to memory growth that stops the local gate (TEST-1), no shared fixtures (TEST-5) and three sleep-ordered races (TEST-6). |
| CI gates and supply chain | not scored | 7.5 | Actions pinned by commit and enforced by a test, a hash-locked toolchain, a fail-closed shard fan-in. Held down by an unhashed application graph and floating base image (TEST-10, both disclosed) and a publish safeguard that is not the real one (TEST-4). |
| Architecture and command ownership | 8 | 7.5 | July's lasting fix landed as three build-failing checkers. Against that: an audit event that commits before its write (ARCH-1), a checker that skips the two largest REST modules (ARCH-2) and an inventory that understates the remaining debt (ARCH-3). |
| Data integrity and durability | 8 | 8 | Durability, lease fencing and the migration ledger held under kill, race and tamper drills. Not raised, because of ARCH-1 and the commit default behind it (DATA-2). Not lowered, because a scan found three such call sites and only one is reachable, under a concurrent write. |
| Web layer thinness | 8 | 7 | Scored by the architecture reviewer. The web layer still has no write SQL. Three capability groups shipped cockpit-only (ARCH-5), and projects and sprints keep authorization in the transport, duplicated between web and REST (ARCH-3). |
| Documentation honesty | 8 | 7.5 | The core agent documents executed as written. Four guarantees did not (below), the README's next step returns 401 (DOCS-1), the guide warns agents off two merged features (LOOP-3) and the index omits five documents (DOCS-4). |
| Craft and maintainability | 8 | 7.5 | No TODO markers and 24 `noqa` in 79,113 lines. Hygiene slipped: no tag, 238 unmerged branches (DOCS-7), a prune ledger with no verdicts (DOCS-3), five paging vocabularies (CONTRIB-5). |
| Security posture | 5 | 7 | All five of July's stop-ship findings are closed. Two guarantees are false for an account with the right precondition (SEC-1, SEC-2). |

| Agentic-use dimension | Now | What sets the score |
|---|---|---|
| Agent API ergonomics (MCP and REST) | 6 | A deliberately designed core. Capped by MCP-1 to MCP-10: a retry that never applies, unbounded reads, a default config three times the size a seat needs, no single result or refusal shape. |
| Agent work loop, documented against executed | 7 | Every happy-path step in `AGENT_GUIDE.md` sections 1 to 8 returned its documented status on both transports (reviewer-executed: 267 REST calls and about 75 MCP calls). Held down by two loop defects (LOOP-1, LOOP-2) and drift in the two entry documents (LOOP-3, LOOP-4). |
| Multi-agent coordination design | 6.5 | Single-issue primitives are strong. The fleet layer is passive: lease expiry is silent (COORD-2) and its other limits are documented, not solved (COORD-3 to COORD-6). |
| Agent contributor experience | 6.5 | Contract scripts that report file, line and reason. Held down by a gate that does not fit the container (TEST-1), no supported step between lint and the full suite (CONTRIB-2) and no board of what is in flight (CONTRIB-3). |

The agentic grade is the mean of those four. One further score is a process
indicator and is not averaged: **quality of recent change, 8** (see "The
recent lanes").

**How the repository grade was set.** The seven dimensions scored both times
average 7.5, against 7.7 in July. July set its grade at 7, below its average,
because written guarantees were false by execution, which this project treats
as the one gap it will not accept. The same rule applies now, so the grade is
again 7. What changed is the composition. July's false guarantees were
stop-ship class and sat in security, which scored 5. Today's each need a
precondition, and security scores 7. Against that, five dimensions slipped by
half a point to a point while the source grew from 50,434 to 79,113 lines.

**What earns the grade.**

- All five stop-ship findings from July are closed. Three were re-probed live
  by the security reviewer; the lead confirmed the other two by reading the
  code and their regression tests.
- The lasting fix July asked for landed. Command ownership and `imported_at`
  exclusion are build-failing AST checkers beside the import contract.
- The suite doubled, from 2,518 tests to 5,216, with its honesty intact.
  A verifier confirmed there is no skip or xfail marker and no `MagicMock`
  anywhere.
- Hosted CI conserves the exact test inventory across four shards and refuses
  any test that did not pass all three phases. The lead read the shard runner
  and ran it end to end.
- A SIGKILL under keyed write load lost no acknowledged write
  (reviewer-executed).
- In the 213 commits since 2026-10-04, the coverage floors, the pragma
  allow-list and the checker exemption lists were not loosened, and no test
  file was deleted or renamed. 70 test lines were removed, 25 of them
  assertions; the lead read those 25, and each sits in a test that still
  exists and was rewritten in the same range.

**What caps it.** Four written guarantees are false by execution:

- `docs/APPROVALS.md` says a gated agent cannot approve itself. An agent
  account holding the admin role approved its own request, removed its own
  gate and deleted its own budget (SEC-1).
- Hidden resources are meant to read as missing. The link-mention routes tell
  a non-member which hidden page and issue ids exist (SEC-2).
- Project delete is documented as atomic with its audit event. A failed delete
  leaves a permanent, chain-verified `deleted_project` event for a project
  that still exists (ARCH-1, DATA-1).
- `docs/AGENT_GUIDE.md` tells an agent to wait for approval and retry the same
  write. Retried under the same idempotency key, the write never applies
  (MCP-1, DATA-3).

Each fix is small. The second cap is that the local product gate, Definition
of Done item 1, does not fit a 15 GB container at four workers, because the
suite pins every app instance in memory (TEST-1). The third is periphery:
inventories, hygiene and the web/API parity rule have not kept pace with the
growth.

**Why agentic use scores lower than the repository.** The primitives are right
and several are better than the norm: generation-fenced leases, a one-call
atomic pickup, scope and profile narrowing that matches its documentation
exactly, a bounded work-context packet with explicit clip markers, and
refusals on the lease path that name the next call. What is missing is anything
that executes the agent's side of the contract. Three loop defects were
reproduced: the guide's own retry instruction never applies an approved write
(MCP-1); the pickup call offers back an issue the agent completed and left
open (LOOP-1); a mistaken cursor value cannot be undone (LOOP-2). A single
list read returned about 50,000 tokens in the verifier's test and has no size
bound (MCP-2). The configuration Athena prints loads about three times the
tool text a seat needs (MCP-3). Refusals have no single shape (MCP-4). A
fleet's failure paths are passive (COORD-2).

## Method and confidence

Nine independent reviewers each took one lens: architecture and command
ownership (which also scored web-layer thinness), the MCP and REST surface,
the agent work loop, security, data integrity, tests and CI, documentation
and craft, multi-agent coordination, and contributor experience with an audit
of recent change. Each ran against its own throwaway `athena-demo` instance
and had to state how every finding was verified.

Every significant finding then went to a second, refute-by-default verifier.
It had to reproduce the finding by execution where it could, which was 48 of
58, and otherwise confirm it by reading the code, and in both cases record
what it ran. Finally the lead reviewer, the session that coordinated the
others and wrote this document, re-ran the headline findings on a private
instance. It is called "the lead" below.

| | Count |
|---|---|
| Findings reported | 118 |
| Significant (reviewer rated important or blocker) | 58 |
| Confirmed real by the verifier | 58 |
| Refuted | 0 |
| Reproduced by execution by the verifier | 48 |
| Confirmed by reading only | 10 |
| Downgraded to minor by the verifier | 28 |
| Marked as a documented, deliberate trade-off | 12 |
| Left at important or blocker after verification | 30 (29 important, 1 blocker), which are 25 distinct issues |
| Re-run by the lead | 7 (MCP-1, LOOP-1, LOOP-2, ARCH-1, SEC-1, SEC-2, TEST-1) |

The 60 minor findings and the 68 claimed strengths went through a lighter
fact-check. One minor finding was refuted (SEC-10: `/login` has a cross-site
Origin check the reviewer missed). Strengths that the fact-check did not
re-execute are marked "reviewer-executed" below.

Severity in this document is the verifier's, with one exception stated in the
ledger. Figures a verifier corrected are given in corrected form. Figures no
one re-measured are labelled as the reviewer's.

Both documents were then checked before commit by two citation checkers
(about 275 claims against the tree and the verification records) and one cold
reader. Their corrections are applied, including the overall grade: an earlier
draft said 7.5, which did not follow from the method stated above.

## Gate evidence

Static stages of `scripts/gate.sh --profile product` on `99a0286`, run in a
4-CPU, 15 GB container: dependencies, lint, format, typecheck, imports (239
modules), ownership, imported-at, styles and routes (398 routes) all pass.

The coverage stage did not complete. After about 16 minutes three xdist
workers had died, and the kernel log shows why: `Memory cgroup out of memory`,
with kills at 4.2 GB and 5.2 GB of anonymous memory. Pytest reported
`3 failed, 5213 passed` and 79.31% coverage, and the gate printed
`GATE FAILED: coverage` with no mention of memory. That result says nothing
about the product. See TEST-1.

The suite was then run the way hosted CI runs it, one shard after another:
`scripts/coverage_shards.py run 0` to `run 3`, then `combine`. All four shards
passed and the fan-in passed with 5,216 exact test IDs.

| | Measured | Floor |
|---|---|---|
| Line coverage | 93.80% | 92.60% |
| Branch coverage | 84.94% | 82.30% |
| Combined | 91.78% | 90.30% |

Each shard took about 3.7 minutes and peaked at about 6.5 GB across its four
workers, with the largest single process at 1.7 GB. The whole run took 15
minutes. So the tree is green at `99a0286`, and the suite fits this container
only when it is split.

Hosted CI on the same commit is green: CI run 981 (four coverage shards, the
fan-in and the audit job) and CodeQL run 372, both completed 2026-11-19.

## Findings

IDs are the reviewers'. Each entry gives the verified severity, how it was
verified, and where it lives. Where the companion specification carries a
requirement for it, the requirement is named in brackets.

### 1. Written guarantees that are false by execution

**SEC-1. An admin-role agent approves its own request, ungates itself and
clears its own budget.** Important. Reproduced by the verifier and by the lead.
`docs/APPROVALS.md:95-97` says "A gated agent cannot approve itself or ungate
itself." `src/athena/core/approval_commands.py:51` says authorization "(admin,
and human-only) is the caller's job", and the caller, `decide_for_actor` in
`src/athena/core/approvals_api.py:60-77`, checks only `require_admin`. Lead
run: the operator onboarded an agent with an admin-scope token, promoted it to
the admin role, gated `issue.close` and set a budget of 3 per day. The agent's
close returned 202 `approval_required`; its own `POST /approvals/2/decision`
returned 200 with `requested_by: 6` and `decided_by: 6`; the retried close
succeeded; `DELETE /approvals/policies/6/issue.close` returned `[]`; `DELETE
/users/6/budget` returned 204. The trail records the approval as an ordinary
decision. The precondition is an agent account that the operator has given
both the admin role and an admin-scope token. `docs/CONDUCTOR_RECIPE.md` tells
operators to give a conductor member credentials, so reaching this needs an
operator to go beyond the recipe. The sentence in `APPROVALS.md` is false for
such an account all the same. [Spec C1]

**SEC-2. Link-mention resolves its target with no visibility check.**
Important. Reproduced by the verifier and by the lead.
`mentions.mention_text` (`src/athena/core/mentions.py:168`) takes no actor, and
both callers use it unfiltered (`src/athena/mentor/api.py:906` and
`src/athena/aegis/api.py:660`). Lead run as a member agent outside a private
space and a private project: `GET /pages/3` and `GET /issues/6` returned 404,
the same as a missing id. `POST /pages/{own}/link-mention` for the hidden page
returned 409 "that mention is no longer in this page" where a missing id
returned 404 "no such link target". When the caller's body contained the
guessed title, the call returned 200 and rewrote it to
`[[axolotl vault page]]`; a guessed issue key became `[[VLP-1]]`. So any
writer can enumerate which page and issue ids exist in private containers and
confirm a guessed title or key. It does not print an unknown title. This is
the same class as July's H-0.2. [Spec C3]

**ARCH-1, DATA-1. Project delete commits its audit event before the delete.**
Important. Reproduced by two reviewers, two verifiers and the lead.
`project_activity.record_project_deleted`
(`src/athena/aegis/project_activity.py:56-68`) calls `activity.record` without
`commit=False`, so the event commits inside `delete_project`'s transaction
(`src/athena/aegis/project_commands.py:126-144`) before the row is removed.
When the delete then fails, the project still exists and the trail
permanently says it was deleted; `athena-doctor` certifies the chain. The REST
route's pre-checks hide this in the common case. The data reviewer reached it
over REST by racing `DELETE /projects/{id}` against issue creation: 5 of 24
attempts returned 500 and left a phantom event (reviewer-executed). The
docstring says "atomically", and `docs/COMMAND_MIGRATION.md:21` lists Projects
debt as "None known". One verifier kept this at blocker; this document rates
it important because it needs a concurrent write or a mid-delete failure.
[Spec C2]

**MCP-1, DATA-3. An approval-gated write retried under its idempotency key
replays a stale 202.** Important. Reproduced by two reviewers, two verifiers
and the lead. `src/athena/middleware.py:1021` stores every 2xx response as a
receipt, including 202 `approval_required`, and the receipt lives 24 hours.
After the operator approves, the same key returns the stored 202 with
`Idempotent-Replay: true` and the issue stays open; a new key closes it.
`docs/AGENT_GUIDE.md:225` instructs "Wait for the decision, then retry the
same write." The claim-next replays the data reviewer also listed are
documented, deliberate behaviour and are not part of this finding. [Spec A1]

### 2. Loop defects

**LOOP-1. `claim_next_ready` offers back the issue the caller just settled.**
Important. Reproduced by the verifier and by the lead. The ready rule
(`src/athena/aegis/ready_queue.py:54-80`) has no notion of work that is waiting
on a decision. Lead run with two ready issues: claim-next returned the
high-priority one; the agent completed it with a report; the next claim-next
returned the same issue ahead of the untouched one. The same happens after a
`needs_input` yield. The guide is clear that completing releases the lease and
that closing is a separate call. An agent whose close is gated, or that
leaves closing to the operator, is offered the same issue first, every time.
[Spec A2]

**LOOP-2. The desk cursor accepts an id beyond the newest event and cannot
move back.** Important. Reproduced by the verifier and by the lead.
`advance_cursor` (`src/athena/aegis/desk_commands.py:60`) bounds `after_id`
only by the SQLite integer maximum. Lead run: `POST /desk/cursor
{"after_id": 999999}` returned 200, a move back returned 409, and
`events_since_cursor` then read 0 after a new event. The documented loop never
sends such a value. One mistaken call does, and it blinds that seat's change
signal with no error and no way back. [Spec A3]

### 3. The product gate does not fit a 15 GB container at four workers

**TEST-1 (also CONTRIB-1 and the lead's first-hand finding). The suite pins
every FastAPI app in memory.** Important. Measured independently by the lead,
two reviewers and one verifier. Three module-level `functools.lru_cache`
wrappers in `fastapi.dependencies.models` hold each app through route-handler
closures that `create_app` defines (`src/athena/main.py:277-918`). Measured
retention per app-building test: 9.2 MB (lead), 8.3 MB (test reviewer), about
8 MB (verifier), 4.7 MB on a different pair of files (contributor reviewer).

The cache holds 4,096 entries. The test reviewer measured about 7 entries and
11 MB per app, so eviction starts near 585 apps, about 6 GB per process, and
`scripts/coverage.sh:41` runs four workers: about 24 GB by projection. The
gate then fails as described under Gate evidence. `.github/workflows/ci.yml:25`
records that monolithic coverage "exceeded 18 GiB locally"; the response on
2026-10-15 was to shard CI across four runners. The cause is not named
anywhere in the repository.

Clearing the three caches after each test took retention to 0.1 MB per test
(lead) and 0.48 MB (test reviewer, with a 43% shorter run). Whether that alone
brings the full gate under 15 GB has not been run. Production is unaffected:
it builds one app per process. [Spec B1]

**CONTRIB-2, TEST-2. There is no supported step between lint and the full
suite.** Minor after verification: the quick profile is documented as partial,
and `pytest -k` remains available. The quick profile runs no tests and no type
check, 339 test files sit in one flat directory, and only 83 of the 232
modules that are not `__init__.py` have a same-named test file. [Spec B2]

### 4. The agent interface

Verified on a member token unless stated. All are important after
verification except where marked.

- **MCP-2. List reads are bounded by row count, not size.** List tools return
  full bodies. The verifier measured 203,000 characters from one `list_pages`
  call over 12 pages (a `seat+knowledge` tool) and 161,000 from one
  `search_work` with a single large issue (a `seat` tool). [Spec A6]
- **MCP-3. Every configuration Athena prints selects the full profile.**
  `claude_mcp_config` (`src/athena/mcp/config.py:14-27`) sets no
  `ATHENA_MCP_PROFILE`. Measured over stdio: 114 tools and 102,836 bytes of
  schema, against 26 tools and 32,793 bytes for `seat`. The printed command is
  a bare `athena-mcp`, which is not on PATH when the documented install is
  followed without activating the venv (DOCS-6, minor). [Spec A7]
- **MCP-4, LOOP-7. Refusals have no single shape.** At least three body shapes
  coexist over REST, and 401, 403, 404 and most 409 refusals carry no machine
  code, including "issue is claimed by …". The reviewers' counts of four and
  eight shapes were not re-measured. [Spec A5]
- **MCP-5, LOOP-5. Results have no single shape.** 0 of 114 tools carry
  annotations, 8 declare an output schema, and none returns structured
  content. A list arrives as one unlabelled block per row and an empty list as
  zero blocks with `isError` false. [Spec A4]
- **MCP-6. The seat profile's own start text names tools the profile hides.**
  `my_desk` (`src/athena/mcp/tools_admin.py:211`) tells the agent to drain
  `recent_events`; `search_work` points at `query_help`. Neither is in `seat`.
  The text never names `claim_next_ready`, which is. [Spec A8]
- **MCP-7 (LOOP-6 is the same observation, rated minor). A write the server
  did not apply returns as a success.** A 202 `approval_required` reaches the
  model with `isError` false, and no seat write tool mentions the outcome.
  [Spec A5]
- **MCP-8. Closed sets are typed as free strings.** `priority`, `relation`,
  `decision`, `kind` and `verdict` are plain strings on the older tools. Some
  typos return an empty 200 rather than an error, for example
  `GET /search?kind=bogus`. [Spec A9]
- **MCP-9. No tool lists a project's statuses**, and the 422 for a wrong
  status does not name them. The tool text does name the three defaults, so
  this bites on projects with custom statuses. [Spec A9]
- **MCP-10. `complete_claim` is the most expensive tool and its report shapes
  are not in its schema.** About 4,700 characters of description and schema;
  the required keys of `results` and `evidence` are discovered one 422 at a
  time. Validation sits in the command by an accepted decision
  (`docs/decisions/2026-11-04-report-credential-refusal.md`); the gap is that
  the shapes are not published and violations are not reported together.
  [Spec A9]
- **SEC-3. Text written by one agent reaches another with no data marking.**
  Minor after verification, as a hardening gap. `advisory_untrusted` exists
  only on handoff, delegation and fleet-work shapes. Comments, bodies, pages
  and learnings carry none, and the server instructions state no rule.
  [Spec A10]
- **DATA-4, DATA-5 (minor).** The MCP client drops the `Idempotent-Replay`
  header, and an indeterminate key has no agent-facing read. [Spec A11]

### 5. Fleet coordination

- **COORD-2. Lease expiry is silent.** Important. Reproduced by the verifier.
  No activity event and no notification is written when a lease lapses. The
  `lease_expired` attention signal reaches only the holder and admins, because
  `fleet_attention` filters to the caller's own claims for non-admins. A
  conductor therefore has nothing to wake it when a worker vanishes; that
  consequence follows from the two facts above and was not run end to end.
  The limit is documented in `docs/CONDUCTOR_RECIPE.md`. The verifier kept it
  at important, unlike the other documented limits below, because the only
  workarounds are polling or an admin token. [Spec C4]
- **COORD-1. An unrequested PASS replaces a requested HOLD in the review
  queue's headline state.** Minor after verification, because the design is
  documented as advisory. Reproduced: the named reviewer reported HOLD; an
  account that was never asked then completed an unrelated issue with
  `review_candidate_id` and PASS, and `review_state` read `reported_pass`.
  The logic is in `src/athena/aegis/review_queue.py`. [Spec C5]
- **COORD-3, COORD-4, COORD-5, COORD-6 (minor, documented trade-offs).**
  Without a root catalog any two path-fenced leases conflict, and the 409 names
  no reason; declaring no paths bypasses fences; budgets do not meter
  comments, claims or leases; heartbeats without progress are derived as
  `pinging` and never raised as attention. Each is disclosed in the docs and
  each is a gap a fleet will feel. [Spec C6, C7]
- **COORD-8, COORD-9 (minor).** Pausing an agent leaves its lease held. The
  reviewer counted about 37 HTTP calls and 12 threaded identifiers for one
  reviewed change (not re-run). [Spec C8, C9]
- **COORD-7 (minor, a documented limit).** A delegated contributor can add a
  second account that resubmits, then review the result. The recipe already
  says distinct account names do not prove independent review. The
  specification carries no requirement for it.

### 6. Enforcement and inventory gaps

- **ARCH-2. The write-ownership checker does not treat the two largest REST
  modules as transports.** Important. `_is_transport`
  (`scripts/check_write_ownership.py:202-207`) matches `web`, `mcp` and
  `*_api`. `aegis/api.py`, `mentor/api.py` and `main.py` do not match. A raw
  `DELETE FROM activity` added to `aegis/api.py` passed the gate in a scratch
  copy. The real tree is clean when `aegis/api.py` and `mentor/api.py` are
  counted, so the gap is latent. `main.py` was not tested under the stricter
  rule. [Spec B5]
- **DATA-2. `activity.record` defaults to `commit=True`, and nothing checks
  for a committing callee inside a transaction.** Important. An AST scan by
  the reviewer found three such call sites, all in `project_commands.py`; one
  is ARCH-1. Composition is also broken: a command nested in an outer
  transaction commits the outer write and then fails with "no such savepoint"
  (reproduced by the verifier). [Spec B5]
- **ARCH-3. `docs/COMMAND_MIGRATION.md` understates the remaining debt.**
  Important, confirmed by reading. Confirmed for projects (6 writers) and
  sprints (5): both take a bare `actor_id` with authorization left at the
  transport, while the table says "None known". The reviewer's total of 35
  writers in 10 modules was not re-run. [Spec B6a]
- **ARCH-5. Three capability groups are reachable only with a browser
  session.** Important. Assign-to-seat with radio, the fleet roster and
  reported-spend rollups have no REST or MCP twin: an admin-scope bearer token
  gets 401 or 404. That contradicts steering rule 1 in `docs/VISION.md`.
  Rooms are also browser-only (ARCH-4, minor, since rooms are cosmetic).
  [Spec C10]
- **TEST-4. `publish.yml` and `RELEASING.md` say the publish job cannot run
  until its GitHub Environments exist.** Important, confirmed by reading and
  not executed here. GitHub's documented behaviour is to create a missing
  environment, unprotected, on first reference. Nothing is exposed today
  because no PyPI trusted publisher is registered. The stated safeguard is not
  the real one. The fix is to correct both passages and to create the `pypi`
  environment with required reviewers before a publisher is registered. It is
  outside the specification's scope.

### 7. Documentation drift

- **DOCS-1. `docs/RUNTIME_RECIPE.md` steps 1 and 2 authenticate with
  `X-Athena-Actor`.** Important. Reproduced. Both commands return 401 on
  `athena-demo`, and `athena-serve` refuses to start with the header trusted.
  `README.md:60` sends readers to this page. `tests/test_runtime_recipe.py`
  checks that the routes exist, not that the request succeeds.
  [Spec A15a, A15b]
- **LOOP-3. `docs/AGENT_GUIDE.md:298-305` lists two features under "Pending
  in PR" and says "Do not rely on them until they merge."** Important. Both
  merged, as #433 and #436. [Spec A15a]
- **DOCS-4, CONTRIB-4. `docs/INDEX.md` is headed "one line per document" and
  omits five documents and the decisions directory.** Important. Missing:
  `CLOSEOUT_RECIPE.md`, `GLOSSARY.md`, `COORDINATION_RELIABILITY_SPEC.md`,
  `RUNBOOK_LINK_IDENTITY_SPEC.md`, `DOCUMENTARY_READER.md` and
  `docs/decisions/`. 31 relative links are dead, 29 of them under `plans/`
  and `research/`. [Spec B6a]
- **Minor.** The boot block names `get_issue_lease(ref)` and
  `mark_notification_read(id)`; the real arguments are `issue_id` and
  `notification_id` (LOOP-4). Neither entry document links the generated
  tool-to-REST map, `AGENT_API.md` (LOOP-8). Four documents give three sizes
  for the conductor profile; it is 37 (DOCS-8). `docs/OPERATIONS.md` omits 13
  of 48 `ATHENA_*` variables, six of which appear in no document (DOCS-2).
  The glossary omits the workspace vocabulary that appears in tool names
  (DOCS-5). The startup log recommends `ATHENA_COOKIE_SECURE=1`, which
  `athena-serve` refuses (DOCS-9).

### 8. Process and hygiene

"The window" below is the 213 commits since `62d6ba5` (2026-10-04). It holds
51 merged PRs: 28 on the codex lane, 19 on the claude lane and 4 from
dependabot.

- **CONTRIB-3 (minor).** Nothing tells an agent what is in flight. There is no
  CODEOWNERS file and no claim convention. `claude/mcp-slim-a` (1,528 added
  lines, PR #438 opened 2026-11-08) never merged; its content landed nine days
  later through a reconcile PR on the other lane (#449). PR #438 is still open
  and conflicted. `claude/trace-context` (PR #442) was opened the same day,
  is still open, and nothing of it is on main.
- **COORD-10 (minor).** 13 of 804 commits name an Athena issue key, the last on
  2026-08-29. The fleet that builds Athena does not coordinate through it.
- **DOCS-7 (minor, documented stance).** No tag has ever been cut. The version
  has read `0.1.0a1` across about 800 commits, above an Unreleased changelog
  section of about 1,300 lines. Of 320 remote branches besides `main`, 238 are
  not ancestors of `main`; 216 of those were last committed in June or July.
- **DOCS-3 (minor).** The prune ledger's header counts are stale and all 20
  verdicts are unset. Its own schedule puts the next review in 2026-Q4, which
  is now. Nothing has been cut since it was written: routes went from 362 to
  398 and tools from 124 to 140. [Spec B6b]
- **CONTRIB-5 (minor).** Paging uses five parameter vocabularies and agent
  packets use two timestamp formats, including two schemas merged on the same
  day. No conventions page exists. [Spec A13]
- **CONTRIB-9, CONTRIB-10, CONTRIB-12, CONTRIB-6 (minor).** Decision records
  accompany 10 of 28 codex-lane PRs by the verifier's count (the reviewer
  counted 11) and none of 19 claude-lane PRs. `CHANGELOG.md` was not touched
  by any of the 51 merged PRs. Three append-only registries are conflict
  hotspots: 16 "merge main into branch" commits in the window. The PR template
  still asks for a pre-gate checklist. [Spec B8, B10]
- **TEST-5, TEST-6 with CONTRIB-8, TEST-7 (minor).** `_bootstrap` is
  redeclared in 95 test files and there is no shared client fixture.
  `pyproject.toml:49` claims the suite has no `time.sleep`;
  `tests/test_event_wait.py` has three. Coverage floors are described as a
  ratchet and have not moved since 2026-07-26; measured coverage now sits 1.2
  to 2.6 points above them. [Spec B9]
- **TEST-3 (minor, disclosed).** `opencode.yml` runs an agent with
  `id-token: write` and an API secret. Only the owner can trigger it; the
  content it then reads is not the owner's.

### Remaining minor findings

Confirmed by the fact-check unless marked.

| ID | Finding | Where |
|---|---|---|
| ARCH-6, MCP-11 | `/openapi.json` (393 operations, about 600 KB) mixes browser form routes with the API and barely describes the write protocol | `src/athena/main.py` |
| ARCH-7 | Error-dialect drift the AGENTS.md authority grep cannot see | `AGENTS.md:83` |
| ARCH-8 | Approval-request, budget and scope-denial writes are owned by exception handlers in the composition root | `src/athena/main.py:517` |
| ARCH-9 | The write-ownership checker inspects call expressions only; an alias or `functools.partial` evades it | `scripts/check_write_ownership.py:678` |
| ARCH-10, ARCH-11 | Module lanes are enforced for imports, not for SQL or import-free reaches | `scripts/check_import_contracts.py:755` |
| ARCH-12 | 20 of 33 command modules cannot be imported without FastAPI (reviewer's count) | `src/athena/core/identity.py` |
| ARCH-13 | Wiring lives in a few very long functions; `create_app` is 642 lines | `src/athena/main.py:277` |
| ARCH-14 | The field-guide seeder creates its label through the data module, skipping the command and its event | `src/athena/guide.py:122` |
| ARCH-15, DOCS-12 | The AGENTS.md module table omits `mcp/` and `field_guide/`, and its orientation tree also omits `workflows/`; there are no change recipes | `AGENTS.md:109` |
| MCP-12 | Pagination and truncation vocabulary differs on almost every tool | `src/athena/mcp/tools_read.py` |
| MCP-13 | Roughly 17 to 18% of tool-list bytes are indentation, title keys and nullable boilerplate | `src/athena/mcp/server.py` |
| MCP-14, DATA-10 | Renewing a held lease needs a fresh issue ETag as well as the generation; the server's hint omits that | `src/athena/aegis/claims_api.py:405` |
| MCP-15 | `heartbeat_agent_run` makes the model supply a run id the session knows, and accepts a wrong one | `src/athena/mcp/tools_read.py:612` |
| MCP-16, MCP-17 | Search grammar atoms missing from the description; argument names for the same thing vary | `src/athena/mcp/tools_read.py:124` |
| MCP-18 | stdio only; the handshake reports the SDK's version as the server's and advertises empty prompts and resources | `src/athena/mcp/server.py` |
| LOOP-9, SEC-11 | `POST /issues/{id}/claim` and `POST /tokens` silently ignore unknown fields; a token request naming another user mints one for the caller (the lead saw this) | `src/athena/core/tokens_api.py:39` |
| LOOP-10 | A lease does not stop another eligible account closing the issue, and no document says so | `docs/AGENT_GUIDE.md` |
| LOOP-11 | Desk lease progress always reports `last_checkin_at` and `last_lease_activity_at` as null | `src/athena/aegis/desk.py` |
| LOOP-12, LOOP-13 | Exact-version submission errors are one opaque 422; OpenAPI declares no error responses | `src/athena/aegis/review_commands.py:46` |
| LOOP-14, LOOP-18 | The demo seeds one agent token and no ready work; the boot document's wiring proof fails against that token | `src/athena/demo.py:444` |
| LOOP-15 | MCP responses are pretty-printed and errors repeat their message | `src/athena/mcp/server.py` |
| LOOP-16 | No seat profile includes `open_my_notebook`, which the desk advertises | `docs/AGENT_BOOT.md` |
| LOOP-17 | An `X-Athena-Run` longer than 200 characters is silently truncated, while the heartbeat rejects the same id | `src/athena/core/run_context.py` |
| DOCS-10 | Ruff selects five rule families; mypy is not strict; 103 functions exceed 80 lines (reviewer's count) | `pyproject.toml:132` |
| DOCS-11 | One desk response mixes two timestamp formats; `asks.review_requests` is absent rather than empty (the lead saw both) | `src/athena/aegis/desk.py:236` |
| CONTRIB-7 | AGENTS.md defers permission rules to a machine-level file that cloud containers do not have; no `.claude/` directory | `AGENTS.md:7` |
| CONTRIB-11 | `generate_agent_api.py --help` rewrites the tracked doc | `scripts/generate_agent_api.py:292` |
| TEST-8, TEST-9 | No source-to-test map; property testing is one file with 12 `@given` | `tests/test_parser_properties.py` |
| TEST-10, TEST-11 | The application graph and base image are pinned by version, not hash (disclosed); `publish.yml` interpolates an input into a shell script | `.github/workflows/publish.yml:94` |
| SEC-4 | Anonymous reads default on, and tailnet mode does not require an explicit choice (documented default) | `src/athena/config.py:331` |
| SEC-5, SEC-6 | A zero budget does not stop comments; the credential guard misses Athena's own `whsec_` and `evtsec_` secrets and is not applied to comments (both documented as narrow) | `src/athena/core/report_credentials.py:27` |
| SEC-7 | A revoked token writes one trail row per presentation with no bound at default settings | `src/athena/core/identity.py:52` |
| SEC-8 | `athena-mcp` starts with all 140 tools when its token is rejected, instead of failing | `src/athena/mcp/server.py:559` |
| SEC-9, SEC-12 | Key uniqueness reveals private container keys; HMAC secrets are stored in plaintext, necessarily, and backups are not flagged as containing them | `src/athena/core/event_sources.py:131` |
| DATA-6 | Import drops run lineage by design; the runbook and the import result do not say so | `src/athena/core/portability.py:1125` |
| DATA-7, DATA-8, DATA-9 | Doctor messages do not name the offending row or object; `PRAGMA synchronous` is inherited from the build; a handler that leaves a transaction open is rolled back with only a log line | `src/athena/core/recovery.py:318` |

## What held

Reproduced by a verifier or the lead unless marked "reviewer-executed".

- **July's stop-ship findings are closed.** Forge source routes require admin
  scope, a non-ASCII signature is an ordinary mismatch, automation does not
  fire on imported history, the security and attention reads exclude imported
  rows, and a refused label attach no longer grows the vocabulary. Each has a
  regression test. A reviewer-executed sweep of 396 method and route pairs
  found no remaining case of admin role without admin scope.
- **The doctrine is mechanical.** Three AST checkers fail the build. Planted
  violations were reported with file, line and reason (reviewer-executed). The
  real tree is clean even when `aegis/api.py` and `mentor/api.py` are counted
  as transports. The web layer has no write SQL. MCP has no database access.
- **Test honesty.** No skip, xfail or `MagicMock`. `unittest.mock` is imported
  in 4 of 339 files. The coverage checker has not changed since 2026-07-23 and
  rejected two narrowing attempts (reviewer-executed).
- **CI.** Every action is pinned by commit and a test enforces it. The
  toolchain is hash-locked. Shard receipts bind the commit, the policy digest,
  the exact test inventory and the data digest; the lead read
  `scripts/coverage_shards.py` and ran it end to end for the evidence above.
- **Leases.** One winner in a 12-way claim race; a stale generation is refused
  after takeover; yield, handoff and resume across two accounts work
  (reviewer-executed, by two different reviewers).
- **Durability.** A SIGKILL under keyed load lost none of 87 acknowledged
  writes; the migration ledger refused five kinds of tampering; the hash chain
  named the edited event; backup, recovery bundle and restore worked under
  write load (reviewer-executed).
- **The MCP surface narrows exactly as documented.** 61, 114 and 140 tools by
  token class; `seat` 26, `seat+knowledge` 37, `conductor` 37. An unknown
  profile refuses to start. `docs/AGENT_API.md` regenerates byte-identical.
- **Refusals on the lease path are actionable.** A repeat claim returns 428
  naming the generation to send; a stale `If-Match` returns 412 with a code.
- **The SSRF guard** blocked every internal address form tried, including
  IPv4-mapped IPv6, decimal and octal loopback, and the metadata address.
- **Core documentation.** `QUICKSTART` path A works as written
  (reviewer-executed). The AGENTS.md authority grep returns nothing, as it
  claims. Dated evidence is labelled as dated.

## The recent lanes

The request behind this review asked about work done by "Astra". No commit,
branch, PR body or file uses that name, so this section audits the two agent
lanes that did land work in the window defined above: codex and claude.

Quality of change scores 8. The coverage floors, the pragma allow-list and the
checker exemptions were not loosened. The `imported_at` checker gained 121
lines and lost none. Tests gained 20,860 inserted lines against 9,148 in
source. Three slices were exercised over real HTTP and behaved as their
documents say (reviewer-executed): the events long-poll (capacity 32, wait
bound 25 s), atomic pickup with idempotent replay, and the MCP profiles. The
last merge on main is a real synchronisation fix for a timing-sensitive test,
not a retry.

What the window shows about process rather than code:

- Merges follow green CI closely. PRs the lead sampled from the 2026-11-17
  batch went from open to merged in 9.5 to 46 minutes, and CI takes about 9 to
  11 minutes. `AGENTS.md` allows an agent to merge its own PR once it is
  green. Whether a second reviewer looked at each was not checked; two of that
  week's specifications record an independent review.
- The lanes follow different conventions: decision records, rebase against
  merge, commit trailers, pagination and timestamp choices.
- The work is not tracked in Athena. See COORD-10 and CONTRIB-3.

## Status of the July findings

| July finding | Status at `99a0286` |
|---|---|
| H-0.1 forge source CRUD gated on role, not scope | Closed, regression test present; re-probed live by the security reviewer |
| H-0.2 signature header enumeration oracle | Closed, regression test present; re-probed live |
| H-0.3 automation fires on imported history | Closed, regression test present; re-probed live |
| H-0.4 security counters and attention rollup count imported rows | Closed: the three reads filter `imported_at IS NULL`, the guard checker registers them, and a test covers it (read by the lead) |
| H-0.5 browser label attach creates vocabulary before authorization | Closed: find-or-create moved inside the command, and two tests assert a refused attach leaves the vocabulary unchanged (read by the lead) |
| H-3 make command ownership and `imported_at` exclusion mechanical | Landed as two gate stages |
| H-1.x, H-2 | Not re-audited individually in this review |

## Verification ledger

Severity is reviewer → verifier. "Exec" means the verifier reproduced it by
execution; "no" means it was confirmed by reading. "Lead" means the lead
re-ran it.

| ID | Reviewer → verified | Exec | Lead |
|---|---|---|---|
| SEC-1 | blocker → important | yes | yes |
| SEC-2 | blocker → important | yes | yes |
| SEC-3 | important → minor | yes | |
| SEC-4 | important → minor (documented) | yes | |
| DATA-1 | blocker → blocker; rated important here | yes | yes, as ARCH-1 |
| DATA-2 | important → important | yes | |
| DATA-3 | important → important | yes | yes, as MCP-1 |
| DATA-4 | important → minor | no | |
| DATA-5 | important → minor (documented) | yes | |
| DATA-6 | important → minor (documented) | yes | |
| ARCH-1 | important → important | yes | yes |
| ARCH-2 | important → important | yes | |
| ARCH-3 | important → important | no | |
| ARCH-4 | important → minor | yes | |
| ARCH-5 | important → important | yes | |
| ARCH-6 | important → minor | yes | |
| MCP-1 | important → important | yes | yes |
| MCP-2 to MCP-10 | important → important | yes | |
| MCP-11 | important → minor | yes | |
| LOOP-1 | important → important | yes | yes |
| LOOP-2 | important → important | yes | yes |
| LOOP-3 | important → important | yes | |
| LOOP-4 | important → minor | no | |
| LOOP-5 | important → important | yes | |
| LOOP-6 | important → minor | yes | |
| LOOP-7 | important → important | yes | |
| LOOP-8 | important → minor | no | |
| COORD-1 | important → minor (documented) | yes | |
| COORD-2 | important → important (documented) | yes | |
| COORD-3, COORD-4, COORD-5 | important → minor (documented) | yes | |
| COORD-6 | important → minor (documented) | no | |
| CONTRIB-1 | important → minor, pending proof of the OOM | yes | yes, as TEST-1 |
| CONTRIB-2 | important → minor (documented) | no | |
| CONTRIB-3 | important → minor | yes | |
| CONTRIB-4 | important → important | yes | |
| CONTRIB-5 | important → minor | no | |
| TEST-1 | important → important | yes | yes |
| TEST-2 | important → minor | no | |
| TEST-3 | important → minor (disclosed) | no | |
| TEST-4 | important → important | no | |
| TEST-5 | important → minor | yes | |
| DOCS-1 | important → important | yes | |
| DOCS-2, DOCS-3, DOCS-5, DOCS-6 | important → minor | yes | |
| DOCS-4 | important → important | yes | |
| DOCS-7 | important → minor (documented) | yes | |

CONTRIB-1's verifier withheld "important" because it had not seen the gate
fail. The lead did: the kernel log shows the kills. It is the same finding as
TEST-1.

## Not covered

- Operations and observability as a lens of its own (logs, metrics, readiness,
  the operator runbook end to end). It was dropped to fit a usage budget.
- Performance, load behaviour and the cockpit's usability or accessibility.
- Live GitHub settings: branch protection, environments and secrets.
- H-1.x and H-2 from July, individually.
- Paths the reviewers named as not exercised: SSRF delivery against a live
  redirecting receiver, review-queue self-review by a live two-agent run,
  attachment integrity in backups, and recovery from an interrupted restore.
