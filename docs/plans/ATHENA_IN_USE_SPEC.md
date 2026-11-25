# Athena in use: review findings and improvement specification

Date: 2026-11-22. Review baseline: `0b42d40` (`main` after Waves 0, 0b, 1 and 2
of [the agentic-use specification](AGENTIC_USE_SPEC_ATHENA.md)).
Status: **decisions accepted by standing instruction; Waves 3, 4 and 5 built; Waves 6, 7 and 8 begun; Wave 9 begun.**
Evidence: [reports/repo-review-2026-11-22-round2.md](reports/repo-review-2026-11-22-round2.md).
Finding IDs such as `FLEET-4`, `OPS-5`, `PERF-1` and `COCKPIT-2` refer to that
report. Requirement IDs that begin A, B or C are carried from the first
specification and keep their numbers.

## State of the project

This page is the continuity document. Read it first in a new session; update
it in the commit that records each wave's results. It holds pointers and
current facts only. One fact, one place: the detail lives where the pointer
goes.

**Objective.** Athena is a workspace several agents and one operator can run
real work through for months: the written loop always has a next move, a
failure is told to whoever is accountable, the operator can steer from the
browser, and none of it slows down as history grows.

**Where it stands.** Review round 2 graded `0b42d40` at 7.5 as a repository and
7.5 for agentic use (about 8 for one seat, about 6 for a fleet). The first
specification's Waves 0, 0b, 1 and 2 are merged (PRs #466 to #469); its Waves 3
and 4 are carried into this document. This document's Wave 3 is built in PR
#471: the six false statements are true again and the three findings that
blocked a fleet are fixed. Wave 4 is built in PRs #472 and #473: one outcome
with three slices costs 88 tool calls and 233,740 bytes where it cost 142 and
359,223. The first five requirements of Wave 6 are built in PR #474: the desk
as a seat answers in 34 ms at 10,000 issues and 93 ms at 50,000, where it took
259 and 1,285. The first four requirements of Wave 7 are built in PR #475: the
operator is told what the instance answers without a credential, a new
database is private to the service user, and a refused start names what is
wrong and what to do. The first four requirements of Wave 8 are built in PR
#476: an admin releases a held lease from the issue page, the navigation and
the dashboard show one "needs you" number, a fresh claim is not attention, and
an approval row says what is being approved. The first two requirements of
Wave 5 are built in PR #477: an edit keeps the text it replaced and can be
undone, and one unrelated assignment no longer voids every stored idempotency
receipt. Two more are built in PR #478: a restarted session replays its own
idempotency key, and stopping an account names its leases and may release
them. The rest of Wave 5 is built in PRs #479 and #480. Seven requirements of
Waves 6 and 7 are built in PR #481: the background loops run in one process
and only once it is bound, every request leaves one log line with an id the
response carries, a command removes rows that have no further use, and the runbook says how to
run its commands, restore onto a new host and upgrade. Five more are built
in PR #482: three lists page by cursor and the page list carries no bodies,
the project brief and a search for a common word stop growing with history,
a key left executing by a killed process can be listed and settled, boards
and lists name the holder of the live claim, and no page carries a script or
handler its own policy blocks. Seven more are built in PR #483, six of
Wave 8 and one debt: the declared fleet seats are data an admin manages,
an existing agent can be given a token and a removed one restored in the
browser, a playbook starts from its page, the inbox groups its rows in
words, the agents page is one section per agent, a long board column
pages, a gate stage fails on a command only one transport reaches, and
the knowledge profile opens its own notebook.
Nothing has been re-graded since.

**Decisions in force.**

- D1 to D15: [accepted 2026-11-22](../decisions/2026-11-22-agentic-use-decisions.md).
- The ready rule after a settlement:
  [its own record](../decisions/2026-11-22-ready-rule-awaiting-decision.md).
- D16 to D28, below:
  [accepted by standing instruction](../decisions/2026-11-22-athena-in-use-decisions.md).
  Each can be reversed by the owner; reversing one reopens the requirements
  that name it.
- Three successor records landed with Wave 3:
  [a review request answers a completion](../decisions/2026-11-22-review-request-answers.md) (D16),
  [the requested reviewer's verdict](../decisions/2026-11-22-requested-reviewer-verdict.md) (D4) and
  [lease-lapse events](../decisions/2026-11-22-lease-lapse-events.md) (D11).

**Known issues, most serious first.**

1. `/docs` and `/redoc` render blank: they are the framework's pages, and
   the content security policy blocks the CDN script and the inline script
   each starts from (found by U14's every-page test; the owner's question
   below).
2. `IssueOut.parent_id` gives the id of a parent issue the caller cannot see
   (found by F6's property test; older than this specification).
3. The idle sort computes its key for every matching issue (8.0 M SQLite
   steps at 50,000 issues) and the project brief is still linear in the
   issues of its own project (3.1 M, from 10.6 M): what is left of H5.
   `/desk/since` without a cursor still grows with history.
4. Assignment answers with the name of an account that cannot see the issue's
   project, so a member can learn that the account exists (found in Wave 4;
   the owner's question below).

**Already fixed; do not redo.** Self-approval, the audit event for a failed
project delete, the link-mention leak, the three loop defects, refusal codes,
the response budget, the seat profiles, executed entry documents, the inner
loop, the committed bootstrap. The "Wave 0 results", "Wave 1 results" and
"Wave 2 results" sections of the first specification say what each changed and
what each found and left. From this document's Wave 3: the fleet trial as a test
with ceilings, review pickup, the desk's structural budget, closed write
bodies, lexical path fences, the `lease_lapsed` event, and the requested
reviewer's verdict on the request row. "Wave 3 results" below says what each
changed and what each found and left. From Wave 4: compact tool answers, the
composed create and the ETag on every write that changes one, acknowledging
many notifications in one call, the work-order report, the event feed's
`project_id` and `mine` filters with facts as fields, the eligible-accounts
read and the onboarding profile, renewal on the generation alone, refusal
facts as fields with every registered `next` followed by a test, and one
timestamp shape with paging frozen. "Wave 4 results" says the same for each.
From Wave 6: the desk's newest-visible-event read, the ready rule evaluated
once over the issues that may be open, the search entry found by key, the
inbox indexes, the fleet-shaped seed and the benchmark with recorded medians,
the 40 ms delay of a server started on an inherited socket, a last-use
stamp that takes no lock on a read, the journal mode set once with a
bounded write-ahead log, three lists that page by cursor with a read of
one comment, a project brief that seeks each issue's own trail, and a
full-text search that ranks a bounded window. "Wave 6 results" says the same
for each. From Wave 7: the doctor's anonymous-reads
line and the tailnet refusal, private modes for a database Athena creates,
startup refusals that name the object and a remedy that runs as printed,
start-up notices that are true, a database file that is not recreated when
it disappears, a token minted on the host, background loops that start after
the bind in the one process holding their lock, one log line per request
with an id on every response, `athena-maintain`, and the list and resolution
of idempotency keys whose outcome is unknown. "Wave 7 results" says the same
for each. From
Wave 8: the lease release on the issue page and its `lease_released` event,
one "needs you" number, the heartbeat grace and the attention ranking, the
approval row, the one report partial and undo on the issue page, pages
with no inline script, handler or blocked style, the claim holder on
boards and in the issue list, seats declared as data, a token for an
existing agent, a restore and a playbook start in the browser, an inbox
grouped by what its rows are about, an agents page with one section per
agent, board columns that page, the parity gate between the cockpit and
the API, a refused form that renders in its page with what was typed, a
comment draft that survives a sign-in, a navigation badge and an issue page
that refresh themselves, and a cockpit that fits a phone and a long name.
"Wave 8 results" says the same for each. From Wave 9: the cockpit's JSON
refusals with registered codes, `authored_by` on issue lists, single reads
and searches, `valid_statuses` on a bulk row, writers that compose inside a
caller's transaction with a check that holds it, the agent OpenAPI
document's scopes, 404 and 428 and its build at start, one clock the lease
lifecycle reads and tests move, and imported comments and pages that name
no author. From Wave 5: the prior text of an edit, its
read and its undo, `If-Match` for a bearer edit of a title or body, and the
idempotency fence scoped to the issue a key names, a key that survives a
restart with replays marked for the agent, the leases of a stopped
account named and released on request, a spent budget that names the
leases to yield and tells who answers for the work, a HOLD or BLOCKED report
that asks whoever answers for it, each agent's last refusal readable by
the operator, a claim renewed with no recorded work raised, a second budget
counter for comments, claims and delegations, the boot block, guide and
glossary as MCP resources, a HOLD answer that asks for a rule, a read of
an idempotency key's state, and learnings that reach an issue's parent and
siblings. Wave 5 is built. "Wave 5 results" says the same for
each.

**Next step.** G9 (documents that drift) is being built. Wave 8 is built
except the idle sort that H5 left and the three missing twins the parity gate
names; what is left of Wave 9 is G1, G7, G9 and B6b (G2 to G6, G8 and G10 are
built; B11 is blocked). Each requirement that removes calls lowers the fleet trial's ceilings
in the same commit.

**How work is done here.** One requirement, one commit, with the test that
fails without it. Delegated build work is briefed with
[BUILD_BRIEF_TEMPLATE.md](BUILD_BRIEF_TEMPLATE.md), and the lead reads, re-runs
and probes each build before committing it. In Wave 4 every build also went to
a review agent that had not written it and ran it against a throwaway
instance; each review reproduced defects the build's own tests passed, so a
build that changes access, a migration or a wire shape is not merged on its
first pass. A wave leaves draft when
`scripts/gate.sh --profile product` passes on its head and hosted CI is green.

**Never done on the owner's behalf.** Cutting or pushing a tag, pushing a
container image, setting a prune verdict, answering an item in
`docs/DECISIONS_PENDING.md`, pushing `main`, lowering a coverage floor,
widening `ALLOWED_PRAGMAS`.

## Purpose

The first specification made the contract for one agent true and executed.
This one is about Athena being used: by a fleet, by the person supervising
it, by whoever keeps the instance running, and for long enough that history
accumulates.

It has five tracks:

- **F, the fleet.** Several agents on one outcome: fences, lapses, reviews,
  and what an outcome costs in calls and bytes.
- **W, the work order.** Three ideas taken from a prompt-writing course the
  owner supplied, applied to how work is briefed and how a mistake becomes a
  rule.
- **H, operate and scale.** Running an instance, and the statements that grow
  with history.
- **U, the operator's cockpit.** What the supervisor can and cannot do in the
  browser.
- **G, debts.** What the first three waves listed and did not pay.

Like the first, this is a specification of required behaviour and its
acceptance tests. It adds no queue, scheduler, orchestrator or second process.
The single-process SQLite architecture, the ownership model and the visibility
rules stay as they are.

## Principles

Principles 1 to 9 of the first specification stand. Four are added, each a
property a test can hold.

10. **The surfaces agree.** The desk never asks for work that pickup will not
    serve or name. The cockpit shows who holds a lease wherever it shows who
    is assigned.
11. **Cost is measured per outcome.** The calls and bytes one outcome takes
    are a number with a ceiling, held by a test, not a count somebody made
    once.
12. **A hot statement does not read history.** The desk, pickup, the inbox
    and every write take time that depends on open work, not on everything
    that ever happened. Tests assert query plans, not timings.
13. **The operator can act where the operator looks.** Every step of the
    operator's loop can be done in the browser, or is on a declared list with
    its reason.

### Wave 8 results (fourth lanes) and Wave 9 results (first lanes)

This is U11, U12 and U15 from Wave 8 and the debts G4 and G10, one commit
each. U11 and U15 were built by a delegated builder from the lead's brief
and read, probed and re-run by the lead; U12, G4 and G10 were built by the
lead. U12 and U15 were driven in Chromium against a seeded instance.

| Measure | Before | After |
|---|---|---|
| A refused plain form post | A bare fragment such as `<div class="error">Label name is required.</div>`: no stylesheet, no navigation, and what was typed lost | The same words and status inside the page shell, with a same-origin way back; htmx still receives the fragment byte for byte. 222 refusals in `web/` converted (U11) |
| A validation refusal on the issue, Mentor page, space, filter, fleet seat or agent mint forms | The typed values lost | The form re-renders with the error and the typed values (U11) |
| A comment submitted after the session expired | Lost | The refusal shows the text that was not posted, and `static/drafts.js` keeps a comment box's text in the tab until the comment is accepted (U11) |
| The "needs you" badge | Changed only on the dashboard or on a reload | Refreshes itself on every page, through the live-panel mechanism; a reader with no session polls nothing (U12) |
| The issue page's claim, run and last event | As of the moment the page loaded | Refresh themselves; in Chromium, "unclaimed" became "Sol Builder live lease" with no reload (U12) |
| Pages that scroll sideways, of 109 a signed-in admin reaches | 109 at 390 px (the navigation alone was 490 px); 37 at 1,280 px with long names | 0 at either width; tables scroll inside their own box (U15) |
| The default button | The accent reserved for "an agent is waiting on the human" | Ink (U15) |
| A skip link; route paths and developer notes in the interface; a next step on an empty page | None; printed; none | The first thing Tab reaches; gone; offered to a viewer who may take it (U15) |
| The cockpit's JSON refusals (the palette, `/admin/fleet.json`, a run's `replay.json`, a project's `floor.json`) | A detail and no code; the palette sent the internal kind `invalid` | The registered code a REST route would send; the refusal-code check holds `web/` to its JSON-body rules (G4) |
| `authored_by` on `GET /issues`, `GET /issues/{ref}`, `GET /issues/{id}/children`, `GET /issues/search` and `GET /search` | Absent: an agent listing work read another account's prose with nothing saying it was data | Present on each row or hit; for a comment hit, the comment's author. Looked up in one batch per page. Write answers do not carry it, so the ETag is unchanged (G10) |
| A `POST /issues/bulk` row refused for a status the project does not offer | The sentence alone | The sentence and `valid_statuses`, as the single write gives them (G10) |
| The contract seat run | 55 calls, 110,771 bytes | 55 calls, 111,011 bytes: the issue reads carry `authored_by` (G10) |

Decisions the lead took while building, each reversible by the owner:

- **A refusal decides its shape when it is sent (U11):** htmx gets the
  fragment, anything else the page. Hidden and missing objects still answer
  alike.
- **A draft lives in the tab's session storage (U11)**, keyed by the form's
  action, and is forgotten only on a clean load of the same form with no
  refusal on the page.
- **The badge is a panel of the dashboard route (U12)**, answered before
  anything else is read. The issue page's poll is built from the issue's
  id, so a page re-rendered by a refused POST still polls the GET route.
- **`authored_by` is not in a write's answer (G10).** It depends on the
  reader (a hidden edit reads as null), so it stays out of the
  representation the ETag is computed from.
- **A bulk row names statuses and nothing more (G10).** The first build
  also gave every refused row a registered `code`; five tests pin the
  legacy row shape, so it was taken out. Only a policy refusal carries a
  code on a bulk row, as before.

Found while building, and fixed before each was committed:

- **U12.** A test took the page's first poll, which became the badge's;
  it now names the panel. One mutant survived (the poll built from the
  request's path), caught by a refused-POST test.
- **G4.** One mutant survived (the whole-tree check not switching the JSON
  rules on), caught by a test that writes a `web/` module into a tree.
- **G10.** The wider run found the five pinned bulk-row tests and the
  contract seat's byte ceiling; the first is the narrowing above, the
  second is raised by 240 bytes with its reason in the ceilings file. The
  product gate then found what that run's file list missed: the batched
  author read undeclared in the `imported_at` guard file, three tests that
  compare a read whole with a write's answer, and the fleet trial's
  conductor ceiling (raised by 234 bytes, with its reason).
- **The coverage floors rose to 94.34, 86.27 and 92.49**, as the ratchet
  requires when coverage passes a floor by more than half a point (line
  94.59, branch 86.52, combined 92.74 on hosted CI).

Found and not fixed:

- **U11.** Forms beyond the first tier still answer a refusal in the page
  shell without the typed values.
- **U12.** The board and the inbox do not refresh themselves.
- **C10's three missing twins** remain, as above.

### Wave 9 results (second lanes)

This is G2, G5, G6 and G3, one commit each. G2 and G6 were built by a
delegated builder from the lead's brief and read, probed and re-run by the
lead; G5 and G3 were built by the lead.

| Measure | Before | After |
|---|---|---|
| Writers that commit whatever their caller is doing | 22, and five functions open `BEGIN IMMEDIATE` by hand | 0 outside two allowed functions, each with its reason in the check: `activity.record` (hands its writer reservation back to its command) and the export's read snapshot (always rolls back). Each converted writer is a savepoint inside a caller's transaction (G2) |
| A new unconditional commit or hand-written transaction statement | Passed the gate | Fails `check_transaction_commits` (G2) |
| The token scope an operation needs, in `/openapi-agent.json` | Not stated | The bearer requirement's role list: 127 operations scoped. A `read` token is refused `scope_denied` by 125 of them and by no unscoped one but two that check `admin` only for another account (G5) |
| 404 and 428 in the agent document | Under `default` | Declared where the route's code refuses with them (G5) |
| The first request for the agent document | Paid the build, about 3 s | `athena-serve` builds it in a thread once bound; a request during the build waits for it (G5) |
| Where the lease lifecycle reads the time | `datetime.now` in five places and SQLite's `datetime('now')` in four | `core/clock.py`; tests replace it through a fixture, and a guard fails a test that leaves it replaced (G6) |
| How a test lapses a lease | Rewrites its stored expiry (36 files) | Moves the clock past it; the tests that still write an expiry plant stored data on purpose, each named (G6) |
| The author of an imported comment, page or page version | The local account the import matched | `null`, from a mark migration 0094 adds; a native edit makes the text its editor's own (G3) |

Decisions the lead took while building, each reversible by the owner:

- **G2: the notifications writers use `db.transaction`, not a `commit`
  parameter**, so no caller changed. A call that passes `commit=True` from a
  function with no `commit` parameter is not a finding: the caller chose.
- **G5: a scope checked in a branch is not a route's requirement**; its 403
  still says a scope may be refused. The document may declare 404 or 428 where
  only an item refuses (`POST /issues/bulk`), as the walk reads what the code
  can raise. The demo server still builds the document on first request.
- **G6: while a test has replaced the clock, `db.connect` installs a
  `datetime()` that reads it**, because the triggers of migrations 0057 and
  0058 read SQLite's clock and an applied migration is never edited. A running
  app installs nothing. Only `datetime()` is bridged.
- **G3: rows imported before 0094 are not marked**: the import is a host
  command with no record of what it wrote, and an author is never guessed.

Found while building, and fixed before each was committed:

- **G5.** A dependency that checks its scope with `if not token_has_scope(...):
  raise` was read as unscoped; the reader knows that shape. The code reader
  walked each tree once per question and again for its calls; it walks once.
  Two mutants survived the first tests (a check after a `return`, and `admin`
  absorbing another scope); each has a test.
- **G6.** Five mutants survived the first tests (the fixture's restore, and
  four readers' default clock); each has a test.
- **G3.** The page read stamped its author from the public projection, which
  carries no mark; it reads the stored row's.

Found and not fixed:

- **G2.** 19 call sites ask a writer to commit from a function with no
  `commit` parameter. The check cannot see `rollback()` or `with conn:`.
  `seed_field_guide` cannot compose inside a caller's transaction.
- **G6.** 113 `datetime('now')` and 20 `datetime.now(` uses remain outside the
  lease lifecycle; in clock tests the SQL ones follow the clock through the
  bridge. `julianday`, `strftime` and `CURRENT_TIMESTAMP` keep SQLite's clock.
- **G3.** The import indexes issues and pages for search, not comments, so an
  imported comment cannot be found by search.
- **G5.** The build still takes about 1.5 s of the code reader beside FastAPI's
  own 2 s; it now runs before the first request rather than in it.

## Scorecard

Baselines are measured at `0b42d40`. A target that is arithmetic and not yet a
measurement says so; it is re-set once, after the first measurement, and never
loosened without a line in the wave's results.

| Measure | Baseline | Target | Wave |
|---|---|---|---|
| Written statements false by execution | 6 | 0 | 3 |
| Leases that may declare paths on an instance with no root catalog | 1 in the whole instance | Any number whose paths do not overlap | 3 |
| A lapsed lease reaches the assignee and the delegating account | Never, unless they poll | Within one `wait_for_events` period (25 s at most) | 3 |
| An unrequested PASS can replace what the requested reviewer said | Yes | No: the request row carries the requested reviewer's own verdict | 3 |
| Largest `my_desk` answer on a seat profile | 23,546 characters | 16,000 | 3 |
| Agent write bodies that accept unknown fields | 25 of 67 | 0 | 3 |
| A keyed retry after an unrelated assignment | 409 `idempotency_authorization_changed`; the marker is kept forever | Replays; a marker expires with its receipt. After Wave 5: both hold (F6); the fenced share of a scripted fleet run fell from 80% to 40%. | 3 |
| Tool calls and bytes for one outcome with three slices, full variant | 142 calls and 359,252 bytes, as F1's test measures them in process. The review's run against a live server counted the same 142 calls and 382,369 bytes: its ids were longer and its drains re-read each feed from the first cursor. | 90 calls (arithmetic: 142 less 35 acknowledgements, 15 setup calls and 4 ETag reads) and 250,000 bytes (arithmetic: 30% less). Measured after Wave 4: 88 calls and 233,740 bytes. | 4 |
| Calls that acknowledge one notification each | 39 of 142 | At most 4. Measured after Wave 4: 0. | 4 |
| `GET /desk` as a seat, median at 10,000 and 50,000 issues | 259 ms and 1,285 ms | 80 ms and 200 ms (arithmetic: less the trail scan and three of four ready passes). Measured after Wave 6: 34 ms and 93 ms. | 6 |
| A comment write, median at 50,000 issues | 75 ms, against 18 ms at 1,000 | Within twice the 1,000-issue figure. Measured after Wave 6: 12.6 ms, against 11.6 ms at 1,000. | 6 |
| The operator's priority inbox, median at 50,000 issues | 1,875 ms | 100 ms. Measured after Wave 6: 7 ms. | 6 |
| Webhook deliveries after three duplicate starts over 30 events | 47 | 30 | 7 |
| Routes that answer without a credential on a default instance | 74 of 156, reported nowhere | Counted and printed by `athena-doctor`; tailnet mode refuses to start without an explicit setting. After Wave 7: both hold. The doctor counts what the credential gate admits, 111 of 162 GET routes, which is more than answer. | 7 |
| Operator steps that cannot be done in the browser | 4 | 0, or each on a declared list. After Wave 8's first lanes: 3 (U5 remains). After U5: 0. | 8 |
| Issues an agent holds by lease that the board files under "Unassigned" | 16 of 18 | 0 | 8 |
| Capabilities on one surface only | 5 groups cockpit-only, 6 API-only | 0, or each on a declared list a gate check reads. After C10: 42 one-sided command functions, each declared; three are missing twins. | 8 |
| Commands that take a bare actor id; writes outside a command | 42; 23 | Lower each wave, held by a ratchet | 9 |

### Wave 3 results

Measured on the Wave 3 head in the same 4-CPU, 15 GB container. Wave 3 is F1
to F5, H11, U1, C6, C4 and C5, one commit each, in PR #471. Each was built by
a build agent from a six-part brief and then read, re-run and probed by the
lead before it was committed.

| Measure | Baseline | After Wave 3 |
|---|---|---|
| Written statements false by execution | 6 | 0. Each is held by a test: F2, F3, F4, F5, H11 and U1. |
| Leases that may declare paths with no root catalog | 1 in the whole instance | Any number whose paths do not overlap, compared by segment. The reviewer's own script, which asserted the refusal, now fails at that assertion (C6). |
| A lapsed lease reaches the assignee and the delegating account | Never, unless they poll | On their next feed read. In the reviewer's 60-second scenario, re-run by the lead, the conductor's wait loop returned `lease_lapsed` 15 seconds after expiry (C4). |
| An unrequested PASS can replace what the requested reviewer said | Yes | No. In the reviewer's scenario, re-run by the lead, the request row reads `reported_verdict: HOLD` while `review_state` reads `reported_pass` (C5). |
| Largest `my_desk` answer on a seat profile | 23,546 characters | 16,000 at every lane's declared maximum, alone and together. The lead's probe with 25 ready issues and long titles measured 15,331 (F3). |
| Agent write bodies that accept unknown fields | 25 of 73. The review counted 67 bodies; the lead's walk of the document finds 73. | 0 of 73 (F5). |
| Tool calls and bytes for one outcome with three slices | 142 calls, 359,252 bytes | 142 calls, 359,223 bytes. Nothing in this wave removes a call; Wave 4 does. Bytes fell 1,177 with F2's shorter lane descriptions and rose 440 with C5's two request fields and 708 with C6's fence basis on each desk read. |

The product gate on the Wave 3 head, `4b72265`: 5,875 tests in 23 minutes 38
seconds; line coverage 94.17%, branch 85.68%, combined 92.22%, against floors
of 93.86, 85.31 and 91.90; peak memory 1.64 GiB. Hosted CI passed all 11
checks on the same commit. Wave 2's head ran 5,771 tests.

Found by reviewing the builds, and fixed before each was committed:

- The first F1 commit failed hosted CI. Its scenarios for unbuilt requirements
  were strict expected failures, and the coverage gate accepts no skipped or
  expected-failure outcome. They became `@unmet` scenarios, which pass while
  the scenario stops at its requirement and fail once it holds, and
  `tests/test_suite_conventions.py` now refuses a skip or xfail mark in
  seconds. The gate did not change.
- U1's card read "yielded" while another agent held a live lease on an open
  handoff. A live lease is shown first.
- C4's observer read, which the event feed runs on every call, started from
  every unarchived issue. It starts from the lease table, held by a query-plan
  test.
- C6's first build published the root catalog's digest and root keys to every
  signed-in account, because the brief asked for the digest to be readable.
  `ACTIVE_WORK.md` deliberately gives no capability hint about the catalog, and
  no decision changes that. The disclosure was withdrawn before commit; the
  office reports the comparison basis only, and a test holds that no read
  returns the digest or a root key.
- Eight documented scripts ran their whole workload when asked for `--help`,
  and three of those rewrote a generated document. Found by F4's test.
- `get_project_floor` answered 243,832 characters at the declared maxima.
  Found by F3's rebuilt budget test.

Found and not fixed:

- **Whether an agent may read the catalog's digest and root keys** is the
  owner's question. A selector still comes from the operator.
- **The changed profile does not run the coverage stage**, so a failure only
  a coverage shard catches is first seen in hosted CI or the product gate. The
  new convention test covers the one case met so far.
- **F3's floors are judgement calls.** A full desk keeps its identity block
  and the first three ready rows; the rest is one call away. A list-shaped
  answer can still exceed its budget when a single row does, and an object
  tool other than the desk and the floor names no `next` on a structural cut.
- **Issue titles have no declared maximum** on create or update.
- **C5.** Reusing a review issue for a replacement candidate hides the earlier
  request and its verdict from every read but the activity history. The desk
  never lists a fulfilled request, so the two new fields are always null
  there.
- **C4.** The holder is not told of its own lapse. The browser dashboard's
  ranking does not observe. The attention row appears for agent-held claims
  only. Every bearer request still takes the writer lock before its route runs
  (H7), so under a long-held lock the feed fails before it can observe.
- **C6.** D5's "a project may require declared paths" is not built. Pickup
  takes no `coordination_root`, so with a catalog its paths cannot be placed
  beside another fence. A drive spelling such as `C:/x` is accepted as a
  declared path. When several leases overlap, the refusal names the lowest
  issue id, even if the caller could see a later one. The lexical basis cannot
  tell two repositories apart; the documents say to use distinct leading
  segments or configure a catalog.
- **F5.** `POST /spaces` does not take `visibility` and refuses it. A project
  born private through `ATHENA_DEFAULT_VISIBILITY` gets no roster row for its
  creator and no visibility event, as before. Multipart bodies still ignore
  extra form fields.
- **H11 was not run in a container.** It relies on three Docker behaviours
  nobody checked here: a key-only environment entry stays unset when the shell
  has none, `docker compose run --name` names the one-off container, and
  `docker exec` inherits its environment. Hosted CI's container job passes.
- **U1.** A lease row keeps no time for when its generation began, so the card
  shows when it was last claimed or renewed. Boards and the issue list still
  group by assignee (U2).

### Wave 4 results

Measured on the Wave 4 head in the same 4-CPU, 15 GB container. Wave 4 is F12,
C9, F7 and W3 (PR #472) and F8, F9, A12, F11 and A13 (PR #473), one commit
each. Each was built by a build agent from a six-part brief. F8, F9, A12 and
F11 were also reviewed by an agent that had not built them and ran them
against a throwaway instance. A13's independent review was stopped unfinished
to conserve usage, and the lead reviewed and probed it instead.

| Measure | After Wave 3 | After Wave 4 |
|---|---|---|
| Tool calls for one outcome with three slices | 142 | 88. The target was 90. |
| Bytes received for the same outcome | 359,223 | 233,740. The target was 250,000. |
| Calls that acknowledge one notification | 39 | 0 (F7) |
| Calls to set up one slice | 5, and a work-context read for each ETag | 1 (C9) |
| Text of a tool answer | Indented JSON | Compact JSON, the same object: 29.4% fewer bytes over the trial (F12) |
| What wakes a conductor waiting on its outcome | Every event in the instance | Events of its project, or of issues it is assigned, delegated, created or delegated onward (F8) |
| Generation, handoff token and verdict of an event | Inside prose `detail` | Fields (F8) |
| A conductor resolving an account id | No tool on its profile | `list_eligible_accounts` (F9) |
| Renewing a lease | The generation and a fresh issue ETag | The generation alone (A12) |
| Registered `next` hints that a test follows from the refusing state | 0 | All 10 that a state can be built for; 1 is recorded as unbuildable (F11) |
| Timestamp shapes in one desk read | 3 | 1 (A13) |
| Contract agent, two seats | 56 calls, 109,686 bytes at the head of PR #472 | 55 calls, 110,671 bytes |

Bytes fell by 129,435 with F12, C9 and F7, and rose by 3,952 across F8, A12,
F11 and A13 for the fields those add to every answer: event facts, the issue
ETag beside a lease, `events_after`, expiry seconds and the always-present
review-request lane.

The product gate runs on the Wave 4 code head; its receipt is in PR #473.

Found by reviewing the builds, and fixed before each was committed:

- **W3.** Two regular expressions in the work-order recognition were quadratic:
  one body line of 100,000 spaces took 60 seconds per work-context read. The
  recognition is linear string operations, held by a timing test.
- **F8.** A project the caller cannot see answered in time proportional to its
  events, which told a non-member that it exists and how busy it is; the read
  now answers before the trail is touched. A conductor's `mine` wait recorded a
  lapse it answers for and then hid it; `mine` includes issues the caller
  delegated to another account.
- **F9.** The eligible-accounts read listed accounts that cannot see a private
  issue. Both writes took removed accounts the read withheld. The cockpit
  onboarding form lacked the profile. Three sentences were not true as written.
- **A12.** The run-id rule lapsed when the run header was dropped, cut short
  or reshaped by the request path; a heartbeat now refuses such a header with
  `run_header_invalid`. Two existing tests compared a claim answer with the
  bare lease and had not been run by the build.
- **F11.** `claim_held`'s `next` was a lease read that restated the refusal and
  cost a call; it is a wait on the issue from a cursor the refusal carries.
  `fence_overlap`'s `next` restated the fence. Three codes lost a `next` that
  could not make progress. The approval scenario approved its own request
  before following `next`, and that hint woke on history. At integration the
  new test caught `run_id_mismatch`, registered by A12 with no scenario.
- **A13.** Its paging freeze caught the eligible-accounts read F9 had added in
  the same release, which paged by offset; it pages in D2's vocabulary. `since`
  on the admin security reads compared a packet-shaped timestamp as text and
  answered with nothing; the edge accepts the packet shape. Hosted CI then
  caught F11's `claim_held` refusal carrying its expiry in the stored shape
  beside a lease read in the new one; a refusal's fields now keep the packet
  conventions, stamped where every refusal is built.
- **The branch coverage floor rose to 85.56.** Hosted CI failed the F9 commit
  because coverage had risen more than half a point above the 85.31 floor.

Decisions the lead took while building, each reversible by the owner:

- A removed account is refused by every write that takes an account id, as a
  missing one is. It could be assigned and delegated to before.
- The eligible-accounts read lists only accounts that can see the issue.
- An event feed's `mine` includes issues the caller delegated to another
  account, because those are the lapses it answers for.
- `claim_held` and `fence_overlap` point at a wait, and `scope_denied`,
  `blocked_issue_close_policy` and `lease_generation_required` name no `next`.

Found and not fixed:

- **F9.** Assignment still answers with the name of an existing account that
  cannot see the issue's project, so a member who compares it with the read
  learns that account exists. Whether assignment should refuse it is the
  owner's question. The read checks visibility once per account and is not
  paged in SQL.
- **F8.** `mine` is the set at the read, not at the event: the event that takes
  an issue away is not in the loser's `mine` feed. A filtered drain from an old
  cursor reads every later event of the caller's issues before it returns a
  page (117 ms at 60,000 events); the documents say to drain unfiltered and
  keep the filters for waiting. The facts table is append-only by trigger but
  is not covered by the hash chain; `detail` carries the attested values. An
  event recorded before migration 0086 has no fields.
- **A12.** A heartbeat body `run_id` in a reserved namespace is accepted when
  no header is sent. The server instructions name `begin_run` and `get_page`,
  which the seat and conductor profiles lack.
- **F11.** A `claim_held` wait does not wake when the lease simply lapses for a
  caller who is not accountable for it, because no event is recorded for that
  caller; the hint says to retry after `expires_at`. An admin refused
  `lease_generation_required` has no `next`. `fence_overlap` with a hidden
  holder has nothing to wait on. Codes raised from several states are followed
  from one. A validation refusal names its bound in prose only.
- **A13.** Resource reads (the issue, comments, pages, search), the issue
  write answer and its `held_lease` note, `/fleet/active-work` and admin reads
  keep the stored timestamp shape, so an agent still meets two shapes in one
  session. The desk, office, floor and history answers are untyped, so the
  OpenAPI document cannot declare their fields; a test walks the values
  instead. Two work-context sections are still omitted when empty. A
  work-context ETag held across the upgrade no longer matches.
- **F12.** In-process `FastMCP.call_tool` still returns indented text; only the
  protocol answer is compact, and budget tests use `results.answer_text`.
- **The changed profile still does not run the coverage stage**, so the floor
  ratchet was first seen in hosted CI.

### Wave 6 results (first lanes)

Measured on the Wave 6 head in the same 4-CPU, 15 GB container. This is H8,
H1, H3, H2 and H4 (PR #474), one commit each, and one fix the benchmark
found. H6, H5, H7 and H9 are not built. Two build agents built the five from
six-part briefs; each lane was reviewed by an agent that had not built it and
corrected after that review, and the lead read, re-ran, corrected and
committed each.

Medians over HTTP against `athena-serve` on a fleet-shaped workspace, by
`scripts/benchmark_hot_paths.py`; the 10,000-issue column is the one recorded
in `docs/PERFORMANCE_MEDIANS.json`.

| Read or write | 1,000 issues | 10,000 | 50,000 | Baseline at 50,000 |
|---|---|---|---|---|
| `GET /desk` as a seat | 20 ms | 34 ms | 93 ms | 1,285 ms |
| `GET /desk/ready` as a seat | 10 ms | 12 ms | 23 ms | not measured alone |
| `POST /issues/claim-next` | 21 ms | 25 ms | 38 ms | 156 ms inside the writer lock |
| A comment write | 11.6 ms | 12.4 ms | 12.6 ms | 75 ms |
| The operator's priority inbox | 7 ms | 8 ms | 7 ms | 1,875 ms |
| The newest visible event id, in process | 0.01 ms | 0.01 ms | 0.01 ms | 840 ms |
| `GET /desk/since` with no cursor | 13 ms | 39 ms | 165 ms | not a target of these five |
| `GET /projects/{id}/brief` | 13 ms | 70 ms | 357 ms | H5, not built |

The baselines are the review's, from its own scripts on its own seed; they
were not re-measured with this benchmark on the old code, so each pair is an
order of magnitude and not a ratio. No answer changed: on the integrated tree
the lead compared the old code with the new over 80 random workspaces, every
desk lane, the ready lane and the `is:ready` atom, 668,534 comparisons, and
all agreed. The reviewer had run 2,757,838 over 334 workspaces on the first
build. The fleet trial's ceilings did not move.

The product gate's receipt is in PR #474.

Found by reviewing the builds, and fixed before each was committed:

- **H3.** Skipping closed issues was switched off for the whole workspace
  when any one project redefined done; it is decided per project. A lane
  limit outside 1 to 50 changed its answer and is now refused. Hosted CI then
  failed an existing test that traced the second of the rule's two
  statements; the rule is one statement and the test follows it.
- **H2.** A stale map row naming the rowid the next entry would take made
  that write, and every one after it, fail: every new issue answered 500
  after an old process had written once. The write replaces its map row on
  either key. Run again on a drifted map, the migration could delete a
  source's only entry; its first pass drops any map row that does not name
  its own source's entry.
- **H4.** The unread index carried the event id, and SQLite then planned an
  acknowledgement by id as a probe of every visible event for every id: 22 s
  where it had taken 7 s at 50,000 issues, inside the writer lock. The index
  no longer carries it, the visibility gate is asked of each row's own event,
  and a statement that names notifications by id finds each by its key; left
  to choose, the planner walked every notification the user has. The
  changelog claimed more reads were faster than are.
- **Every HTTP median the benchmark printed was about 42 ms too high.**
  `athena-serve --fd`, the socket-activated start, answered every request
  about 40 ms late: 44 ms for `/healthz` against 2 ms for a server started
  with a host and port. The server library wraps an inherited descriptor as a
  UNIX-domain socket, so `TCP_NODELAY` was never set on its connections. The
  launcher sets it on the listener. An instance started by socket activation
  had this delay on every request.

Found and not fixed:

- **The desk still grows with history**: 3, 18 and 72 ms in process at the
  three sizes. `/desk/since` with no cursor grows faster (5, 31 and 155 ms in
  process), and neither was in these five requirements.
- **H4.** Not faster: the priority summary, which reports on every visible
  row; a priority read whose `min_priority` few rows pass, which reads back
  until its page is full; and the unread count for a non-admin, which checks
  visibility on every unread row. Notifications are still never deleted
  (H20).
- **H2.** Entries an old process writes after the migration are not mapped
  until the migration file is run again; stop Athena before migrating.
- **Plans are pinned with no statistics.** Athena never runs ANALYZE, so the
  statements these five changed name their index or forbid one. A plan test
  fails if one changes, and asks again after ANALYZE where statistics could
  change it.
- **H8.** The gate times nothing. The recorded medians describe one container
  and are for people; the benchmark refuses to record on a busy machine or
  from an uncommitted tree.
- **The changed profile still does not run the coverage stage.**

### Wave 7 results (first lanes)

This is C11, H10, H12 and H18 (PR #475), one commit each. H13 to H17, H19 and
H20 are not built. One build agent built the four from a six-part brief; an
agent that had not built them reviewed them twice against throwaway
instances, and the build was corrected after each review. The lead then ran
the operator commands by hand against broken databases, ran every command a
line printed exactly as printed, and corrected what that found.

| Measure | Before | After Wave 7 |
|---|---|---|
| What an instance answers without a credential | Reported nowhere | One `athena-doctor` line: the setting and whether it was chosen, the GET routes the credential gate admits (111 of 162), the public projects and spaces by key, and the reads no container scopes (C11) |
| Tailnet mode with `ATHENA_ANONYMOUS_READS` unset | Starts, readable without a credential | Refuses to start until the setting is given either way (C11, D7) |
| A database the documented bootstrap creates | `0644` in a `0755` directory | `0600` in a `0700` directory, whatever the umask (H10) |
| A refused start | A bare exception text | One line naming the database, the object and the remedy; a command only where it runs as printed (H12) |
| A database the service account cannot write | Passes preflight, then fails every write | Refused at start (H12) |
| The cookie advice every start logged | Named a setting `athena-serve` refuses | Says what is true of that start (H18) |
| A misspelled `ATHENA_` variable | Silently ignored | Named in one warning, without its value (H18) |

The product gate's receipt is in PR #475.

Found by reviewing and probing the builds, and fixed before each was
committed:

- **C11.** The stored route count was one behind at integration: its test
  caught the eligible-accounts read Wave 4 had added. A space key could hold
  a newline and write a line of the doctor's report; a key is printed only
  when it has a project key's shape, and as `#<id>` otherwise.
- **H10.** A `chmod` was printed for files in a shared directory, where
  running it would not make them private, and for a directory holding other
  applications' entries. It is printed only for a regular file the caller
  owns in a directory of its own, and for the directory only when it holds
  nothing but Athena's entries.
- **H12.** The bootstrap remedy was one command whose token the operator's
  shell never saw, so the shell could not create the first user; it is an
  `export` and a start run in one shell. A start command dropped the refused
  start's `--host` and `--port`. A root-run instance with a `0400` file was
  refused although root can write it; writability is judged by what a write
  would meet. Found by the lead: the keep-a-copy command stopped at `mkdir`
  when a copy from an earlier refusal was still there; the line names that
  copy and prints no command until it is moved.

- **Code scanning read a local named `secrets` as a secret reaching the
  doctor's warning line.** It held a fixed sentence; no secret reaches that
  line. The local is renamed and nothing is suppressed.
- **The coverage floors rose to 94.06, 85.95 and 92.19.** Hosted CI failed
  the head because branch and combined coverage had risen more than half a
  point above their floors.

Decisions the lead took while building, each reversible by the owner:

- A printed remedy runs exactly as printed and does what its line says, or
  the line states the condition and prints no command.
- A kept copy of the database is never overwritten by a printed command.
- `athena-doctor --deployment` warns about a database its own account cannot
  write and keeps its exit code, since the account running it may be an
  operator's and not the service's.

Found and not fixed:

- **A tailnet instance that upgrades must set `ATHENA_ANONYMOUS_READS`
  before it will start.** This is D7, and the changelog says so.
- **C11.** The count is what the credential gate admits, not what answers:
  past the gate a page may still ask the viewer to sign in. It is stored with
  the release and a test recomputes it, so a new route fails that test until
  the count is updated. The default stays on (D23).
- **H10.** An existing database or directory with a wider mode is reported
  and never changed. A backup or export file's mode is not covered here.
- **H12.** The lead's probes ran as root, so the not-writable refusals were
  exercised by the build's tests and the reviewer's runs, not by hand.
- **H18.** Only `athena-serve` and `athena-doctor` report an undeclared
  variable; the MCP server and the seat doctor do not.

### Wave 8 results (first lanes)

This is U3, U9, U6 and U7 (PR #476), one commit each. U2, U4, U5, U8, U10 to
U15 and C10 are not built. Two build agents built the four from six-part
briefs; an agent that had not built them reviewed each pair against a
throwaway instance, and each build was corrected after its review. The lead
then drove the pages in a browser at 1,280 and 390 px against a demo
instance, read the screenshots, and corrected what that found.

| Measure | Before | After Wave 8's first lanes |
|---|---|---|
| Releasing another account's live lease | REST only | A confirmed button on the issue page, through the same command (U3) |
| How that release is recorded | `claim_completed` by the admin, read as finished work | `lease_released`, told to the holder, counted by nothing as a completion (U3) |
| "Needs you" in the navigation and on the dashboard | Two totals that differed | One number from one read; a live refresh moves both (U6) |
| Where sign-in lands | A page with no attention on it | The dashboard, whose first panel is the attention queue (U6) |
| A fresh claim | "Needs attention" at once: 54 of 54 | Not attention until its heartbeat is overdue, measured from the acquisition (U7) |
| A pending approval among missed check-ins | Row five | Outranks every missed check-in (U7) |
| An approval row | The action and a target id | The issue key, its title and the verdict the ask rests on, with its author (U9) |
| A completion report | Raw JSON on five surfaces | One partial, escaped text (U9) |
| Undo | The activity page only | Also the issue page, for its newest undoable event (U9) |

The product gate's receipt is in PR #476.

Found by reviewing and probing the builds, and fixed before each was
committed:

- **U3.** The first build kept the recording `main` has for an admin's
  release: the admin's own `claim_completed`, which settlement, work progress
  and review requests read as finished work. It is its own verb. The form's fence was a timestamp, so
  a new possession in the same second could be released by a stale page; it
  is the lease generation.
- **U6.** A live refresh moved the card and left the navigation number
  stale. Open questions waiting on the operator were not in the number. The
  sign-in page followed a `next` parameter; it lands on the dashboard
  whatever the request carries.
- **U7.** The grace restarted at every renewal, so a claim that renewed and
  never reported was never attention; it runs from the acquisition. Another
  agent's comment marked an operator's question answered; only a person's
  act does.
- **U9.** The lead's rule replaced the build's: the issue page offers undo
  for its newest undoable event and nothing when that one would be refused.
  The build moved down to an older event. Found in the browser: the approval
  row made the agents page 66 px wider at phone width; each table of that
  page scrolls inside its own region.

Decisions the lead took while building, each reversible by the owner:

- An admin ending another account's lease is `lease_released`, not a
  completion: the issue returns to the ready lane and the holder is told.
- One Undo on the issue page means the last change that can be reversed. An
  older event is undone from the activity page.
- Only a person's act answers a question in the operator's queue. Any
  account that is not an agent counts, so a member's reply answers it until
  the asker says it is still blocked.
- A claim never tagged with a run is attention at once, at medium, because
  it cannot check in.
- An undo that would close an issue behind an approval gate is not offered;
  a click asks for the approval and says so in the page.

Found and not fixed:

- **At 390 px the navigation row runs off the right edge on every page**, and
  the "needs you" number with it; the page scrolls 100 px sideways. This is
  on `main` before Wave 8.
- **Every page logs one Content-Security-Policy refusal of an inline style**
  to the browser console. Also on `main` before Wave 8.
- **U3.** A form message returned in the URL is shown in the page, capped at
  300 characters and escaped, as the activity page already does; a crafted
  link can show its own sentence there.
- **U7.** The read of each held lease's acquisition is one statement per
  lease; its cost is left for Track H. A member's question back on an
  agent's question reads as answered until the agent replies.
- **U9.** The offer does not predict a durable action budget or a project's
  blocked-close policy; the click answers for those. An edit of an issue's
  text is not undoable until F10.
- **Screenshots at 1,280 and 390 px were taken and read by the lead**; the
  tools at hand cannot attach an image to a pull request.
- **Code scanning left five medium notes on PR #476**, "URL redirection from
  remote source": four on the undo form's return target and one on the
  release redirect. The lead traced each. The return target is followed only
  when it fully matches an issue page's path, and the release redirect is a
  fixed prefix and an integer. They are answered on the pull request and left
  open for the owner to dismiss.

### Wave 5 results (first lanes)

This is F10 and F6 (PR #477), one commit each. C8, C7, F13, F14, F15, A11,
A14, W4 and W5 are not built. Each was built by a build agent from a six-part
brief. F6 was reviewed by an agent that had not built it, corrected, and its
reviewer's scripts re-run. F10 had no independent review: the lead read it
and probed it against a throwaway instance instead.

| Measure | Before | After Wave 5's first lanes |
|---|---|---|
| The text an edit replaced | Gone | Kept beside the edit's event, read by one call, by a caller who can see that event (F10) |
| Undoing an edit | Refused: one-way | Restores the old text exactly; refused once a later edit replaced it (F10) |
| A bearer edit of a title or body with no `If-Match` | Applied | 428 `precondition_required`; `*` or a list is 400; a stale tag is 412 (F10, D20) |
| A keyed retry after an unrelated assignment | 409 for every stored receipt | Replays (F6) |
| A fenced marker | Kept forever | Expires with its receipt (F6) |
| Narrowings that moved no counter | A contributor removed, a decline, `is_agent`, an approval gate, a budget, a close policy | Each fences (F6) |
| Fenced share of a scripted fleet run | 80% | 40% (F6) |
| One outcome with three slices | 88 calls and 233,740 bytes | 88 calls and 233,740 bytes: unchanged |
| Contract agent, two seats | 55 calls and 110,671 bytes | 55 calls and 110,671 bytes: unchanged |

The product gate's receipt is in PR #477.

Found by reviewing and probing the builds, and fixed before each was
committed:

- **F6.** Six narrowings moved no counter, so a replay returned what its
  caller could no longer obtain: a contributor removed, a decline, an
  account's `is_agent` flag, an approval gate added, a budget set, and a
  project's blocked-close policy. Each fences now. A process still running
  the old code after the migration replayed a fenced receipt; the original
  counter keeps moving on every act, so the old code fails closed. The
  documents claimed "never" without the exceptions; they list them. The
  property test could not see the workspace half; it runs 3,000 generated
  histories and fails on fourteen deliberately too-narrow fences.
- **F10.** Found at integration, each by a test of an earlier wave: its read
  was not classified as an agent packet (A13); its timestamp used a second
  copy of the shape helper; its history test compared a packet timestamp with
  a stored one. The lead added an undo precheck for edits, so the issue
  page's offer (U9) includes them, and an edit that never had an inverse is
  no candidate for that offer.
- **F6 against F10.** Five of F6's tests and its refusal scenario edited a
  title by bearer token with no tag, which F10 refuses. They change a
  priority.
- **Three counts and one tool text, each caught by a test of an earlier
  wave.** F10's route moved the anonymous-read counts stored with the release
  (C11) to 112 of 163 GET routes; its tool moved the full surface to 142; and
  `update_issue`, a seat tool, told a seat to call the conductor's read.

What the lead ran against F10 on a live instance: an edit with no tag, with
`*`, with a stale tag and with the current one (428, 400, 412, 200); the
prior text read by a project member (200), by a non-member and with no
credential (404), through another issue (404) and for a non-edit event (404);
the old text searched for in the feed, the event stream, search, the
work-context, notifications and the history (in none); undo by the seat
(restored), undo of an edit a later edit replaced (409 `undo_no_effect`) and
undo by a non-member (404). After the issue left its private project, an
account that could now see the issue still saw none of the edits recorded
while it was private, and could read none of their text.

Decisions the lead took while building, each reversible by the owner:

- An edit that never had an inverse (recorded before prior texts were kept)
  does not end the issue page's Undo offer; the next event is asked.
- F6's tests keep testing the fence by changing a field that needs no tag,
  not by sending tags.

Found and not fixed:

- **F10.** Editing a secret out of an issue no longer removes it: the
  replaced text stays readable, for good, by everyone who could see that
  edit. Nothing redacts or purges a prior text, and the table only grows.
  Whether an admin needs a way to remove one is the owner's question below.
  An edit recorded before the migration, and imported history, carry no
  prior text. An export carries only an issue's current text. The row is
  append-only by trigger and is not covered by the hash chain.
- **F10 is a wire change that can refuse a caller that works today**: a
  bearer `PATCH /issues/<built-in function id>` or `link-mention` that sends a title or body
  with no `If-Match`. The refusal's `next` names `get_issue`.
- **F6.** A replay can still be refused where a fresh request would succeed:
  the fence fails closed. `is_agent` set to false fences too. Revoking a
  token moves no counter, and a revoked token cannot reach a replay. Spending
  a budget moves none, and a replay is not metered. A replay does not
  re-check the target's state. Review requests are not in the property test.
  A future narrowing that needs a longer history than the generator draws
  could pass the property; the named HTTP tests are the backstop.
- **F6's migration rebuilds the receipt table**: about 1.4 s at 200,000
  receipts and past the five-second busy timeout above about 700,000, so
  migrate with the service stopped.
- **`IssueOut.parent_id` gives the id of a parent the caller cannot see.**
  F6's property test met it; it is older than this specification.

### Wave 5 results (second lanes)

This is F14, C8, F13 and F15 (PR #478), one commit each. The lead built F14,
F13 and F15, test first. A build agent built C8 from a six-part brief; it had no
independent review, and the lead read it, re-ran its tests on the integrated
tree and probed it against a throwaway instance. Running two build agents
beside the lead had exhausted a usage window, so one ran at a time.

| Measure | Before | After |
|---|---|---|
| A restarted session retrying its in-flight write | 409 `idempotency_mismatch`: the key was matched on the run id, and `athena-mcp` mints one per session | Replays the first answer; the event stays attributed to the run that made it (F14) |
| An MCP answer that is a stored one | Not marked | Carries `_replayed: true`, as REST sends `Idempotent-Replay` (F14) |
| Pause, token revocation, offboarding and removal | Leave the account's leases and fences active and say nothing | Each answers with `active_leases`; `release_leases` releases each in the same transaction, one `lease_released` per lease (C8, D12) |
| An admin settling a dead agent's expired lease | Refused `claim_required`; the admin had to claim it first | Settled with its exact generation: `lease_lapsed` once, then `lease_released` (C8) |
| A seat refused for budget while holding a lease | 429 with no `next`; the issue stays out of reach until the lease lapses | 429 with `held_leases`, `retry_after_seconds` and `next` = `yield_claim` with reason `capacity`; the yield is not metered (F13) |
| Who is told that a holder ran out of budget | Nobody | The issue's assignee and the account that delegated it, once per budget window, by one `holder_budget_exhausted` event per held issue (F13) |
| `agent_budget_exhausted` rows for 20 refused requests in one window | 20 | 1, saying what was spent, how many leases are held and the first request refused (F13) |
| A completion that reports `HOLD` or `BLOCKED` | An event; no attention signal | A ranked row, `held_report` (`high`), for the assignee, the delegating account and the operator, and a count on the card, until someone acts on the issue (F15) |
| What an operator can read about a refused or idle agent | Nothing: a refusal and "nothing ready" are answers, on no trail | Per agent, the newest 4xx it was answered (`code`, status, request, time) and the newest time a pickup answered `nothing_ready`, in the run-health read and on the agent-runs page (F15) |

The product gate's receipt is in PR #478.

Decisions the lead took while building, each reversible by the owner:

- **The run is not part of what a key is matched on.** The requirement
  allowed this or a stable run id in the printed configuration. A stable run
  id would make every session of a seat one run, which run controls and the
  heartbeat rule depend on being separate.
- **A replay is marked for the agent.** The run match had one protective
  effect: a key reused in a later session for an identical call was refused
  and not answered from history. The marker replaces it.
- **`release_leases` is never a default**, is refused on a resume and on the
  acting admin's own account, and a failed release rolls the whole act back.
- **A spent budget releases nothing.** The refusal names the yield; the seat
  makes it, or the lease lapses and says so. An automatic release would end
  work a seat could still report on.
- **"Once per window" is the stored budget window.** A raised limit inside a
  window does not open a new one, so a second exhaustion after a raise in the
  same window records nothing and tells nobody.
- **The holder's own refusal is not sent to the holder**, also when the
  holder is the issue's assignee.
- **What ends a HOLD row is an act on the issue, not a button.** The ready
  lane's answer rule (a comment, status change, assignment or claim by an
  account other than the reporter), or anyone claiming the issue again. No
  new write was added to acknowledge one.
- **The last refusal is the newest only, per agent, outside the trail.** A
  row per refusal would let a looping agent grow a table by being refused,
  and a refusal on the trail would say something happened. It is diagnosis:
  the agent's own calls move it and nothing may be decided from it. Agent
  accounts only.
- **"Nothing ready" is recorded at pickup**, not when the desk is read: the
  desk is the hot read and stays a read.

Found while building, and fixed before each was committed:

- **F14.** Three older tests asserted the run match on purpose (pickup and
  both review-queue receipts); they assert the new rule. Thirty-four test
  cases in eight tests compared a replayed MCP answer with its original for
  exact equality; they check the marker and compare the rest. Four mutants
  are each caught.
- **C8.** One older test encoded the rule C8 reverses for an admin. A first
  wording of the desk's text added bytes to every desk answer; it was
  reworded to the same length, so the trial's ceilings did not move.
- **F13.** A refused charge stored no budget window, so an account whose
  limit refuses the first write of a new window (a limit of zero, a frozen
  account) would have had every refusal open its own window and record
  again; the recorder stores the rolled window. Three mutants survived the
  first tests (the frozen window, the holder told of its own refusal, a
  stored inbox row for an account that cannot see the issue); the tests were
  strengthened until each was caught. The new read of
  the trail was undeclared to the imported-history check and is declared
  native-only. The product gate failed once on this head: the new command
  module is imported only by the app's composition root, which the test
  selector's own check refuses until the map names its tests.
- **F15.** Left to itself the planner found the held completions by walking
  every issue event; the read is driven from the partial index and a test
  plans it. A "newest settlement" clause in the first version was redundant
  (a completion ends its lease and only a claim starts one, so a later report
  always follows a claim) and no test could tell it from its absence; it was
  removed. Eighteen mutants are each caught, after two survived the first
  tests (that clause, and a success held like a refusal, now covered by a
  streaming test of the middleware).

What the lead ran against C8 on a live instance: a pause with no option
named two leases and released neither; the option on a resume was refused
422; a second pause with the option released both, recorded one
`lease_released` each with the holder told, and no second `paused_user`; a
token revocation left the lease active though the seat could no longer act,
and released it on request; an offboard released on request; a member was
refused 403. For an expired lease an admin was refused 428 with no
generation and 409 with a wrong one, another seat was refused 403, and the
exact generation settled it as `lease_lapsed` then `lease_released`, after
which the issue was back in its assignee's ready lane.

Found and not fixed:

- **F14.** A key reused across sessions for an identical call is answered
  from history where it was refused. The answer says so, and only for the
  same credential. A process on older code refuses a receipt this release
  stored, which fails closed. A list answer is not marked; no keyed write
  answers with a list today.
- **C8.** The cockpit's Admin, Agents buttons do not offer `release_leases`
  and the page does not show the leases; removal has no MCP tool. A removed
  account that watches an issue still gets an inbox row for the release the
  removal wrote: the watcher fan-out does not skip removed accounts, which is
  older than C8. A lease on an issue the acting admin cannot see would be
  neither named nor released; no such issue exists today, because an admin
  sees every project. An admin settling a lapsed lease leaves two events,
  the lapse and the release. The lister reads every live lease in the
  instance to find one account's.
- **F13.** The refusal names at most 20 held leases and the events cover the
  same 20. A seat that ignores the `next` keeps its leases until they lapse.
  A delegating account that can no longer see the issue is not told, and
  nothing tells the operator that it was skipped.
- **F15.** A comment by any third account ends a HOLD row, as it lifts the
  ready lane's hold. A completion recorded before migration 0086 stored no
  verdict and is never listed. The row's `next_action` is a link to the
  issue; no call is named for an agent. A replayed idempotency receipt of a
  refusal is not traced, because the replay is answered before the route. A
  seat that reads the desk and never calls pickup leaves no "nothing ready"
  time. The trace costs one extra write for each refused request of an agent,
  after the write token resolution already makes.

### Wave 5 results (third lanes)

This is C7 with its decision D6, A14, W4 and A11 (PR #479), one commit each;
C7 is two commits, the signal and the counter. The lead built C7, D6, W4 and
A11, test first. A build agent built A14 from a six-part brief; it had no
independent review, and the lead read the patch, corrected one glossary row and
re-ran its tests on the integrated tree. One build agent ran beside the lead.

| Measure | Before | After |
|---|---|---|
| A holder that renews with no recorded work | Read as `pinging`; never raised | Each renewal's answer carries `renewals_without_progress`; the renewal that reaches `ATHENA_NO_PROGRESS_RENEWALS` (3) records `no_progress` once and tells the assignee and the delegating account; the live claim carries the reason in attention (C7) |
| A budget of 3 an hour and 5 comments | All 5 applied | With `coordination_limit` 3, the fourth is refused 429 `agent_coordination_budget_exhausted`; without one, as before (C7, D6) |
| What an MCP-only agent can read of the boot block, the guide and the glossary | Nothing: no resource is served, and prompts are advertised with none behind them | Three resources in every profile; no prompts advertised (A14) |
| Words in tool names with a glossary row | None of desk, office, floor, inbox, watch, yield, claim-next | Every word that is not a plain verb or connective, each with a "Not the same as" cell, held by a test (A14) |
| The answer to a HOLD or BLOCKED report | The same sentence as any completion | Names `record_run_learning` in `next` (W4) |
| What a caller can learn about a key it sent | Only by retrying and reading the refusal | `GET /idempotency/{key}` and `get_idempotency_key`: one of five states, the request's method and path, and what a retry would meet; nothing is changed (A11) |
| Contract agent, two seats | 55 calls and 110,671 bytes | 55 calls and 110,701 bytes: C7's count on the one renewal |
| Fleet trial | 88 calls and 233,740 bytes | 88 calls and 233,853 bytes: W4's sentence on the reviewer's one HOLD |
| MCP tools: seat, with knowledge, conductor, all | 26, 37, 39, 142 | 27, 38, 40, 143: `get_idempotency_key` (A11) |

The product gate's receipt is in PR #479.

Decisions the lead took while building, each reversible by the owner:

- **Progress is the holder's own recorded work on that issue.** Another
  account's comment does not start the count again, and work recorded
  elsewhere is not seen: the holder says where it is in a comment. The
  threshold is 3 renewals, at most 90 minutes with the default lease, and
  `0` turns the signal off.
- **`no_progress` is told to who answers for the work, not to watchers**, and
  ranks `medium` when a missed heartbeat is the only other trouble. Nothing is
  refused or released.
- **The coordination ceiling is optional, shares the budget's window, and has
  its own refusal code and its own once-per-window event.** Nobody is told on
  the account's behalf when it is spent, because the account can still do,
  renew, complete or yield what it holds. Comment edits, labels, links and
  watches stay unmetered.
- **A budget is replaced whole.** Setting one without `coordination_limit`
  removes a ceiling set earlier.
- **The three documents ship as copies** under `mcp/documents`, held identical
  to `docs/` by a test, as the field guide ships. Moving the source of truth
  would have touched every test and link that pins `docs/`.
- **`get_idempotency_key` is in the seat profile.** A seat makes keyed writes,
  so it is the seat that has to be able to ask. It is the first tool added to
  that profile since it was fixed.
- **The read of a key is not a `next`.** The registry's rule is that a `next`
  makes progress or waits; a read that restates a refusal does neither. The
  two refusals name the read in their meaning instead.
- **The read of a key changes nothing**: an expired receipt reads `unused` and
  is not deleted, and a fenced one keeps its stored answer until a real retry
  purges it.

Found while building, and fixed before each was committed:

- **C7.** The first version guarded a case that cannot happen (a count of
  zero after a renewal); it was removed. Thirteen mutants are each caught.
- **D6.** One mutant survived the first tests: changing the ceiling of a
  budget that already existed. A test covers it; sixteen are now caught. A
  sentence in the budget document called a yield "a coordination write", which
  D6 made a metered term; it now says a yield is never metered.
- **A14.** The Budget row described one counter; it describes two.
- **W4.** A first wording added 190 bytes to the answer; it was cut to 113.
- **A11.** A declared response budget below the default is refused by the
  tool registry; the tool uses the default. Ten mutants are each caught.

Found and not fixed:

- **C7.** A holder whose work is all outside the issue (a long build in a
  repository) is raised at the threshold unless it comments. The count is
  read once per row of the active-work projection, up to 200 rows.
- **D6.** A seat out of coordination budget cannot comment to say so; it can
  still edit the issue. The operator learns of a spent coordination ceiling
  from the card and from the agent's last refusal, and the conductor from
  neither.
- **A14.** `SERVER_INSTRUCTIONS` does not mention the resources, because every
  session pays for that text. Copying `docs/` into the package is a manual
  `cp`, which the failing test names. The glossary test checks that a row
  exists and is filled, not that it is right. `OFFICE.md` says "at most one
  chair" where the code says it is guidance; the Office row follows the code
  and the document is not corrected.
- **W4.** `record_run_learning` is not in the `seat` or `conductor` profile:
  a reviewer on `seat+knowledge` can follow the `next`, a reviewer on `seat`
  cannot.
- **A11.** The read cannot say whether a retry would be refused
  `idempotency_mismatch`, which depends on the request. H14's operator view of
  a key is not built.

### Wave 5 results (fourth lanes) and Wave 6 results (second lanes)

This is W5, the last requirement of Wave 5, and H7 from Wave 6 (PR #480), one
commit each, both built by the lead, test first.

| Measure | Before | After |
|---|---|---|
| A learning recorded on one slice of an outcome | Reaches the next agent through that issue's backlinks only | The work-context packet of that issue and of each sibling under the same parent lists it, and a parent's learning reaches every child: `learnings`, newest first, at most five, each with `authored_by` and `advisory_untrusted` (W5, D22) |
| A bearer-authenticated read while another writer holds the database | Waits up to five seconds to stamp the token's last use, then 500 | Answers at once; the stamp is written at most once a minute and a due one is skipped after a quarter of a second (H7) |
| A write that waits five seconds for the lock | Unhandled error, 500 | 503 `busy` with `Retry-After` (H7) |
| Contract agent, two seats | 55 calls and 110,701 bytes | 55 calls and 110,771 bytes: the `learnings` lane on two work-context reads |
| Fleet trial | 88 calls and 233,853 bytes | 88 calls and 234,203 bytes: the `learnings` lane on ten work-context reads |

The product gate's receipt is in PR #480.

Decisions the lead took while building, each reversible by the owner:

- **A learning row is its event; its text is the runbook's.** No new table
  (D22). The recorder's event says who and when; the text is read from the
  runbook page and paired with its event by position, which holds while the
  page's learning blocks and its recorded learnings agree.
- **A runbook edited by hand gives no excerpt**, for every row of that page,
  and names the page. Words are never put under an author the trail does not
  support.
- **The parent's packet lists its own and its parent's learnings, not each
  child's.** The requirement names the issue, its parent and its siblings.
- **Siblings are read only through a parent the reader can see.**
- **The last-use stamp is advisory.** It never fails or delays a read: a due
  stamp that finds the database busy is skipped and tried on the next request.
- **`busy` is its own code**, beside `unavailable`: it says the request was
  not applied and can be sent again as it is.

Found while building, and fixed before each was committed:

- **W5.** A first version carried each issue's key and title; the title is
  peer-written text that would have needed its own author, and the key is on
  the hierarchy already. Rows carry `issue_id`. One mutant survived the first
  tests (an imported learning event counted); a test covers it, and twelve
  are now caught.
- **H7.** An `UPDATE` that changes no row still takes the writer lock, so the
  check for a due stamp is a separate read. Seven mutants are each caught.

Found and not fixed:

- **W5.** A learning does not reach other outcomes or the rest of a project.
  The brief packet (`brief=true`) does not carry the lane. The read examines
  the newest 50 learning events of the related issues, so `total` is a count
  among those. A runbook bound to two issues would be listed under both.
- **H7.** Other writes on a request path still wait the full five seconds:
  a refused agent request's last-refusal row (F15), a keyed write's claim.
  `last_used_at` is accurate to the minute, not the second. Whether a keyed
  write that answers `busy` keeps its key was not established.

### Wave 6 results (third lanes) and Wave 7 results (second lanes)

This is H9 from Wave 6 and H16, H17, H13, H15, H20 and H19 from Wave 7
(PR #481), one commit each, all built by the lead, test first.

| Measure | Before | After |
|---|---|---|
| Opening a connection to an existing database | 0.31 ms: every connection asked SQLite for WAL mode | 0.04 ms: two bytes of the file's header say it is already in WAL (H9) |
| The write-ahead log after a burst of writes | Kept its largest size for good (59.8 MB measured) | Cut back to 32 MiB at the next write after a checkpoint (H9) |
| The database file disappears while Athena serves | SQLite created an empty one; every request failed on a missing table | 503 `unavailable`, no file created, and `/readyz` logs why once per change of reason (H16) |
| The first token of an instance | Through the browser form only | `athena-token <db> <email> --name N --scope S` prints it once; the event says it was minted on the host (H17) |
| A second `athena-serve` on a port in use | Ran the webhook and automation loops, then failed to bind: 30 events gave 47 deliveries | Runs nothing: the loops start after the listener is bound (H13) |
| A second process on another port | Ran both loops beside the first, with no warning | Serves requests, logs one line saying it runs no loops, and `/readyz` reports `background_loops: standby` (H13, D24) |
| What the log says of one request | The web server's line: raw path and status | `request id=... actor=... method=... route=... status=... code=... ms=...`; the response carries the id as `X-Request-Id` (H15, D25) |
| An unhandled error in the log | One record with no request named and the traceback three times | One line with the request's id and one traceback; the 500 carries the same id (H15) |
| A webhook delivery that fails | Not logged | One `WARNING` per attempt, without the URL (H15) |
| Read notifications, expired sessions, expired idempotency receipts | Never removed, or only as a side effect | `athena-maintain <db>` removes them and prints how many; `docs/OPERATIONS.md` says what is kept and for how long (H20, D26) |
| What the runbook says about running a command, restoring elsewhere and upgrading | Bare command names; restore in place only; no upgrade section | "Running the Commands" (where the scripts live, all sixteen and what each does), "Restore onto a new host or path" and "Upgrade" (H19) |
| `RELEASE_READINESS.md` on the schema | "The schema is at migration 0074", eighteen migrations ago | Its counts are stated as those of each dated entry's evidence commit (H19) |

The product gate's receipt is in PR #481.

Decisions the lead took while building, each reversible by the owner:

- **The journal mode is read from the file every time, never remembered.** A
  first version cached the file's device and inode and was wrong when a
  deleted file's inode was reused.
- **The lock is an `flock` on `<database>.loops.lock`, taken once.** A
  process that did not get it does not take over when the holder exits;
  restarting it makes it the one that runs the loops. Takeover would be a
  second behaviour to operate, and D24 asks for "serve without them and say
  so".
- **`athena-serve` builds its own `uvicorn.Server`** so the loops start after
  the bind. An app started any other way (a test, the demo, direct uvicorn)
  starts its loops in its own startup as before, under the same lock.
- **The request log is the outermost layer and answers an unhandled error
  itself.** The framework's own outermost layer sits outside every
  middleware and would send a 500 with no id.
- **A caller's `X-Request-Id` is kept** when it is 1 to 64 of
  `A-Z a-z 0-9 . _ -`, so an agent can follow one request across systems;
  anything else is replaced.
- **The route's template is logged, never the raw path**, as the slow-request
  line already did. `athena-serve` turns off the web server's access line.
- **A read notification is kept 90 days after it was read**; the age is an
  option of the command.
- **The sweep a keyed write already did stays.** H20's "nothing in a request
  path deletes" is read as: nothing new does. Removing the existing bounded
  sweep would make the receipts table grow on an instance that never
  schedules the command.

Found while building, and fixed before each was committed:

- **H13.** `uvicorn.run` takes no hook for "bound", so the server is
  subclassed. Its exit status for a failed start is 3, which the wrapper
  keeps. Two mutants survived the first tests: a "start only once" guard
  nothing needed (removed) and the lock's no-symlink flag (now tested).
  The product gate then failed: the process smoke and the MCP launch smoke
  each compared the readiness answer with an exact body. Both now expect
  `background_loops`; a probe of an operator's own that compares the body
  exactly needs the same.
- **H19.** The restore sequence for a new host was run before it was written
  down. The runbook's opening still told an operator to turn both loops off
  by hand in a second process; since H13 the lock decides.
- **H15.** The framework answers an unhandled error outside every
  middleware, so the first version logged the request with no status and the
  500 had no id. The traceback's three copies came from a task group named
  as the error's cause; only a group that holds nothing but the error itself
  is dropped, and a cause the code named is kept. A refusal code that is not
  one word is not logged.
- **H17.** A removed account's email is anonymised, so "the account is
  removed" was unreachable; the command says no account has the email.
- **H20.** Rows are removed in row order so the table is walked once, and
  the count is what the delete removed, not what was selected.

Found and not fixed:

- **H13.** The lock is advisory and local: it does not hold across hosts or
  on a network filesystem without `flock`. A standby process does not report
  which process holds the lock.
- **H15.** A request no route matched logs no route, so the path an agent
  mistyped is not in the log. A streamed error body's code is read only
  while it is under 64 KiB. Lines are `key=value` text under the existing
  prefix, not JSON. The demo and a directly started app keep the web
  server's access line beside Athena's.
- **H16.** `athena-serve` already refused to start on a missing database
  without `--bootstrap`; that half of H16 was true before this.
- **H20.** Run bindings, approval requests, lease generations and webhook
  state are kept for ever and the document says so. The database file does
  not shrink when rows are removed. Nothing schedules the command: no timer
  unit ships.
- **H19.** No command installs or upgrades Athena; the runbook says to
  install the new release "the way this instance was installed". The other
  dated documents still quote old counts in the present tense (G9).
- **H14 is not in this pull request.** It is built and waits for the next
  one with H6.

### Wave 6 results (fourth lanes), Wave 7 results (third lanes) and Wave 8 results (second lanes)

This is H6 and H5 from Wave 6, H14 from Wave 7, and U14 and U2 from Wave 8
(PR #482), one commit each. H6 and H5 were built by a delegated builder from
the lead's brief and read, re-run and probed by the lead; H14, U14 and U2
were built by the lead.

| Measure | Before | After |
|---|---|---|
| `GET /spaces/{id}/pages` over 300 pages of 2,100 characters | 702,083 bytes: every page, with its body | 23,150 bytes: 100 summaries without bodies, and a cursor (H6) |
| Listing those pages through `list_pages` | 5 calls and 3,510,415 bytes of REST: the whole list on every call | 6 calls and 69,507 bytes: each row once (H6) |
| `GET /issues/{id}/comments` over 300 comments | 196,283 bytes: all of them | 32,698 bytes: 50, and a cursor (H6) |
| A comment longer than one tool answer | Could not be read whole from a seat | `get_issue_comment` reads it, continued by `body_offset` (H6) |
| An issue created between two pages of `GET /issues` | Repeats a row: the list paged by offset | Shifts nothing: the list pages by the last row's sort key (H6) |
| A retry of a keyed write whose process was killed | `idempotency_in_progress` for about a minute with no owner, then `idempotency_indeterminate` | `idempotency_indeterminate` as soon as a process starts serving the database alone (H14) |
| Keys whose outcome is unknown | Listed nowhere; settled only by editing the table | An admin lists them with the request and sender of each, and resolves one as applied or not applied; the trail records it (H14) |
| Mission Control's "Copy command" | Did nothing: its inline script was blocked | Copies and answers "Copied" (U14) |
| What a page's own policy blocks, in Chromium over eight pages | An inline style on every page (htmx's), an inline script on one, an inline handler on three | Nothing (U14) |
| An unassigned issue an agent has claimed, on the board | Under "Unassigned", with no sign of the claim | Under "Agent work", its card saying "claimed by" the holder; `?group=claim` gives one lane per holder (U2) |
| The issue list | An Assignee column | An Assignee column and a Claim column (U2) |
| The project brief's SQLite work at 50,000 issues | 10.6 M virtual-machine steps: every issue event grouped | 3.1 M: one seek into each issue's own trail (H5) |
| A full-text search for a word in most entries, at 50,000 issues | 3.29 M steps: every match scored, every visible source listed first | 0.08 M: the newest 1,000 eligible matches ranked (H5) |
| The idle sort at 50,000 issues | 8.0 M steps | 8.0 M: not bounded (H5) |
| Tools and routes | 143 tools (seat 27, seat+knowledge 38, conductor 40); 105 REST GET routes | 146 tools (seat 28, 39, 41); 107 |

The fleet trial's and the contract agent's ceilings did not move. The product
gate's receipt is in PR #482.

Decisions the lead took while building, each reversible by the owner:

- **The third list is `GET /issues`.** The finding names the page list and the
  comment list; the issue list was already capped at 100 rows, and what it
  lacked was a stable place to resume from. Its `offset` is retired and
  refused with 422, so an old caller is told and not served page one.
- **`get_issue_comment` is in the seat profile**: a seat could not read a long
  comment whole in any profile.
- **Bounds**: pages 100 by default and 200 at most, comments 50 and 200, and
  40 for the `list_issue_comments` tool, whose 50 excerpt rows overran its
  budget.
- **"Alone" is an exclusive lock on `<database>.serving.lock`**, which every
  serving process then holds shared. A process that cannot take it assumes
  nothing about another's keys.
- **A key resolved as applied keeps refusing, with its own code
  (`idempotency_applied`)**, so its sender learns the write landed and does
  not send it under a new key. It has no expiry, which needed no migration.
- **A key is resolved by its id and its text together**, and the resolution
  takes an `Idempotency-Key` of its own.
- **The decision is an event on the sender's account**, not on the admin's.
- **The two tools are in the full profile only.**
- **A card's lane follows whoever is working it**: the holder of the live
  claim when there is one, else the assignee. The third lane is "Unassigned
  and unclaimed".
- **A search ranks the newest 1,000 eligible matches** (`RANK_WINDOW`). A
  word in fewer entries is ranked exactly as before; for a commoner word
  the results change. The requirement allows "an index or a limit", and no
  index bounds a ranking.
- **The idle sort is left unbounded.** Bounding it needs a stored last-work
  time per issue kept by the recorder, and even that does not bound
  `is:open sort:idle-desc` unless the key also carries open or closed. That
  is a schema decision for the owner.
- **The content security policy is unchanged.** A style that depends on data
  stays a `<style>` element carrying the response's nonce, as the design
  system prescribes; scripts and handlers become behaviours of
  `static/confirm.js` asked for with a data attribute.

Found while building, and fixed before each was committed:

- **H6.** The work-context packet's comment cut pointed at
  `list_issue_comments` with an offset that no longer exists; it names
  `get_issue_comment`. A page cut by the response budget is asked for again
  with the rows that fit, so a tool never makes up a cursor; the path had no
  test until a mutant survived.
- **H14.** The list first answered `has_more` with no cursor, a paging style
  the conventions test refuses; it pages as every new list does. The word
  "indeterminate" had no glossary row.
- **U2.** Two mutants survived the first tests, which had no person holding
  a claim; a test now has one.
- **H5.** The brief said the work query's bare word was the full-text
  search. It is a `LIKE` that stops at its limit for a common word; the
  search that grew was the full-text one behind `/search` and `/find`. With
  statistics the planner chose another index for the brief, so the read pins
  its indexes and a plan test runs with and without statistics.
- **U14.** The first test of the script matched its comments; it now reads
  the code. The every-page test names each browser route that is not a page,
  so a new page is rendered without being listed.

Found and not fixed:

- **H6.** `GET /pages/{id}/comments` is still unbounded. `GET /issues` still
  returns whole bodies, so `search_work` still pulls about 115 kB from REST
  for 50 summary rows. `GET /issues/search` and `GET /search` still page by
  offset. A page title of about 6,000 characters makes a cursor longer than
  its cap, and the next page is a 422. A row whose sort key changes between
  two pages may still repeat or be skipped. An external client of the three
  lists breaks on the new shape; the changelog says so.
- **H14.** "Alone" is established only at start. With two processes serving,
  a key orphaned when one dies answers `idempotency_in_progress` until its
  lease runs out, and is then listed as `lease_expired`. The cockpit has no
  page for the list. Keys resolved as applied are kept for ever.
- **U2.** The board still clips at its card cap before it groups, so a lane
  can miss a holder's cards beyond the cap (U13). Saved filters and the
  dashboard still name the assignee alone.
- **H5.** The idle sort, as above. The brief is linear in the issues of its
  own project, and its completion rows cost twice their count. The search
  window is not mentioned in the `search` tool's description. One mutant
  survives: the transaction around the search's two reads.
- **U14.** `/docs`, `/docs/oauth2-redirect` and `/redoc` render blank: the
  framework's pages load from a CDN and start from an inline script. The
  behaviours in `confirm.js` are checked in a browser by hand; the suite has
  no script runner.

### Wave 8 results (third lanes) and G8

This is U4, U5, U8, U10, U13 and C10 from Wave 8 and the debt G8 (PR #483),
one commit each. U4 and C10 were built by a delegated builder from the
lead's brief and read, re-run and integrated by the lead; the rest were
built by the lead. Every cockpit change was driven in Chromium against a
seeded instance.

| Measure | Before | After |
|---|---|---|
| The Fleet page on a new instance | A "Roster drift" alert about eight seats the instance never had | No alert; "No seats are declared", and a form to declare one (U4) |
| An agent onboarded in the cockpit | Listed as drift, with nothing to do about it | Listed with a "Declare a seat" form on its row, its slug suggested (U4) |
| The declared seats | Eight entries in `core/fleet_roster.py` | Rows in `fleet_seats` (migration 0093); an admin declares, edits and removes them in the cockpit and over `/fleet/seats`, each write on the trail (U4) |
| A new token for an agent whose tokens were revoked | Not possible on any surface; onboard a second account | `POST /users/{user_id}/tokens`, and "Mint a token" in the agent's section; the trail names the admin as minter (U5) |
| Restoring a removed agent | The cockpit said "Restore via POST /users/{id}/restore" | A "Removed agents" panel on the same page, with Restore (U5) |
| Starting a playbook | REST and the tool only | "Start this playbook" on the page, saying how many steps it will make (U5) |
| Starting a playbook into a project that does not exist | 500 `internal_error`, on REST and the tool | 422 `validation_error` "no such project"; nothing created (found building U5) |
| An inbox row | `Sol claim_completed issue #12`: a raw verb and a target number | Under a panel headed by the issue's key and title: "Sol completed a claim on this issue" (U8) |
| Rows about one issue | Scattered in arrival order | One group, newest first, with a count of its unread rows; five kinds to filter by (U8) |
| The agents page with two agents, at 1,280 px | 2,896 px: every agent's whole detail open | 1,955 px: one shut disclosure per agent, with its own address (U10) |
| The outcome of an action on an agent | At the top of the page, naming no agent: "Paused: every authenticated action ..." | In that agent's section, which opens: "Paused Terra Reviewer: every authenticated action ..." (U10) |
| A column of 25 cards | 25 cards tall | 20 cards and "Show 5 more"; the count still says 25 (U13) |
| A command function only one transport reaches | Nothing said which were meant | The gate fails on one that `scripts/surface_parity.toml` does not declare with a kind and a reason; 42 are declared, in 28 rows (C10) |
| `seat+knowledge` and the notebook | Could edit a notebook and could not create one | Carries `open_my_notebook`; 40 tools, was 39 (G8) |
| A notebook longer than one tool answer, opened | Cut, with nothing naming how to read on | Cut, with the `get_page` call that continues (G8) |
| Tools and routes | 146 tools (seat 28, seat+knowledge 39, conductor 41); 107 REST GET routes | 146 tools (28, 40, 41); 108 |

The product gate's receipt is in PR #483.

Decisions the lead took while building, each reversible by the owner:

- **D27 as built (U4).** The migration tells an existing instance from a
  new one by whether the database already has a user. A seat's slug is its
  key and is never edited; removing a seat deletes its row and the trail
  keeps what it held. `kind` is a closed set of three. Seats have no MCP
  tool. The seats list over REST is admin only.
- **A token is minted for agent accounts only (U5).** A person mints their
  own; an admin holding a person's token would be acting as them. The
  agent's role is not changed, so a token for a read-only agent reads and
  nothing more, and the cockpit says so beside the token.
- **The mint has no MCP tool (U5).** REST and the cockpit are the two
  surfaces; a tool would put a raw credential into a model's context.
- **The mint takes no `Idempotency-Key`**, as every route that answers with
  a secret: a stored answer would hand the secret out again.
- **A restore brings back a read-only viewer with no token**, as the command
  always did. The section says the next two steps and offers the second.
- **Removed people are not listed on the agents page.** The cockpit cannot
  remove a person, so it has none to restore; REST can do both.
- **The agents page keeps every agent in one page (U10)**, as disclosures,
  not one page per agent: every control and fact is still in the page, and a
  deep link opens the one section it names.
- **An approval decision's outcome stays at the top of the agents page**,
  beside the approvals panel it was made in.
- **The inbox's kinds are five, fixed in the view (U8)**, and a verb the
  view does not know is said as itself with its underscores as spaces.
- **A board column pages with a disclosure, not a request (U13).** Every
  card stays in the page, so a drag and the keyboard move need no new
  parameter. A page is twenty cards.
- **C10 builds no twin.** Each one-sided command is declared as a pair, a
  read, one transport's by design, reached elsewhere, or a missing twin.
  `main.py` is neither transport. MCP is a client of REST and is not
  compared.
- **G8: the profile carries the tool**, where the alternative was the desk
  no longer saying how a notebook is made.

Found while building, and fixed before each was committed:

- **U5.** A playbook start let an issue write's refusal escape as a 500
  (above); it has its own commit and a REST test. Three mutants survived the
  first tests: the command's own admin check behind each transport's, the
  token line beside a config that also carries the token, and a refused
  restore whose redirect ended in the same 403. Each has a test.
- **U4.** A test that migrates through 0085 and then runs today's desk
  failed on the missing seats table; it now finishes the upgrade first. The
  builder's suites did not include it, and the wider run the lead made did.
- **U10.** The open section had no padding: `.panel-body` is styled only
  inside a panel. The row has its own body rule now.
- **U8.** The first rows showed a claim's generation hash, which the
  finding had named. Two mutants survived the first tests.
- **G8.** With the tool in a budgeted profile, the test that every budgeted
  tool answers within its budget had no step for it, and a grown notebook
  was cut with no reader; both are fixed in the commit.
- **C10.** On integration the check failed on the two rows U5 had just
  closed and on one new cockpit-only read. That is the check doing its
  job; the rows are gone and the read is declared.

Found and not fixed:

- **C10's three missing twins.** Assign-to-seat with the Buzz radio has no
  REST route; the cockpit cannot resume a claim handoff or edit a sprint.
  The check cannot see a command reached through a string, a registry
  filled at import, or `main.py`.
- **U4.** A seat declared with no email is a drift line ("no Athena
  handle"), as `glm` always was. A refused seat form loses what was typed
  (U11). The floor looks a seat up once per chair. One mutant survives: a
  cockpit route's admin check, behind which the command refuses again.
- **U5.** An admin can revoke all of an agent's tokens, not one of them. A
  refused mint form loses what was typed (U11). The cockpit
  cannot remove or restore a person.
- **U10.** The onboarding form still stands above the list of agents. At
  390 px the page overflows sideways (U15). An approval decision's
  outcome names no agent.
- **U8.** The inbox still loads 100 rows with no next page. "Mark read" is
  per row or for everything, not per group.
- **U13.** A column opened with "Show N more" closes when a move redraws the
  board. The whole board is still built and sent.
- **G8.** The conductor profile does not carry the notebook tool.

## Priorities

- **P0.** A written statement is false, or a fleet following the recipe is
  blocked. Fix before anything else.
- **P1.** What every session or every outcome pays.
- **P2.** Friction, hardening and growth.

Each baseline cell ends with how it was established: *(lead)* re-run by the
lead, *(reviewer)* executed by one reviewer and not re-run, *(read)* confirmed
by reading the code or the document.

## Track F: the fleet

| ID | Pri | Baseline | Required behaviour |
|---|---|---|---|
| F1 | P0 | The fleet trial is a script in a scratch directory. Its counts are in the report and nothing holds them. *(reviewer)* | `tests/test_fleet_trial.py` plays the trial in process: one conductor on `conductor`, three seats on `seat`, an operator on REST, following `CONDUCTOR_RECIPE.md`. It asserts every issue ends done and holds calls and bytes per role under ceilings in a tracked file, as the contract agent does. It has a scenario for each of C4, C5 and C6, marked unmet: its test passes while the scenario stops at the requirement's check and fails once the requirement holds, so the change that lands it removes the mark. |
| F2 | P0 | With the recipe's own links the desk shows one review request while `claim_next_ready` answers `nothing_ready`. After the reviewer's own HOLD and a second request, the review issue sits under `not_offered: completed_by_you`. A direct `claim_issue` works. FLEET-4. *(lead)* | A review request naming the caller, made by another account, is an answer under the ready rule. When the desk holds a review request for the caller, every `claim_next_ready` answer that claims nothing names it, with the call that claims its review issue. Needs D16. |
| F3 | P0 | `my_desk` returned 23,546 characters with 23 ready issues against a 16,000 budget. The Wave 1 results say every seat tool fits. PERF-2. *(lead)* | `my_desk` fits its budget at the ready lane's maximum, at the notification preview's maximum and with both, held by a test that fills each lane. The budget test for every seat tool builds its fixture from the declared lane maxima, not from one large issue. |
| F4 | P0 | `scripts/seed_benchmark.py` raises `TypeError`: Wave 2 made `commit` a required keyword. A document and a code comment call its numbers reproducible. PERF-3. *(lead)* | The script runs. A test runs it at its smallest size, and a test imports every file under `scripts/` that a document names as runnable and calls it with `--help`. |
| F5 | P0 | `POST /projects` with `visibility: private` answers 201 and creates a public project. 25 of the 67 JSON write bodies in the agent OpenAPI document accept unknown fields; `AGENT_GUIDE.md` says write bodies refuse them. FLEET-16. *(lead)* | Every JSON write body in `/openapi-agent.json` refuses an unknown field with 422 `validation_error`, as the closed bodies do today. A test walks the document and fails on a body that does not. `POST /projects` accepts `visibility`, or refuses it. |
| F6 | P1 | Any change to any issue's assignee, project or creator moves one workspace-wide authorization revision. Every stored receipt is then purged; its key answers 409 `idempotency_authorization_changed` and its marker never expires. 30% of receipts ended that way in a sustained run. The breadth is documented as deliberate in `OPERATIONS.md`; a fleet assigns work all day. PERF-1. *(lead)* | An issue's assignment or creator changing fences receipts for that issue only. Role, membership, visibility, pause and placement changes still fence everything. A fenced marker expires when its receipt would have. The refusal names how to reconcile in `next`. Needs D17. |
| C6 | P0 | On an instance with no root catalog, which is the default, a second lease declaring any path is refused 409 `conflict`, whatever the path, across projects, and for a holder the caller cannot see. `claim_next_ready` takes no paths. With a catalog, the selector needs a `catalog_sha256` no API returns. FLEET-1. *(lead)* | Without a catalog, paths are compared lexically under one implicit root and the basis is reported as "lexical, root unknown" (D5). A refusal is `fence_overlap` and names the overlapping path, and the holder's issue when the caller can see it. `claim_next_ready` accepts `paths`. The office reports the comparison basis. `OFFICE.md` states all of it. As first written this row also made the catalog digest readable; that was withdrawn, because `ACTIVE_WORK.md` deliberately gives no capability hint about the catalog. |
| C4 | P0 | A 60-second lease lapses. No event, no notification, no attention row for the assignee or the conductor who delegated: four wait timeouts over 100 seconds showed nothing. FLEET-2. *(reviewer)* | As specified under "C4: failure is an event" in the first specification, with D11: one `lease_lapsed` event per generation, written by the first request that observes it, a notification to the assignee and the delegating account, and an attention row for both. |
| C5 | P0 | The requested reviewer reports HOLD. An unrequested account completes an unrelated issue naming the candidate with PASS; the queue reads `reported_pass`. FLEET-3. *(lead)* | As specified under C5 in the first specification, with D4: the request row carries what the requested reviewer reported, in a field no other account can move. The conductor recipe gates on that field. |
| C9 | P1 | Setting up one outcome with three slices takes 21 conductor requests, four of them reading a 5.9 KB packet only for an ETag. `create_issue` takes no parent, assignee, delegate or blocker; `set_issue_parent` changes the ETag and does not return it. FLEET-6. *(reviewer)* | `create_issue` accepts `parent_id`, `assignee_id`, `delegate_ids` and `blocked_by`, applied in one transaction. Every write that changes an issue's ETag returns the new one. The recipe's setup is at most 6 requests. |
| F7 | P1 | 39 of 142 calls acknowledge one notification each. A conductor had 23 unread after one outcome. Each lease renewal notifies the assignee. No seat profile can acknowledge a set. FLEET-5. *(reviewer)* | `mark_notification_read` accepts a list of ids or `through_id`. A lease renewal is an event and not a notification. Needs D18. |
| F8 | P1 | `wait_for_events` reads an instance-wide feed with no filter: a conductor was woken by 19 events from two unrelated projects. An event carries the verdict, the lease generation and the handoff token only inside a prose `detail`. FLEET-7. *(reviewer)* | The feed and the wait take `project_id`, and `mine=true` for issues the caller is assigned, delegated or created. An event carries its verdict, generation and handoff token as fields. |
| F9 | P1 | A conductor cannot resolve an account id: its tool text names `list_users`, which its profile lacks and a member is refused. The answers of `delegate_issue` let a member enumerate accounts through a write. Onboarding prints the `seat` profile for an account meant to conduct. FLEET-8. *(reviewer)* | A read on the `conductor` profile lists the accounts that may be assigned or delegated on a given issue: id, name and whether it is an agent. `delegate_issue` answers the same for an account that does not exist and one that is not eligible. Onboarding takes the profile to print. Needs D19. |
| F10 | P1 | Any eligible account can replace an issue's title and body with no precondition. The old text is in no event, history or undo. A seat that has lost its lease can still edit. The recipe keeps the contract and acceptance criteria in issue bodies. FLEET-10. *(lead)* | A change to an issue's title or body stores the prior text, readable through the issue history and restorable by undo. A bearer-token caller must send `If-Match` to change either. `update_issue` stops being marked destructive. Needs D20. |
| F11 | P1 | Refusals carry their facts in prose: `claim_held` names the holder and expiry only in `detail`, and its `next` answers `nothing_ready` for a seat with one delegation. FLEET-15. *(reviewer)* | A refusal that names a holder, an expiry, a generation or a limit carries each as a field. A test follows every registered `next` from the state that produces its refusal and fails on one that cannot make progress. |
| F12 | P1 | Tool results are sent as indented JSON; about 30% of each budget is whitespace. *(build agent's measurement in Wave 1, not re-run; Wave 4 measured 16% on the budget fixture and 25 to 29% on uncut answers)* | Tool results are compact JSON. The budget is unchanged, so each answer carries more. |
| C8 | P1 | Pause, token revocation and offboarding leave the account's lease and fence active and say nothing about them. An admin's `complete` on a dead agent's expired lease is refused `claim_required`; the admin has to claim it first. FLEET-9. *(reviewer)* | As specified under C8 in the first specification, with D12: the three acts return the account's active leases and may release them as an explicit option, recorded per lease. An admin can settle an expired lease without claiming it. |
| A12 | P1 | Carried unchanged. Renewing a lease needs a fresh ETag as well as the generation. *(reviewer)* | As in the first specification. |
| A13 | P1 | Carried, and wider than recorded: an agent packet uses three timestamp shapes and the agent OpenAPI document has six paging styles. *(reviewer)* | As in the first specification: agent packets emit one timestamp shape, and new endpoints page in D2's vocabulary. A test over `/openapi-agent.json` holds the timestamp format at one and freezes the paging styles that exist, so only D2's can be added. Existing endpoints keep their paging. |
| C7 | P2 | Renewing and heartbeating without progress is derived as `pinging` and never raised. FLEET-12. *(reviewer)* | As in the first specification, with D6. |
| F13 | P2 | A budget exhausted mid-task gives the seat no `next`, the accountable conductor is not told, and the operator gets one row per refused request. FLEET-13. *(reviewer)* | The refusal's `next` is a yield with reason `capacity`. The assignee and the delegating account are notified once per window. The operator's attention list has one row per account and window. |
| F14 | P2 | A restarted MCP session cannot replay its own key: `athena-mcp` mints a run id per session unless `ATHENA_RUN_ID` is set, and the key answers 409 `idempotency_mismatch`. FLEET-14. *(reviewer)* | The run id is not part of what a key is matched on, or the printed configuration sets a stable one. The guide says which. |
| F15 | P2 | A seat that is refused or finds nothing ready leaves no trace the operator can read. The attention vocabulary has no signal for a HOLD or BLOCKED verdict. FLEET-11. *(reviewer)* | A HOLD or BLOCKED report raises an attention row for the assignee and the delegating account. The operator can read, per agent, its last refusal code and when it last found nothing ready. |
| A11, A14 | P2 | Carried unchanged. *(reviewer)* | As in the first specification. A11's read of a key's state is also what H14 gives the operator. |

### F1: the trial is the instrument

The contract agent holds one seat to the documented loop. The fleet trial does
the same for the recipe. It is a test and not a benchmark: it counts calls and
bytes, which are deterministic, and never time. Its ceilings file starts at
what the test measures in process, 142 calls and 359,252 bytes, and is lowered by each requirement in
Wave 4 that removes calls, in the same commit, so the scorecard row moves in
the diff.

Agents in the trial are scripted. A real model will spend more, and the
ceiling says only what the recipe costs when followed exactly.

### F2: what the desk asks, pickup names

The recipe links a review issue as `blocked_by` the build issue, and the build
issue stays open while it is under review, so the review issue is never ready
under the ready rule. The recipe's reviewer claims it directly. That is
coherent, and it leaves two gaps: `claim_next_ready` says `nothing_ready`
without saying a review is waiting, and a reviewer who completed a HOLD is
held back from the same issue by the completed-by-you clause when the
conductor asks again.

The requirement leaves the `blocked_by` semantics alone. It adds one verb to
the answers the ready rule recognises, and one field to an answer that claims
nothing.

### F6: narrow the fence, keep its direction

The fence fails closed and should go on doing so. What changes is which acts
move it. An assignment is the most frequent act a conductor makes, and it
cannot change what any account may read on a different issue. Role,
membership, visibility, pause and placement changes can, and stay global.

Read the reasoning in `OPERATIONS.md` under idempotency before building. If
per-issue scoping cannot be shown to preserve the guarantee that a replay
never returns a response its caller could no longer obtain, build only the
expiry and the `next`, and record that outcome.

## Track W: the work order

The owner supplied a prompt-writing course and asked whether its ideas apply.
Three do. The rest is advice for people typing into a chat window and has no
place in a product specification.

| ID | Pri | Baseline | Required behaviour |
|---|---|---|---|
| W1 | P1 | Build agents were briefed in free prose, different each time. *(lead)* | A committed brief template in six parts: goal, context, inputs, action, format, success criteria. **Done in the change that adds this document**: [BUILD_BRIEF_TEMPLATE.md](BUILD_BRIEF_TEMPLATE.md). |
| W2 | P1 | The state of the work was spread over three results sections, two decision records and a session summary. *(lead)* | One page states the objective, the decisions in force, the known issues, what is already fixed and the next step. **Done**: "State of the project" above. It is updated in each wave's results commit. |
| W3 | P1 | An issue is a title and a free-text body. The recipe keeps the contract and the acceptance criteria in the body, and nothing tells a seat that an issue has no success criteria before it claims. *(read)* | An issue template with the six parts as headings. The work-context packet reports which parts the body has and which it lacks. Nothing is refused for a missing part. Needs D21. |
| W4 | P2 | A HOLD review says what is wrong with one candidate. Nothing asks what rule would have prevented it. `RUN_LEARNINGS.md` requires promotion to be explicit. *(read)* | The answer to a HOLD or BLOCKED report names, in `next`, the call that records a learning. The conductor recipe asks for one rule per HOLD. Nothing is promoted automatically. |
| W5 | P2 | A learning is bound to one issue's runbook and reaches the next agent only through that issue's backlinks. A rule learned on one slice does not reach its sibling. *(read)* | The work-context packet lists learnings recorded on the issue, its parent and its siblings under the same parent: newest first, inside the packet's budget, each with `authored_by` and marked as data. Needs D22. |

A learning stays what `RUN_LEARNINGS.md` says it is: quoted, attributed,
verified, never executed and never read as an instruction.

## Track H: operate and scale

| ID | Pri | Baseline | Required behaviour |
|---|---|---|---|
| H1 | P1 | `GET /desk` for a seat scans the whole trail: one statement, the latest visible event id, takes 15, 171 and 840 ms at 1,000, 10,000 and 50,000 issues. Asked one visibility arm at a time it takes 0.02 ms and returns the same id. `/desk/since` without a cursor has the same shape. PERF-4. *(lead, at 10,000)* | Both ask per arm. A test asserts the plan of each hot statement names no scan of `activity`. |
| H2 | P1 | Every issue create and comment scans the full-text index inside the write transaction (0.7, 9.1 and 48.9 ms), because the columns it deletes by are not indexed. PERF-5. *(reviewer)* | The delete finds its rows by an indexed key. Needs a forward-only migration. |
| H3 | P1 | The ready rule reads every issue that is not archived, closed ones included; `claim-next` does so holding the writer lock (4.5, 35 and 156 ms); the desk runs the rule four times. PERF-6. *(reviewer)* | The rule reads open candidate issues only, and the desk evaluates it once per request. |
| H4 | P1 | Notifications are never deleted and every inbox read walks all of a user's rows: the operator's priority inbox takes 33, 357 and 1,875 ms. PERF-7. *(reviewer)* | Inbox reads and the unread count use an index over unread rows. Read notifications older than a set age are removed by the maintenance command (H20). |
| H5 | P2 | The project brief, a search for a common word and the idle sort are linear in history. PERF-8. *(reviewer)* | Each is bounded by an index or a limit; the brief reads the latest completion per issue without walking every event. |
| H6 | P1 | Three REST lists return everything: the page list was 2.65 MB for 1,250 pages and `list_pages` fetches it again per continuation (31 calls, 82 MB); the comment list has no limit; no route reads one comment. PERF-9. *(reviewer)* | The three lists page with D2's `cursor`, `next_cursor` and `has_more`. The page list omits bodies. A route and a tool read one comment whole. |
| H7 | P2 | Every bearer-authenticated read takes the writer lock to stamp the token's last use. A lock wait beyond five seconds is a 500. PERF-10. *(reviewer)* | The stamp is written at most once a minute per token. A lock timeout answers 503 `busy` with `Retry-After`. |
| H8 | P1 | The numbers in the report came from scripts in a scratch directory. *(lead)* | A committed seeder and benchmark at three sizes, with the 10,000-issue medians in a tracked file. The gate does not time anything; H1's plan tests are what it holds. |
| H9 | P2 | A connection per request sets the journal mode each time (2 to 3 ms), and the write-ahead log reached 59.8 MB with no size limit. PERF-13. *(reviewer)* | The journal mode is set once per database. A journal size limit is set and documented. |
| C11 | P1 | With defaults, 74 of 156 GET routes answer without a credential: comments, attachment bytes, lease generations, the work-context packet, the activity CSV. The anonymous rate limit defaults to off. `athena-doctor` says nothing about it. OPS-1. *(reviewer)* | As in the first specification, with D7. In addition `athena-doctor` prints the setting, the count of anonymous routes and the public containers, and the first start logs one line saying so. Needs D23 for the default. |
| H10 | P1 | The documented bootstrap leaves the data directory 755 and the database 644. The database holds webhook secrets, CSRF tokens and password hashes. OPS-2. *(reviewer)* | Bootstrap creates the directory 700 and the database 600. `athena-doctor` warns on a wider mode. |
| H11 | P0 | `docs/DOCKER.md` gives `athena-backup` one argument where it needs two. `compose.yaml` does not forward the bootstrap token its own comment says to pass. The first-admin step points at a host `curl` with no published port. OPS-3. *(lead: the backup command run, the compose file read; nobody ran a container)* | The three are corrected. A test parses every CLI invocation shown in `DOCKER.md` and `OPERATIONS.md` against the command's own argument parser. |
| H12 | P1 | A database whose schema differs refuses to start and names no object. The migration-state refusals state the condition and not the remedy, and no document mentions them. OPS-4. *(reviewer)* | Each refusal names the object or file and the command that resolves it. `OPERATIONS.md` has a section listing them. |
| H13 | P1 | A duplicate `athena-serve` start runs the background loops before it fails to bind: 30 events gave 47 webhook deliveries. A second process on another port starts with no warning. OPS-5. *(lead)* | The loops start after the bind succeeds, and only in the process that holds an exclusive lock beside the database. A process without it serves requests, logs one line saying so, and `/readyz` reports which. Needs D24. |
| H14 | P1 | After a hard kill, an in-flight keyed write answers `idempotency_in_progress` for about a minute with no owner, then `idempotency_indeterminate` for ever. No route or tool lists or resolves such keys. OPS-6. *(reviewer; the no-expiry rule read by the lead)* | At start, keys left executing by a process that no longer exists become indeterminate at once. An admin route and tool list indeterminate keys with the request each belongs to, and resolve one as applied or not applied, recorded in the trail. |
| H15 | P1 | The log cannot answer what an agent did and what failed: no timestamp, request id, actor or duration on an access line; no correlation id on a 500, whose traceback prints twice; a failed webhook delivery is not logged. OPS-7. *(reviewer)* | One structured line per request: time, request id, actor id, method, route, status, refusal code, duration. Every response carries `X-Request-Id`. A 500 logs once. A failed delivery logs once. Needs D25. |
| H16 | P2 | `/readyz` fails with no reason logged. A missing database file is silently recreated empty. OPS-8. *(reviewer)* | The reason is logged. `athena-serve` refuses to start on a missing database unless `--bootstrap` is given. |
| H17 | P2 | The first token can be minted only through the browser form. OPS-9. *(reviewer)* | A command on the host mints a token for a named account and prints it once. |
| H18 | P2 | Every start logs "set `ATHENA_COOKIE_SECURE=1` for HTTPS", and `athena-serve` refuses that setting. A misspelled `ATHENA_` variable is ignored. OPS-10. *(reviewer)* | The line is corrected. An `ATHENA_` variable that the settings registry does not declare is named in one warning at start. |
| H19 | P2 | `OPERATIONS.md` uses bare command names, covers restore in place only, and has no upgrade or logs section. `RELEASE_READINESS.md` says the schema is at migration 0074; the code has 85. OPS-11. *(reviewer)* | The four are added. The migration count is generated or removed. |
| H20 | P2 | Nothing documents growth or retention. Notifications, run bindings and approval requests are never pruned; expired sessions go only on logout. OPS-12. *(reviewer)* | `athena-maintain` removes read notifications, expired sessions and expired idempotency markers past a documented age, and prints what it removed. Nothing in a request path deletes. The activity trail and its chain are never pruned. Needs D26. |

Recorded and not required: one process saturates at about four concurrent
seats (PERF-11), and the automation engine drains ten events a second
(PERF-12). At the shipped rate limit of 120 requests a minute per token, a
fleet of ten reaches the limiter first. Both are limits of the single-process
design, which this specification keeps.

## Track U: the operator's cockpit

Read [`docs/DESIGN_SYSTEM.md`](../DESIGN_SYSTEM.md) before any of these.

| ID | Pri | Baseline | Required behaviour |
|---|---|---|---|
| U1 | P0 | The issue page's "Claim" card shows the assignee: it read "unclaimed" while an agent held a live lease. `DESIGN_SYSTEM.md` says "Claim, not assignment". COCKPIT-5. *(read by the lead; observed by the reviewer)* | The card shows the lease: holder, expiry and when it was last claimed or renewed, or that there is none. A lease row keeps no time for when its generation began, so that is not shown. The assignee is shown as the assignee. |
| U2 | P1 | Boards and the issue list filed 16 of 18 agent-worked issues under "Unassigned". COCKPIT-5. *(reviewer)* | Wherever a view groups or labels by assignee, a held lease is shown too, and a view can group by holder. |
| U3 | P1 | The operator cannot release another agent's live lease in the browser, so reassigning claimed work does not move it. The same admin can over REST. COCKPIT-1. *(reviewer)* | The issue page offers the release to an admin, with the confirmation the design system prescribes, through the same command as REST. |
| U4 | P1 | The Fleet roster and "Assign to a seat" work only for eight seats written into `core/fleet_roster.py`. An empty instance opens with a "Roster drift" alert, and agents onboarded through the cockpit are listed as drift. COCKPIT-2. *(read by the lead)* | Declared seats are data, managed from the cockpit and over REST. An instance with none shows no alert. Needs D27 and a forward-only migration. |
| U5 | P2 | A playbook cannot be started from the browser. A removed agent cannot be restored there. An existing agent cannot be given a new token on either surface. COCKPIT-3, COCKPIT-4. *(reviewer)* | Each can be done in the browser. A token can be minted for an existing agent on both surfaces. |
| U6 | P1 | Login lands on a page with no attention on it. The navigation badge and the dashboard show different "needs you" totals. COCKPIT-6. *(reviewer)* | Login lands where the attention queue is. One read model gives both numbers. |
| U7 | P1 | 54 of 54 fresh claims read "needs attention"; a claim with no heartbeat is "high" at once; the one pending approval sat at row five; rows are labelled in internal codes and name "issue #12". An answered question stays "critical" until the agent resumes. COCKPIT-7, COCKPIT-9. *(reviewer)* | A claim is not attention until a heartbeat is overdue. A pending decision outranks a missed check-in. Rows use words, the issue key and its title. A question with an answer newer than it reads "answered, waiting on the agent". |
| U8 | P2 | The inbox held 34 unread rows after about 15 agent actions, as raw verbs, hashes and whole reports as JSON, with no filter or grouping. COCKPIT-8. *(reviewer)* | Rows group by issue, filter by kind, and render a report through the shared partial of U9. |
| U9 | P1 | An approval row does not say what is being approved. A completion report is raw JSON on five surfaces. Undo lives on the activity page only. COCKPIT-11, COCKPIT-12. *(reviewer)* | An approval row carries the issue key, its title and the verdict. One partial renders a report everywhere. The issue page offers undo for its own latest undoable event. |
| U10 | P2 | The agents page is 7,500 px tall; every action reloads to the top with a message that names no agent. COCKPIT-10. *(reviewer)* | One agent, one page or one anchored section. An action returns to where it was made and names the agent. |
| U11 | P2 | Errors from plain form posts are bare unstyled pages. A comment submitted after the session expired is lost. COCKPIT-13. *(reviewer)* | A form error renders in the page that posted it. A comment draft survives a login. |
| U12 | P2 | Only three panels update live. COCKPIT-14. *(reviewer)* | The navigation badge and the issue page update with the same mechanism. |
| U13 | P2 | With 226 issues the board was 41,477 px tall with no paging. COCKPIT-15. *(reviewer)* | Each column pages. |
| U14 | P1 | Mission Control's "Copy command" does nothing: its inline script is blocked by the page's own content security policy. Every page load logs one blocked inline style. COCKPIT-16. *(reviewer)* | No template carries an inline script, style or handler. A test renders every page and fails on one. |
| U15 | P2 | Layout breaks on long titles and at 390 px; the default button uses the accent reserved for "an agent is waiting on the human"; developer notes ship in the interface; there is no skip link; empty pages give no next step. COCKPIT-17 to 20. *(reviewer)* | Each is corrected against the design system's own contract. |
| C10 | P2 | Five capability groups exist only in the cockpit and six only in the API. *(reviewer)* | As in the first specification: each gains its twin or a row on a declared list, and a gate check fails on an undeclared one. |

## Track G: debts

Each was listed by an earlier wave under "found and not fixed".

| ID | Pri | Debt | Required behaviour |
|---|---|---|---|
| G1 | P2 | 42 commands take a bare actor id and 23 writes sit outside a command. | A ratchet on both counts in `COMMAND_MIGRATION.md`. Every later wave that touches a debtor pays it. |
| G2 | P2 | Writers with no `commit` parameter commit unconditionally; `start_sprint` and `update_page` open `BEGIN IMMEDIATE` by hand. | Each takes `commit` or goes through `db.transaction`. |
| G3 | P2 | Imported comments and pages name the local account the import matched. | Imported rows are marked by a forward-only migration and `authored_by` says so. |
| G4 | P2 | JSON refusals from the cockpit's own routes carry no registered code, and the source check does not cover `web/`. | They do, and it does. |
| G5 | P2 | The agent OpenAPI document omits the scope each operation needs, puts 404, 428 and scope denial under `default`, and costs 2.3 to 2.5 seconds on first request. | Scope per operation; the three statuses per route; built once at start. |
| G6 | P2 | The app has no injectable clock; tests lapse a lease by rewriting its stored expiry. | One clock, replaceable in tests. C4's tests use it. |
| G7 | P2 | The changed profile picks tests that reach changed code only through the app from a 17-entry map of file names. | A scheduled CI job records per-test coverage contexts and the map is generated from them. |
| G8 | P2 | `seat+knowledge` can edit a notebook and cannot create one. | The profile can do both, or the desk stops advertising it. |
| G9 | P2 | `field_guide/07_watching.md` and `DESK.md` name tools no seat profile carries; `examples/desk_loop.md` is not executed; `DISPATCH.md` and `WORKERS.md` keep hand tables of settings; `AGENT_API.md` calls `/openapi.json` the full reference; dated documents quote old coverage floors. | Each is corrected, generated or executed. |
| G10 | P2 | `POST /issues/bulk` reports a wrong status without `valid_statuses`; list, search and learning reads carry no `authored_by`. | Both are closed. |
| B6b | P2 | Carried unchanged. | As in the first specification. Setting a verdict stays the owner's act. |
| B11 | P2 | Carried unchanged, and blocked: D9 waits on somebody operating an instance the lanes can reach. | As in the first specification, when unblocked. |

## Waves

These replace Waves 3 and 4 of the first specification. One requirement ID is
one change with its own commit, in the order given.

| Wave | Content | Uses decisions |
|---|---|---|
| 3. True again, and the fleet unblocked | F1, F2, F3, F4, F5, H11, U1, C6, C4, C5 | D4, D5, D11, D16 |
| 4. What an outcome costs | C9, F7, F8, F9, F11, F12, A12, A13, W3 | D18, D19, D21 |
| 5. Failure and recovery in a fleet | F6, F10, C8, C7, F13, F14, F15, A11, A14, W4, W5 | D6, D12, D17, D20, D22 |
| 6. Scale | H8, H1, H2, H3, H4, H6, H5, H7, H9 | D2 |
| 7. Operate | C11, H10, H12, H13, H14, H15, H16, H17, H18, H19, H20 | D7, D23, D24, D25, D26 |
| 8. The cockpit | U2, U3, U4, U6, U7, U9, U14, U5, U8, U10, U11, U12, U13, U15, C10 | D12, D27 |
| 9. Debts | G1 to G10, B6b, B11 | D9, D10 |

Waves 3 and 4 are done, and Waves 5, 6, 7 and 8 are begun; their results are above. Wave 3 is the six false
statements and the three findings that block a fleet following the recipe. Wave 4 is where the per-outcome scorecard row moves.
Waves 6 to 9 do not depend on each other and can be reordered.

## Compatibility and deliberate exclusions

- Everything under this heading in the first specification stands.
- No lease is swept or released automatically. C4 records a lapse; it releases
  nothing. C8 and U3 release only on an operator's explicit act.
- The ready rule's treatment of `blocked` and `capacity` yields, and of a
  direct claim by the account that settled, is unchanged (D28).
- `review_state` and the behaviour of unrequested reviews are unchanged.
- The idempotency fence still fails closed. F6 changes which acts move it.
- The default for `ATHENA_ANONYMOUS_READS` does not change here (D23).
- No read returns the root catalog's digest, root keys or contents. A selector
  comes from the operator, as before.
- No metrics endpoint, no second process, no connection pool.
- No approval covers more than one action. A gated conductor closing an
  outcome and three slices still needs four decisions (FLEET-17); U9 makes
  each row say what it is.
- Nothing is promoted to a learning automatically (W4).
- Migrations stay forward-only: H2, U4, F10 and G3 each add one.
- From the course, only W1 to W5 are taken.

## Decisions for the owner

On 2026-11-22 the owner told the lead to go with its recommendations for this
specification and to default to them at stopping points. Each recommendation
below is therefore recorded as accepted, and each is the owner's to reverse.
The record is
[decisions/2026-11-22-athena-in-use-decisions.md](../decisions/2026-11-22-athena-in-use-decisions.md).

| ID | Decision | Recommendation (accepted by standing instruction) |
|---|---|---|
| D16 | Is a review request naming an account an answer to that account's own completion, under the ready rule? This amends the 2026-11-22 ready-rule record. | Yes, when another account made the request. `blocked_by` keeps its meaning; pickup names a waiting review and does not claim it. |
| D17 | May an issue's assignment or creator changing fence only that issue's receipts, and may a fenced marker expire? `OPERATIONS.md` documents the global fence as deliberately broad. | Yes to both, if the build can show a replay never returns what its caller could no longer obtain. Otherwise only the expiry. |
| D18 | Should a lease renewal stop notifying the assignee, and should one call acknowledge many notifications? | Yes to both. The renewal stays an event. Extend the existing tool; add none. |
| D19 | May a member read the accounts eligible for assignment or delegation on an issue it can see? | Yes: id, name and whether it is an agent, for that issue only. It replaces an enumeration that a write already allows. |
| D20 | Should issue title and body edits keep the prior text, and should a bearer-token caller need `If-Match` to make one? The second can refuse a caller that works today. | Yes to both. The browser's forms are unaffected. |
| D21 | Should the six parts of a work order be columns, or headings the packet recognises? Should a missing part refuse anything? | Headings, reported by the packet. Nothing is refused. |
| D22 | Should a learning reach issues other than the one it was recorded on? | The parent and siblings under the same parent only. No new table and no project-wide scope yet. |
| D23 | Should `ATHENA_ANONYMOUS_READS` default to off? | Not here. It would change every existing instance. Report the state loudly (C11) and decide the default with the next tagged release, which is the owner's. |
| D24 | When a second process starts on the same database, should it refuse, or serve without the background loops? | Serve without them and say so. Refusing would break a deliberate second reader. |
| D25 | Structured request logs and a request id on every response; a metrics endpoint? | The first two. No metrics endpoint: it is a new unauthenticated surface, and the log answers the question asked. |
| D26 | May a maintenance command delete read notifications, expired sessions and expired idempotency markers? | Yes, as an explicit command the operator schedules, with documented ages. Never the trail. Ruling K1, on leases, is untouched. |
| D27 | Should the declared fleet seats move from source to data? | Yes. An existing instance keeps its eight by migration; a new one starts with none. |
| D28 | Should `blocked` and `capacity` yields hold work back from the account that yielded, and should a direct claim by the settling account count as an answer? | No change to either. The accepted ready-rule record stands until the fleet trial shows a loop. |

Six questions stay with the owner and are not answered here: what the name
"Astra" refers to in the lanes; whether the fallback rules in `AGENTS.md`
match the machine-level handbook; D9's precondition, an operated instance;
whether an agent may read the root catalog's digest and root keys, which
`ACTIVE_WORK.md` withholds pending operator evidence; whether assignment
should refuse an account that cannot see the issue's project (today it
succeeds and answers with the account's name, which Wave 4's eligible-accounts
read does not list); and whether an admin needs a way to remove a kept prior
text (F10 keeps what an edit replaced, so editing a secret out of an issue no
longer removes it for those who could see that edit).

## Verification and delivery

The standing constraints of the first specification are unchanged.

For each requirement: write the failing test from the baseline cell and
confirm it fails on `0b42d40`; make the change; run
`scripts/gate.sh --profile changed` while working and
`scripts/gate.sh --profile product` on the wave's head; record the receipt in
the PR. For Track F, add or extend a scenario in the fleet trial once F1
exists, and lower its ceilings in the same commit when the change removes
calls. For Track H, a statement that was slow gets a plan assertion. For Track
U, read the design system first and attach a screenshot of the changed page
at 1,280 and 390 px to the PR.

A requirement is done when the Definition of Done in `AGENTS.md` holds and, in
addition, its test fails without the change and passes with it, its scorecard
row, if it has one, was re-measured, the documents that describe the behaviour
were updated in the same PR, and "State of the project" says so.
