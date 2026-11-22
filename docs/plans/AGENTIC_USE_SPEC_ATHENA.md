# Agentic use: review findings and improvement specification

Date: 2026-11-21. Review baseline: `99a0286ba12c7b9250348a61819827ce943f01b9`.
Status: **decisions accepted; Waves 0, 0b, 1 and 2 built.** On 2026-11-22 the
owner accepted all fifteen decisions as recommended and authorized Wave 0, then,
once each had merged, Wave 0b, Wave 1 and Wave 2. See
[the decision record](../decisions/2026-11-22-agentic-use-decisions.md). Wave 0
landed in PR #466 and its results are under "Wave 0 results" below. Wave 0b
(A2, under D3) landed in PR #467; its rule is recorded in
[its own decision record](../decisions/2026-11-22-ready-rule-awaiting-decision.md).
Wave 1 (A4 to A10, A15b, A16 and A17) landed in PR #468 and its results are
under "Wave 1 results" below. Wave 2 (B2 to B5, B6a and B7 to B10) is built in
PR #469 and its results are under "Wave 2 results". Waves 3 and 4 are not
built; they remain proposals for when to build, not whether the design holds.
Several requirements change or extend an earlier accepted decision; each says
so and names the decision it relies on under "Decisions for the owner".
Evidence: [reports/repo-review-2026-11-21.md](reports/repo-review-2026-11-21.md).
Finding IDs such as `MCP-1` refer to that report.

## Purpose

Make Athena a workspace where an agent that follows the written rules always
has a next move, cannot overrun its context, and can prove what it did. That
has to hold for three kinds of user:

- **The seat.** An agent working *through* Athena over MCP or REST: read the
  desk, claim, work, report, hand off.
- **The builder.** A coding agent working *on* Athena: branch, change, run the
  gate, open a PR.
- **The fleet and its operator.** Several seats and builders at once, with one
  person supervising.

This is a specification of required behaviour and its acceptance tests. It
does not add a queue, an orchestrator, a state store or a workflow engine.
Athena's ownership model, its single-process SQLite architecture and its
visibility rules stay as they are.

## Assessment

The primitives are right and should be kept: generation-fenced leases, a
one-call atomic pickup, idempotent writes, a bounded work-context packet with
explicit clip markers, tool surfaces narrowed by scope and profile, liveness
that separates holding from working, and lease refusals that name the next
call. The review exercised these and found them working; the report's "What
held" section says which were re-run by a second reviewer.

What is missing is one thing. **Nothing executes the agent's side of the
contract.** The gate proves the architecture: imports, write ownership and
`imported_at` exclusion are AST checkers that fail the build. The documents an
agent boots from are checked only for existence: a test confirms that a tool
named in the guide is registered, not that the call in the guide succeeds.
Nothing measures what a seat is sent. Two kinds of defect follow.

The first kind is a document that says one thing while execution does another:

- The guide says to retry an approved write. The retry never applies (MCP-1).
- The pickup loop offers back the work just completed (LOOP-1).
- The seat profile's start text names tools the profile hides (MCP-6).
- The README's next step returns 401 (DOCS-1), the guide warns agents off two
  merged features (LOOP-3) and the boot block names two arguments that do not
  exist (LOOP-4).

The second kind is a surface nobody measured: reads with no size bound
(MCP-2), a printed configuration three times the size a seat needs (MCP-3),
results and refusals with no single shape (MCP-4, MCP-5), closed sets typed as
free strings (MCP-8).

July's review reached the same conclusion about command ownership. It found
"the *doctrine* sound and the *enforcement* human", and the fix was to make
the doctrine mechanical. This specification applies that fix to the agent
contract. Requirements A4 to A9 close the surface gaps directly. A15b and A16
make the documents and the loop executable, so the first kind of defect fails
a test instead of reaching an agent.

Two further groups sit beside the agent contract and are included because an
agent fleet depends on them: the builder's workplace (Track B), where the
contract's first condition of done does not fit the container an agent is
given, and the controls an operator relies on (Track C), where a gate, a
hidden container and an audit event each failed to hold.

## Principles

Each is a property a test can hold.

1. **No dead ends.** An agent that follows the documented loop always has a
   next call that makes progress. A mistaken call is refused or can be undone.
2. **Every response fits a budget.** Reads are bounded by size, not only by
   row count. More is available on request and the response says so.
3. **One result shape and one refusal shape.** A refusal carries a stable
   machine code and, where one exists, the call that resolves it. A write that
   was not applied never looks like success.
4. **The smallest sufficient surface is the default.** What Athena prints is
   what a seat should load.
5. **Documents are executed.** Every request or tool call shown to an agent
   runs in the gate. Counts and inventories are generated.
6. **Done is achievable where agents work.** The contract's definition of done
   completes in a 4-CPU, 15 GB container, and there is a supported inner loop
   below it.
7. **Failure is an event.** When a worker vanishes or loops, something is
   recorded and the accountable account is told within one wait period.
8. **Controls narrow for every account.** A gate, a budget or a hidden
   container holds against an agent whatever role it has been given.
9. **Every capability has a REST route and an MCP tool.** Steering rule 1 in
   `docs/VISION.md` is checked, not remembered. Deciding approvals and
   changing one's own gate or budget are deliberate human-only exceptions
   (C1).

## Scorecard

Baselines are measured on `99a0286`. The target column is the acceptance bar
for the wave named. A row with no number yet says how the number is to be set.

| Measure | Baseline | Target | Wave |
|---|---|---|---|
| Reproduced loop defects with no recovery by the agent | 3 (MCP-1, LOOP-1, LOOP-2) | 0, held by A16 | 0, 0b, 1 |
| Written guarantees false by execution | 4 (SEC-1, SEC-2, ARCH-1, MCP-1) | 0 | 0 |
| Product gate in a 4-CPU, 15 GB container | Workers killed for memory after about 16 minutes | Passes; peak memory printed | 0 |
| Apps left alive after a 20-test run | About one per test, at 8 to 9 MB each | At most one, held by a test | 0 |
| Tool text loaded by the printed MCP config | 114 tools, 102,836 bytes | `seat` (26 tools, 32,793 bytes) for a token without `docs:write`; `seat+knowledge` (37 tools, size to be measured) with it | 1 |
| Largest default read | 161,000 characters (`search_work`, a `seat` tool); 203,000 (`list_pages`, a `seat+knowledge` tool) | 16,000 characters, or a declared and listed exception | 1 |
| Refusals that carry a machine code | Not measured; 401, 403, 404 and most 409 carry none | All 4xx and 5xx JSON bodies | 1 |
| Tools with a declared result shape | 8 of 114 | All in the three seat profiles, then all | 1 |
| Entry-document examples executed in the gate | 0; existence is checked | All | 1 |
| Test evidence available below the full suite | None supported | `--profile changed` | 2 |
| Lease lapse reaches the accountable account | Never, unless it polls or is admin | Within one `wait_for_events` period (25 s at most) | 3 |
| Calls for one reviewed change by a conductor | About 37 (reviewer's count, not re-run) | Re-measure first; then the count less the calls C9 removes | 3 |
| Cockpit-only capabilities | 4 groups (ARCH-4, ARCH-5) | 0, or each on a declared exception list | 4 |
| Commits that name an Athena issue | 13 of 804; none since August | Every agent branch, if D9 is accepted | 4 |

### Wave 0 results

Re-measured on the Wave 0 head in the same 4-CPU, 15 GB container.

| Measure | Baseline | After Wave 0 |
|---|---|---|
| Reproduced loop defects with no recovery by the agent | 3 | 0. MCP-1 and LOOP-2 are fixed in Wave 0 (A1, A3), LOOP-1 in Wave 0b (A2). |
| Written guarantees false by execution | 4 | 0. Each original reproduction was re-run against the fixed code and is refused (C1, C2, C3, A1). |
| Product gate in a 4-CPU, 15 GB container | Workers killed for memory after about 16 minutes | Passes in about 15 minutes: 5,259 tests, peak 1.41 GiB (B1). |
| Apps left alive after a 20-test run | About one per test | One, the module-level app, held by `tests/test_suite_memory.py`. |

Coverage on that head is 93.83% line, 84.98% branch and 91.81% combined,
against floors of 92.60, 82.30 and 90.30.

Three things Wave 0 found and did not fix:

- A bulk close by a gated agent needs one approval per issue and answers 202
  for the whole call. Retries under one key now make progress and write no
  duplicate event, so A1 did not make it worse, but the shape is poor.
- A project create or rename whose name or key collides after the route's
  pre-check returns 500. It writes nothing.
- Trails written before C2 may hold a `deleted_project` event for a project
  that still exists. No repair is proposed.

### Wave 1 results

Measured on the Wave 1 head in the same 4-CPU, 15 GB container. Wave 1 is A4
to A10, A15b, A16 and A17, one commit each, in PR #468.

| Measure | Baseline | After Wave 1 |
|---|---|---|
| Reproduced loop defects with no recovery by the agent | 3 | 0, now held by `tests/test_contract_agent.py` (A16). Switching the A2 rule off makes it fail. |
| Tool text loaded by the printed MCP config | 114 tools, 102,836 bytes | `seat`: 26 tools, 36,618 bytes. `seat+knowledge`: 37 tools, 45,623 bytes (A7). |
| Largest default read | 161,000 characters (`search_work`); 203,000 (`list_pages`) | 16,000 for every tool in the three seat profiles; one declared exception, `get_issue_work_context` at 32,000 (A6). |
| Refusals that carry a machine code | 401, 403, 404 and most 409 carried none | Every 4xx and 5xx JSON body: 43 REST codes, 11 naming a `next` call, plus 5 MCP-side codes. A source check fails on a refusal without one (A5). |
| Tools with a declared result shape | 8 of 114 | All 140: one JSON object per call, an output schema, and read-only, destructive and idempotent hints (A4). |
| Entry-document examples executed in the gate | 0 | All: 33 fenced blocks in five documents; 14 run as requests, 14 checked against a real response or config, 5 excluded with a reason. Of 45 inline tool calls, 11 are run and 34 checked against the tool's schema and profile (A15b). |

The `seat` tool list is larger than the 32,793 bytes measured at the baseline
because each tool now carries hints, an output schema and the report's closed
sets. The full surface on a member token grew for the same reason, from
102,836 to 114,486 bytes; a printed config no longer loads it.

The product gate passes on the head that holds all ten requirements
(`8ab2c59`): 5,449 tests in about 18 minutes, peak memory 1.42 GiB. Coverage
is 94.04% line, 85.36% branch and 92.06% combined, against floors of 92.60,
82.30 and 90.30. Wave 1 added 174 tests.

What executing the contract found, all fixed in this wave:

- Five wrong examples in the entry documents (A15b): the runtime recipe's
  issue named a project that does not exist on a fresh instance, so its next
  step returned 404; `QUICKSTART.md` told a seat to call `watch`, which no
  seat profile carries; two tool calls omitted a required `issue_id`; and the
  guide's 428 example showed a stale `detail`.
- Thirteen seat tools answered above 16,000 characters on a workspace with one
  large issue and one large page, including `get_issue`, `my_desk` and
  `update_issue` (A6).

Found and not fixed:

- JSON refusals raised by the cockpit's own routes under `web/` carry no
  registered code. The source check does not cover them.
- The response budget counts the text as sent, which the MCP library
  pretty-prints; about 30% of each budget is whitespace.
- Six list routes take a limit and no offset. MCP pages them by over-fetching
  up to the route's cap and says `truncated` with no `next` at the cap. A REST
  cursor in D2's vocabulary would remove the cap.
- A comment longer than 12,000 characters cannot be read whole through MCP,
  and no REST route reads one comment.
- An in-process `FastMCP.call_tool` still raises on failure; only the wire
  carries the single failure object. About fifty existing tests rely on it.
- `update_issue` is marked destructive, because an issue's title and body keep
  no history. A client that asks before destructive calls will ask on the most
  common seat write. Keeping a history of issue text would remove the reason.
- Imported comments and pages name the local account the import matched,
  because their rows carry no import marker. Marking them needs a migration.
- `POST /issues/bulk` reports a wrong status per item without
  `valid_statuses`.
- `GET /issues/{ref}`, the list and search reads and the learning reads carry
  no `authored_by`; A10 named five packets and they are outside them.
- The desk advertises a notebook that `seat+knowledge` can edit and cannot
  create, and `delegate_issue` tells a conductor to resolve ids with
  `list_users`, which is admin-only. Both need a profile decision.
- The app has no injectable clock. The contract agent lapses a lease by
  rewriting its stored expiry, as the existing lease tests do.
- `src/athena/field_guide/07_watching.md` and `docs/DESK.md` still name tools
  no seat profile carries; `examples/desk_loop.md` is not executed.

### Wave 2 results

Measured on the Wave 2 head in the same 4-CPU, 15 GB container. Wave 2 is B2
to B5, B6a and B7 to B10, in PR #469. B8 landed as three commits, one per
part. The build order differed from the table's so that `scripts/gate.py` and
`AGENTS.md` each changed once.

| Measure | Baseline | After Wave 2 |
|---|---|---|
| Test evidence available below the full suite | None supported | `scripts/gate.sh --profile changed`. A one-line change to a command module ran 570 of 5,589 tests in 88 seconds *(lead)*; a document, 111 tests in 35 seconds, and `core/activity.py`, 4,692 tests in 711 seconds *(build agent, not re-run)* (B2). |
| Modules the write-ownership check covers | Matched by name; `aegis/api.py`, `mentor/api.py` and `main.py` unchecked | Every module that builds a router or registers routes, handlers or middleware (B5). |
| A committing call inside `db.transaction` | Undetected | A gate stage and CI step fail on it, and `db.transaction` raises at run time. It found one bug, in `dependencies.remove_link` (B5). |
| Places edited to add an MCP tool | Five tables in `server.py`, the tool, its client method, the generated doc | The tool with its declaration, its client method, the generated doc. The tool list an agent sees is byte-identical across 24 surfaces (B8). |
| Hand-kept inventories | The index omitted 5 documents and 2 directories; `OPERATIONS.md` had no row for 21 of 56 settings; two "None known" debtor rows were wrong | The index is asserted, and the settings and debtor tables are generated (B6a). |
| OpenAPI document for agents | One document, 393 operations, browser routes included | `/openapi-agent.json`: 220 operations, with the bearer scheme, `Idempotency-Key` on 99, `If-Match` on 7, the run headers on all 118 writes, and a shared refusal body (B7). |
| Coverage floors | 92.60 line, 82.30 branch, 90.30 combined; unchanged since July and 1.5 to 3.3 points under the measurement | 93.86, 85.31, 91.90; the check fails when a measurement is more than half a point above its floor (B9). |
| `time.sleep` in the tests | 3 | 0, held by a test (B9). |
| Test files that redeclare `_bootstrap` | 99 | 93, held by a ratchet; `tests/support.py` is the shared start (B9). |
| A fresh checkout to a working environment | By hand, from memory | `scripts/bootstrap.sh`: 17 seconds in a fresh shallow clone here, 839 commits fetched, `.venv` verified against the pinned constraints (B3). |

The product gate passes on the head that holds all nine requirements
(`edb55cd`): eleven stages, 5,771 tests in about 23 minutes, peak memory
1.59 GiB. Coverage is 94.11% line, 85.56% branch and 92.15% combined, against
the raised floors. Hosted CI passed on the same head under those floors. Wave 2
added 322 tests. The suite is about five minutes slower than at the end of
Wave 1.

What the new checks found, fixed in this wave:

- `dependencies.remove_link` always committed, so an unlink refused after the
  delete removed the link and wrote no event. A refused unlink now leaves the
  link in place.
- Four writes in `main.py` sat outside any command. Three moved behind
  commands unchanged; the startup migration is allow-listed by name.
- The "None known" debtor rows for sprints and API tokens were wrong: every
  sprint command and both token commands take a bare actor id.

Found and not fixed:

- **The changed profile has a blind spot.** Imports are not followed through
  `athena.main`, because 289 of 360 test files import it. A test that reaches
  changed code only through the app is selected by a 17-entry map that matches
  test files by name, so a differently named test can be missed. A change to
  any of 15 core modules that reach `athena.middleware` runs the full suite.
  Recording per-test coverage contexts in one scheduled CI job would make the
  map exact.
- **42 commands take a bare actor id and 23 writes sit outside a command.**
  They are now listed in `COMMAND_MIGRATION.md`; none is paid down.
- **Writers that take no `commit` parameter still commit unconditionally**
  (`issues.update_status`, `notifications.mark_read` and others). None is
  called inside a transaction, and the run-time guard would catch one that
  was. `start_sprint` and `update_page` open `BEGIN IMMEDIATE` by hand, outside
  both checks.
- **The agent OpenAPI document leaves out what could not be derived:** the
  token scope each operation needs, and per-route 404, 428 and scope-denial
  statuses, which fall under a `default` response. Its first request costs two
  to three seconds per process. `docs/AGENT_API.md` still calls
  `/openapi.json` the full REST reference.
- **Eight setting reads are checked, not routed through the registry**, in
  `mcp/server.py` and the seat doctor, because importing the server's config
  there would make them fail on unrelated settings. `DISPATCH.md` and
  `WORKERS.md` keep hand-written tables of the same variables, and `examples/`
  and the `Dockerfile` are not scanned.
- **The `imported_at_guards.toml` files are not package data**, so the guard
  checker works from a checkout only.
- **The fallback rules in `AGENTS.md` were written without sight of the
  machine-level handbook they stand in for.** They restate this repository's
  rules and the owner's standing instructions; the owner should check them
  against the handbook.
- `AGENTS.md` grew from 10.9 KB to 17.6 KB over the wave, and it is re-read
  every session.
- Dated documents under `docs/` still quote the old coverage floors.

## Priorities

- **P0.** A written rule is false, or a loop defect has no recovery. Fix
  before anything else.
- **P1.** The contract an agent depends on in every session.
- **P2.** Friction and hardening.

Each baseline cell ends with how it was established: *(lead)* re-run by the
lead reviewer, *(verifier)* reproduced by execution by an independent
verifier, *(reviewer)* executed by one reviewer and not re-run, *(read)*
confirmed by reading the code.

## Track A: the seat

| ID | Priority | Baseline behavior at `99a0286` | Required behavior |
| --- | --- | --- | --- |
| A1 | P0 | A gated `PATCH /issues/{id}` returns 202 `approval_required`. The idempotency middleware stores it as a 24-hour receipt (`middleware.py:1021` stores every 2xx). After the operator approves, the same key replays the 202 with `Idempotent-Replay: true`; only a new key applies the write. `AGENT_GUIDE.md:225` says to retry the same write. *(lead)* | A response that reports the write was not applied is never a receipt. The key is released, as it already is for a 4xx. After approval, the same request under the same key applies once, and that result is the receipt. |
| A2 | P0 | With two ready issues, `claim_next_ready` returns the first; the agent completes it with a report; the next `claim_next_ready` returns the same issue ahead of the untouched one. The same follows a `needs_input` yield. *(lead)* | `claim_next_ready` does not offer an issue to the account whose own lease on it most recently ended in a completion or a `needs_input` yield, until a comment, status change, assignment or claim by a different account that is not automation is newer than that settlement. |
| A3 | P0 | `POST /desk/cursor {"after_id": 999999}` returns 200. A lower value then returns 409, and `events_since_cursor` reads 0 after new events. *(lead)* | `after_id` above the newest activity id is refused with code `cursor_ahead`; the body carries the newest id visible to the caller. Existing cursors above the newest id are repaired once. |
| A4 | P1 | 0 of 114 tools carry annotations; 8 declare an output schema; none returns structured content. A list arrives as one unlabelled block per row. An empty list arrives as zero blocks with `isError` false. *(verifier)* | Every tool returns exactly one JSON object. A list is `{items, count, truncated, next}` and an empty list is that object with no items. The read-only hint is generated from `TOOL_SCOPES` plus the admin-read list; destructive and idempotent hints are declared per tool, and registration fails closed on a tool without them. |
| A5 | P1 | At least three refusal bodies coexist. 401, 403, 404 and most 409 refusals have no code, including "issue is claimed by …". A 202 `approval_required` reaches the model with `isError` false and no seat write tool mentions it. *(verifier)* | Every 4xx and 5xx JSON body from a REST route carries a `code` from one registry; existing fields are unchanged. MCP returns the same object with `isError` true and, where the registry defines one, `next`: the call that resolves it. A 202 `approval_required` is returned with `isError` false, `applied: false` and `next`. |
| A6 | P1 | List and search tools return full bodies. Measured: 161,000 characters from `search_work` with one large issue; 203,000 from `list_pages` over 12 pages. *(verifier)* | List and search return summary rows. Bodies come from a get-one call or an explicit `include_body` with `max_chars`. Every tool in `seat`, `seat+knowledge` and `conductor` has a default response budget of 16,000 characters unless it declares a larger one and appears on a reviewed exception list. A test enforces it against a seeded large-content database. |
| A7 | P1 | `claude_mcp_config` sets no `ATHENA_MCP_PROFILE`, so its three callers (`athena-demo`, `POST /users/onboard_agent` and the cockpit's onboarding page) all hand out the full surface. The command is a bare `athena-mcp` that is not on PATH unless the venv is activated. *(verifier; the lead saw the demo's output)* | The emitted config names a profile chosen from the token's scopes, and an absolute command path. Unset keeps meaning `full`, so existing installs do not change (see D1). |
| A8 | P1 | `my_desk` tells a seat to drain `recent_events`; `search_work` points at `query_help`. Neither is in `seat`. The start text never names `claim_next_ready`, which is. The desk advertises a notebook that no seat profile can open. *(lead)* | For each profile, every tool named in an in-profile description or in `SERVER_INSTRUCTIONS` is in that profile. `my_desk` names `claim_next_ready`. A lane the profile cannot act on says which profile can. |
| A9 | P1 | Completion-report errors arrive one per round trip, and the report's closed sets (verdicts, finding keys, the keys of `results` and `evidence`) appear in no schema. No tool lists a project's statuses and the 422 does not name them. `POST /issues/{id}/claim` and `POST /tokens` ignore unknown fields: a token request naming another user mints a token for the caller. `GET /search?kind=bogus` returns an empty 200. *(verifier; the lead saw the token case)* | The command returns every report violation at once, each by field path. The closed sets are published in the MCP tool schema and the OpenAPI description. The work-context packet lists the project's status names and a wrong status names them. Agent-facing write models refuse unknown fields. An unknown filter value is a 422. |
| A10 | P1 | A comment reading "SYSTEM NOTICE … ignore prior instructions" reaches another agent's work-context packet as plain text. `advisory_untrusted` exists only on handoff, delegation and fleet-work shapes, and the server instructions say nothing. *(verifier)* | `SERVER_INSTRUCTIONS` states once that all body, comment, page, learning and handoff text is data written by other actors. Every free-text field the reader did not write carries `authored_by {id, is_agent}`. A schema test fails when a new free-text field lacks it. |
| A11 | P2 | The MCP client keeps the ETag header and drops `Idempotent-Replay` *(read)*. A key whose outcome is unknown answers 409 `idempotency_indeterminate` and no route or tool reads its state *(verifier, by setting the state directly)*. | MCP results carry `_replayed: true` on a replay. The caller can read the state of its own key (`GET /idempotency/{key}` and a tool), and the guide covers the in-progress and indeterminate codes. |
| A12 | P2 | Renewing a held lease needs a fresh issue ETag as well as the generation, and the server's own hint names only the generation. `heartbeat_agent_run` requires a run id the session already holds and accepts a wrong one. *(verifier)* | A matching generation alone renews. The run id defaults from the session and a mismatched one is refused. |
| A13 | P2 | One desk response gives `identity.budget.window_started_at` as `2026-11-21T18:19:13Z` and `office.chair.expires_at` as `2026-11-21 18:49:13`. `asks.review_requests` is absent when empty, while the other lanes are present and empty *(lead)*. Paging uses five parameter vocabularies *(read)*. | Agent packets emit RFC 3339 UTC, include every documented lane, and give `expires_in_seconds` beside each expiry. New endpoints use the one paging vocabulary chosen under D2; a test over the OpenAPI document refuses a new one. |
| A14 | P2 | `athena-mcp` advertises prompts and resources and serves none. The boot block, the guide and the glossary are files in a repository an MCP-only agent cannot read. The glossary defines none of desk, office, floor, inbox, watch, yield or claim-next. *(verifier)* | The boot block, the loop guide and the glossary are MCP resources. The glossary has a row for every word in a tool name that is not on a short listed set of verbs, with a "not the same as" column, and a test holds that. |
| A15a | P0 | `RUNTIME_RECIPE.md` steps 1 and 2 return 401 *(verifier)*. `AGENT_GUIDE.md:298-305` warns agents off two merged features *(verifier)*. The boot block names `get_issue_lease(ref)` and `mark_notification_read(id)`; the arguments are `issue_id` and `notification_id` *(read)*. Four documents give three sizes for the conductor profile *(verifier)*. | The four are corrected by hand. |
| A15b | P1 | The doc tests check that routes and tools exist, not that the example succeeds. *(lead, by reading the tests)* | Every fenced request in the entry documents runs against a seeded app and returns its documented status. Every tool call shown is validated against the tool's input schema. Profile sizes and similar counts are generated or asserted. |
| A16 | P1 | No test plays the agent. Each defect in this track passed the gate. | One scripted seat runs in the gate, on the `seat` profile, through the documented loop and each failure branch, asserting progress, call counts and response sizes. |
| A17 | P2 | With a rejected token, `athena-mcp` prints a warning to stderr and starts with all 140 tools, each of which then fails with 401. *(verifier)* | A 401 or 403 from the startup `whoami` exits with a message. The server keeps failing open only when Athena cannot be reached. |

### A1: approval and idempotency

The approval request is already its own durable, idempotent record
(`approvals.open_request` returns the existing ask). The receipt adds nothing
and causes the loop. Let the route mark the response non-final and have the
middleware release the key instead of completing it. Do not change the
claim-next receipts: replaying `claimed: false` and `claimed: true` is an
accepted decision in `docs/ATOMIC_PICKUP_SPEC.md`.

Tests: gate, request, approve, same key returns 200 and the issue is closed;
a second replay returns the identical 200 with the replay header; reject, same
key returns the rejection with the operator's note; two concurrent same-key
retries after approval apply once. Update `AGENT_GUIDE.md` and `APPROVALS.md`
in the same change, and add the outcome to the description of each seat write
tool that can be gated.

### A2: work the caller just settled is not offered back

Add one clause to `ready_queue.ready_clause`, as stated in the table. It is
scoped to the caller: another eligible account can still be offered the
issue, and direct `claim_issue` is unchanged, so an explicit takeover or a
resume by another account still works. The desk already has a
`work.waiting_on_operator` lane; the excluded issue belongs there with the
reason it is waiting.

The rule covers a completion with or without a report, because every report
field is optional. It ignores automation and the caller's own later events,
so an automatic comment does not hand the issue back.

D3 asks whether to go further and treat such an issue as not ready for every
account. Either way this amends the ready rule accepted on 2026-11-17 with
atomic pickup, and needs a decision record.

Tests: the two-issue sequence above returns the second issue; an operator
comment makes the first ready again for the same account; an automation
comment does not; a `needs_input` yield is not re-offered until answered; the
ready count on the desk matches what claim-next can serve.

### A3: a cursor cannot pass the present

Validate in `desk_commands.advance_cursor` inside the existing immediate
transaction, against `MAX(activity.id)`. The refusal body carries the newest
id the caller can see, not the global one.

Migration 0073 makes cursors advance-only at the database. Repairing a cursor
that is already past the end is therefore a one-time rewind: a forward-only
migration drops the rewind trigger, clamps every row whose position exceeds
`MAX(activity.id)`, and recreates the trigger unchanged. D14 asks the owner to
confirm that rewind.

Tests: an id above the newest is refused with `cursor_ahead`; the newest id is
accepted; a repaired row can advance; an ordinary rewind is still refused.

### A4 to A6: one shape, one budget

Introduce the result envelope behind the MCP layer first, where no external
client depends on the current shape. On REST, add `code` to every refusal
without changing `detail`. Whether to normalise REST bodies further, and which
paging vocabulary new endpoints use, is D2.

The registry of codes is the single owner. Command modules already raise
errors with a `kind`; map each kind to a code once, and give every
`HTTPException` raised in a transport a code from the registry. The check is
static, because refusals are raised at run time and a route table does not
list them: an AST pass over `raise HTTPException` sites and the kind-to-status
tables fails on one without a code. The same registry generates the reference
table in `docs/AGENT_API.md`.

Response budgets are declared beside each tool's scope, in the same place as
A7's profile membership (see B8). The enforcing test seeds one large page, one
large issue and sixty comments, calls every tool in the three seat profiles
with default arguments, and compares the serialized size with the declared
budget. Today that test would fail on `search_work` and `list_pages`.

### A7 and A8: the default surface is the seat

`claude_mcp_config` gains a `profile` argument. Its three callers pass
`seat+knowledge` when the token carries `docs:write` and `seat` otherwise. The
demo's token carries `docs:write`, so the demo prints `seat+knowledge`. The
command is the absolute path of the `athena-mcp` script beside the running
interpreter. `whoami` and `my_desk` name the other profiles so a seat can ask
to be widened. A7 does not depend on D1.

The closure test extends `tests/test_mcp_docstring_references.py`, which
already checks tool names in descriptions against the full registry. Run it
once per profile, match any registered tool name followed by `(` whether or
not it is backticked, and fail on one that is outside the profile. Today it
would fail on `recent_events(` in `my_desk` and `query_help()` in
`search_work`.

### A9: report grammar stays in the command

`CompleteClaimIn` types its report fields as `object | None` on purpose: the
shared command owns the grammar, and FastAPI's own validation errors would
echo rejected input, including a pasted credential. That is the accepted
decision in `docs/decisions/2026-11-04-report-credential-refusal.md` and this
requirement keeps it. The change is inside the command: collect every
violation and return them together by field path, never the value. Publish
the closed sets as documentation in the MCP tool schema and the OpenAPI
description, without moving validation to FastAPI.

### A10 and A14: definitions the tests need

For A10, a free-text field is a string field in an agent packet with no enum,
pattern or format. The schema test walks the response models of the work
context, desk, comment, page and notification reads.

For A14, the glossary test splits every tool name on underscores, drops the
words on a listed set of verbs and connectives (`get`, `list`, `my`, `for`,
and so on), and requires a glossary row for each word left.

### A15b and A16: executed documents and the contract agent

A15b is a harness, not a rewrite. It extracts fenced `http`, `bash` curl and
tool-call blocks from `AGENT_GUIDE.md`, `AGENT_BOOT.md`, `RUNTIME_RECIPE.md`,
`QUICKSTART.md` and `CLOSEOUT_RECIPE.md`, substitutes tokens and ids from a
seeded demo database, runs each through `TestClient` or the tool registry, and
asserts the status the document shows. On `99a0286` it would fail on the four
drifts A15a corrects.

A16 is the lasting fix. `tests/test_contract_agent.py` runs in-process, the
way `tests/test_mcp_surface_slim.py` already drives tools: a
`TestClient`-backed `AthenaClient` through `build_server` with the `seat`
profile, and an injected clock so a lease can lapse without waiting. It plays
a seat:

1. `my_desk`, then `claim_next_ready`, work context, a comment, a completion
   report, and `claim_next_ready` again. Assert a different issue.
2. A gated close: request, operator approves, same-key retry. Assert applied.
3. A yield with a question, an answer, a resume by a second account.
4. A lapsed lease and a deliberate re-claim.
5. Every refusal met on the way has a code and, where the registry defines
   one, a `next` call that succeeds.
6. The cursor advances to the last handled event and cannot pass it.

It records the number of calls and the bytes received for the whole run in a
tracked file. The test fails when either rises, and the file may be raised
only with a stated reason in the same PR. On `99a0286` at least scenarios 1,
2, 5 and 6 fail.

A separate smoke test asserts that the configuration `claude_mcp_config`
emits launches: start `athena-mcp` as a subprocess against an ephemeral
server and list its tools. That needs a listening server and stays out of the
in-process suite.

## Track B: the builder

| ID | Priority | Baseline behavior at `99a0286` | Required behavior |
| --- | --- | --- | --- |
| B1 | P0 | `scripts/gate.sh --profile product` in a 4-CPU, 15 GB container: static stages pass; three test workers die and the kernel log shows memory kills at 4.2 and 5.2 GB; the gate reports `3 failed, 5213 passed`, 79.31% coverage and `GATE FAILED: coverage`. Each app-building test retains 8 to 9 MB through three `lru_cache` wrappers in `fastapi.dependencies.models`. Clearing them after each test leaves 0.1 to 0.5 MB. *(lead)* | The suite releases each test's app. The product gate completes in that container; one full run confirms it and records peak memory. The gate prints peak memory, and when a worker dies it says so instead of naming whichever tests were running. |
| B2 | P1 | The quick profile runs lint, format and five contract scripts: no tests, no type check. 339 test files sit in one directory; 83 of the 232 modules that are not `__init__.py` have a same-named test. *(read)* | `scripts/gate.sh --profile changed` runs the quick stages, the type check, and the tests mapped to the diff, and prints a receipt marked PARTIAL that lists what it selected. |
| B3 | P1 | The cloud checkout this review started from was shallow and had no venv. There is no `.claude/` directory. `AGENTS.md:7-11` defers its permission rules to `~/.claude/CLAUDE.md`, which a cloud container does not have. *(lead)* | A committed bootstrap builds the venv exactly as `CONTRIBUTING.md` does and fetches full history. `AGENTS.md` states the rules that apply when no machine-level handbook is present (see D8). |
| B4 | P1 | The `AGENTS.md` module table omits `src/athena/mcp/` (8 files, 140 registered tools) and `field_guide/`; its orientation tree also omits `workflows/`. No document says which files a new tool, route, migration or setting touches. *(verifier)* | `AGENTS.md` lists them and carries a change-recipe table: files to edit, generator to run, tests to run. |
| B5 | P1 | `_is_transport` in `check_write_ownership.py` matches `web`, `mcp` and `*_api`, so `aegis/api.py`, `mentor/api.py` and `main.py` are unchecked: a raw `DELETE FROM activity` added to `aegis/api.py` passes. `activity.record` defaults to `commit=True` and nothing detects a committing call inside `db.transaction`; a reviewer's scan found three, one of which is ARCH-1. *(verifier)* | The checker covers every module that defines a router. A new gate stage fails on a call inside a `db.transaction` block to a function that commits. `commit` is a required keyword on `activity.record` and the raw writers. |
| B6a | P2 | Hand-kept inventories have drifted: `INDEX.md` omits five documents and `docs/decisions/` *(verifier)*; `OPERATIONS.md` omits 13 of 48 `ATHENA_*` variables *(verifier)*; `COMMAND_MIGRATION.md` says "None known" for projects and sprints, whose writers still take a bare actor id *(read)*. The one generated inventory, `AGENT_API.md`, has not drifted. | Each is generated or asserted the way `AGENT_API.md` is: an index-completeness and link test, a settings table generated from one registry in `config.py`, and a debtor table generated from the command modules. |
| B6b | P2 | The prune ledger's counts are from August: 362 routes and 124 tools against 398 and 140 today, with all 20 verdicts unset. *(verifier)* | Route, tool and table counts live in a tracked file the gate compares. A PR that raises one updates the file and says what the addition earns. |
| B7 | P2 | `/openapi.json` lists 393 operations, browser form routes included, and mentions `If-Match` three times. *(verifier)* | An agent-facing OpenAPI document excludes browser routes and declares the bearer scheme, `Idempotency-Key`, `If-Match`, `X-Athena-Run` and the refusal `code`. |
| B8 | P2 | Adding one MCP tool edits `TOOL_SCOPES` and a profile set in `server.py`, a method in `client.py`, a `tools_*.py` body and the generated doc. In a reviewer's sample of 45 merged PRs, `AGENT_API.md` was touched by 12 and `server.py` by 10, and 16 commits in the window merge main into a branch before landing. *(verifier)* | A tool declares its scope, profiles, hints and response budget where it is defined. The registration check stays fail-closed. The `imported_at` guard registry is split per module. Changelog entries are per-PR fragments. |
| B9 | P2 | `_bootstrap` is redeclared in 95 test files. `pyproject.toml:49` says the suite has no `time.sleep`; `tests/test_event_wait.py` has three. Coverage floors last moved on 2026-07-26; measured coverage is 1.2 to 2.6 points above them. *(verifier; coverage measured by the lead)* | A shared test-support module is the starting point for new tests. The three sleeps wait on the condition. `check_coverage.py` fails with "raise the floor to X" when a measured value exceeds its floor by more than half a point. |
| B10 | P2 | The PR template asks for `ruff check`, `pytest -q` and a smoke script. `AGENTS.md` defines done as the gate. *(read)* | The template asks for the gate receipt. `gate.py` can write it as JSON. The template joins the "change together" list in `AGENTS.md`. |
| B11 | P2 | Nothing shows what is in flight. PR #438 (1,528 added lines) never merged; its content landed nine days later through the other lane's reconcile PR, and #438 is still open and conflicted. 238 of 320 remote branches are not ancestors of main. 13 of 804 commits name an issue. *(verifier; PR state checked by the lead)* | One board. See D9: either Athena tracks its own development, or the contract states another convention and a CI advisory checks it. |

### B1: the gate fits the worker

The smallest correct fix is an autouse fixture in `tests/conftest.py`:

```python
import functools

import fastapi.dependencies.models as fastapi_models


@pytest.fixture(autouse=True)
def _release_apps():
    yield
    for name in dir(fastapi_models):
        obj = getattr(fastapi_models, name)
        if isinstance(obj, functools._lru_cache_wrapper):
            obj.cache_clear()
```

It discovers the caches instead of naming them, so a FastAPI upgrade that adds
or renames one does not silently restore the leak. Measured on 30 app-building
tests: 9.2 MB per test before, 0.1 MB after. That measurement is a sample. The
full gate has not been run with the fixture, so the PR that adds it must run
the product gate once in a 15 GB container and record the peak.

The fixture clears the caches after the test body, so a test cannot observe
its own app being released. Hold it from outside: a test that uses pytest's
`pytester` to run a generated file of twenty app-building tests in a
subprocess, whose last test collects garbage and asserts that at most one
`FastAPI` instance is alive. Without the fixture that count is about twenty.

In the same PR, because `AGENTS.md` requires the gate script and its two
documents to change together:

- `coverage.sh` takes its worker count from the environment, default 4.
- `gate.py` reports peak resident memory for the coverage stage and, when a
  worker exits on a signal, prints that fact and the likely cause.
- `AGENTS.md` and `CONTRIBUTING.md` state the memory the product gate needs,
  measured after the fix.

Then re-measure one CI shard. Whether four runners are still needed is D15.
Do not lower a coverage floor and do not drop tests to make this pass.

### B2: an inner loop with evidence

`--profile changed [--base origin/main]` selects tests by import graph from the
changed paths, plus a small checked-in map for modules reached only over HTTP
(`mcp/tools_*.py`, `*_api.py`). Recording per-test coverage contexts in one
scheduled CI job would make the map exact; start with the import graph. The
receipt uses the product format and the word PARTIAL. `AGENTS.md` names it as
the inner loop and keeps the product profile as the condition of done.

### B5: checkers that reach what they claim

Add `aegis/api.py` and `mentor/api.py` to the transport predicate. The review
showed the tree is clean with those two counted, so that step adds no debt.
Then replace the name test with a structural one, a module is a transport if
it defines an `APIRouter` or registers routes on the app, which also brings in
`main.py`. That has not been tested. It will flag at least the
approval-request exception handler (`main.py:517`, ARCH-8), which writes
through `approvals.open_request`. Move that write behind a command or
allow-list it by name in the same PR.

The transaction check resolves callees by module. It fails on a call, inside a
`with db.transaction` block, to a function whose `commit` default is true and
is not overridden, or to a function that reaches `conn.commit()`. Pair it with
a runtime assertion in `db.transaction`: on a non-nested exit, raise if the
connection is no longer in a transaction.

## Track C: the fleet and its operator

| ID | Priority | Baseline behavior at `99a0286` | Required behavior |
| --- | --- | --- | --- |
| C1 | P0 | An agent account with the admin role and an admin-scope token is gated on `issue.close` and budgeted. Its close returns 202; its own `POST /approvals/{id}/decision` returns 200 with `requested_by` and `decided_by` equal; the close then applies; it deletes its own policy and its own budget. `APPROVALS.md:95-97` says a gated agent cannot approve itself or ungate itself. *(lead)* | The approval command refuses a decider who is the requester, and refuses the requester's attempt to clear its own policy. The rule lives in the command, not the route. Whether any agent account may decide, and whether an account may change its own budget, is D13. |
| C2 | P0 | `delete_project` records `deleted_project` through a helper that commits, then deletes. When the delete fails, the event stays: the trail says a live project was deleted and the chain verifies *(lead)*. Racing the route against issue creation reached this in 5 of 24 attempts *(reviewer)*. | The event and the delete commit together or not at all. The emptiness checks run inside the command's transaction and a conflict is a typed 409, not a 500. |
| C3 | P0 | For a member outside a private space and project, `GET` of a hidden page or issue returns 404. `POST /pages/{id}/link-mention` naming the hidden target returns 409 where a missing target returns 404, and with the title or key guessed in the body returns 200 and writes the link. *(lead)* | Both link-mention routes resolve the target through the visible-read helpers first. Hidden and missing targets return the same response. |
| C4 | P1 | A 60-second lease lapses. No event is recorded and nobody is notified. The `lease_expired` attention signal reaches the holder and admins only. *(verifier)* | An expired, unsettled lease produces one native `lease_lapsed` event for that generation and a notification to the assignee and to whoever delegated the work. They see the signal in their attention list. A caller in a `wait_for_events` loop learns within one wait period. Needs D11. |
| C5 | P1 | The named reviewer reports HOLD. An account that was never asked completes an unrelated issue with `review_candidate_id` and PASS. The queue's `review_state` becomes `reported_pass`. *(verifier)* | The queue row also says what the requested reviewer reported, in a field an unrequested review cannot move, and the conductor recipe points at it. Needs D4, which supersedes part of an accepted specification. |
| C6 | P2 | With no root catalog, a second lease declaring any path conflicts with the first, and the 409 gives no reason. Declaring no paths succeeds. `OFFICE.md` does not mention the catalog. *(verifier)* | The 409 carries `fence_unresolved` or `fence_overlap`. `OFFICE.md` and the office protocol text state the catalog requirement. Whether an instance without a catalog gets an implicit single root, and whether a project may require paths, is D5. |
| C7 | P2 | An agent that renews and heartbeats without progress is derived as `pinging` and never raised *(read)*. A budget of 3 per hour does not stop 5 comments *(verifier)*. Both are documented limits. | A claim renewed N times with no substantive event raises `no_progress` to the assignee and admins, N configurable. The renew response reports the count. Whether coordination writes are metered is D6. |
| C8 | P2 | Pausing an agent leaves its lease and fence active until expiry. The paused holder cannot release them. An admin must read the generation and complete the claim by hand. *(reviewer; the pause path was read by a verifier)* | Pause, offboard and token revocation return the account's active leases. Whether they may also release them is D12. |
| C9 | P2 | Setting up one outcome with three slices took 21 conductor requests; the reviewed happy path took about 37 calls and 12 threaded identifiers. `create_issue` takes no parent, assignee or delegate. *(reviewer; the missing arguments were read by a verifier)* | `create_issue` accepts `parent_id`, `assignee_id`, `delegate_ids` and `blocked_by`, or one plan command creates the slices atomically and returns ids and ETags. |
| C10 | P2 | Assign-to-seat with radio, the fleet roster and reported-spend rollups answer 401 or 404 to an admin-scope bearer token. Rooms have no API twin. *(verifier)* | Each gains a REST route and a tool, or appears on a declared exception list with a reason. A gate check fails when the web layer calls a command or read model that no REST module reaches and that is not on the list. |
| C11 | P2 | `ATHENA_ANONYMOUS_READS` defaults on. Tailnet mode validates rate limits and nothing about anonymous reads. On a default instance, work context, comments and lease generations answer without a credential. *(verifier)* | Tailnet mode requires an explicit value for `ATHENA_ANONYMOUS_READS`. `athena-doctor` prints the anonymous-read state and the public containers (see D7). |

### C1 to C3: make the written rules true

Each is one small PR with the regression test that would have caught it.

C1 restores what `APPROVALS.md` already says: in `approval_commands.decide`,
refuse when the decider is the requester; in `clear_policy`, refuse when the
target is the actor. Tests: an admin agent deciding its own request; an admin
agent clearing its own policy; the human operator still succeeds. Correct the
docstring that assigns "human-only" to the caller.

Two stronger rules are new, not restorations, and wait for D13: refusing every
agent account as a decider, which is what the command's "human-only" comment
implies, and refusing an account's change to its own budget.

C2: give the three `project_activity` wrappers a `commit` parameter and pass
`commit=False`, as `record_project_visibility_changed` already does. Move the
issue and sprint emptiness checks inside the immediate transaction. Test:
delete a project that owns an issue through the command; assert the refusal,
zero new activity rows and an unchanged chain head. Correct the "None known"
entry in `COMMAND_MIGRATION.md`. B5's new gate stage then keeps the class
closed.

C3: give `mentions.mention_text` and `link_token` an actor, or resolve the
target with `issue_for_read` and `_page_for_read` before reading its text.
Test: hidden and missing targets return byte-identical responses on both
routes. Visibility semantics do not change; this restores them.

### C4: failure is an event

The read paths already derive `lease_expired`. The requirement is that
something writes it down once and that a waiting caller finds out.

The proposed mechanism adds no background sweeper. The first request that
observes an expired, unsettled lease writes the event, keyed by lease
generation so a second observer writes nothing. `wait_for_events` is one of
the observers: on entry it checks the leases on issues the caller is assigned
or has delegated. A conductor in a wait loop re-enters at least every 25
seconds, the long-poll's maximum, so it learns within one period.

Three things need settling before this is built, and they are D11:

- It makes a read path write. `docs/COORDINATION_RELIABILITY_SPEC.md`,
  accepted on 2026-11-17, says readers take a snapshot and no write lock. The
  lapse write must run after the response's read snapshot, in its own
  immediate transaction, and D11 asks whether that is acceptable or whether
  an existing background loop should own it instead.
- The event's actor. The holder did nothing; the proposal is to record the
  observing request's actor with the holder as the subject.
- Automation must not fire on the verb. Exclude `lease_lapsed` from the
  trigger vocabulary, the way imported history is excluded.

Extend `fleet_attention` so the assignee and the delegating account see claims
they are accountable for. No lease is released or swept: decision K1 in
`docs/decisions/2026-10-20-coordination-history.md` stands.

### C5: what the requested reviewer said

`docs/REVIEW_REQUEST_SPEC.md`, accepted on 2026-11-17, says request status is
advisory, that the queue keeps verdict and request-status fields separate,
and that unrequested reviews keep their behaviour. This requirement leaves
`review_state` and unrequested reviews alone. It adds one field that joins a
request to the verdict that fulfilled it, which the accepted specification
deliberately did not do. That is a supersession of those two sentences and
needs a successor decision record (D4).

### C8: pause and the leases it strands

Returning the paused account's active leases is uncontroversial and makes the
manual path one step shorter. Releasing them is a policy change.
`AGENT_GUIDE.md` tells agents "Never sweep leases" and decision K1 rules out
an automatic sweep. An operator releasing the active leases of an account the
operator has just paused is neither automatic nor a sweep of expired rows,
but it is close enough that it needs its own decision (D12).

## Waves

One requirement ID is one change with its own commit, in order within a wave.
Every fix lands with its regression test. Wave 0 lands as one commit per
requirement on the branch that carries this document.

| Wave | Content | Needs a decision first |
|---|---|---|
| 0. Make the written rules true | B1, then A1, A3, A15a, C1, C2, C3 | D14 for A3's repair |
| 0b. The pickup loop | A2 | D3 |
| 1. The agent contract, executed | A4 to A10, A15b, A16, A17 | D2 |
| 2. The builder's workplace | B2 to B5, B6a, B7 to B10 | D8 |
| 3. The fleet | C4 to C9, A11 to A14 | D4, D5, D6, D11, D12 |
| 4. Parity, pruning, one board | C10, C11, B6b, B11 | D7, D9, D10 |

Wave 0 is small and should not wait for the rest to be accepted. It needs no
product decision except A3's one-time cursor repair. C1 as scoped restores a
documented rule; its stronger form waits for D13. B1 goes first because it
makes the gate usable for everything after it. Wave 1 is where the scorecard
moves most.

## Compatibility and deliberate exclusions

- No new queue, scheduler, state store or orchestrator.
- No change to visibility semantics. Hidden reads as missing; C3 repairs a
  bypass.
- No lease is swept or released automatically. K1 stands.
- No change to the claim-next receipts, to `review_state` or to how
  unrequested reviews behave.
- Report validation stays in the shared command (A9).
- REST refusals gain `code` additively. No existing field is removed without
  D2.
- An unset `ATHENA_MCP_PROFILE` keeps meaning `full` unless D1 says otherwise.
- A remote MCP transport is out of scope. `athena-mcp` over stdio against a
  remote base URL already serves a cloud agent.
- Migrations stay forward-only. Coverage floors do not go down.
- This specification does not set a prune verdict, cut a tag or answer an item
  in `docs/DECISIONS_PENDING.md`.
- Two findings in the report have no requirement here: COORD-7, a documented
  limit, and TEST-4, a release-workflow correction outside agentic use.

## Decisions for the owner

All fifteen were accepted as recommended on 2026-11-22.

| ID | Decision | Recommendation (accepted) |
|---|---|---|
| D1 | Should an unset `ATHENA_MCP_PROFILE` mean `seat` instead of `full`? | Not yet. Emit the profile explicitly (A7) and revisit once every printed config carries one. |
| D2 | REST conventions: normalise refusal bodies into one envelope or only add `code`; and which paging vocabulary new endpoints use. | Add `code` everywhere now and normalise later, if at all. For paging, an opaque `cursor` in, `next_cursor` and `has_more` out. |
| D3 | Should work that awaits a decision be "not ready" for every account, not only the one that settled it? This amends the ready rule accepted with atomic pickup. | Yes for a `needs_input` yield. For a completion, keep A2's per-account rule until the fleet has used it. |
| D4 | May the review queue carry a field that joins a request to the verdict that fulfilled it? This supersedes two sentences of `REVIEW_REQUEST_SPEC.md`. | Yes, as an added field. Leave `review_state` as it is. |
| D5 | Should an instance with no root catalog compare paths lexically under one implicit root? May a project require declared paths? | Yes to both, with the basis reported as "lexical, root unknown". |
| D6 | Should comments, claims and delegation count against a budget? `AGENT_BUDGETS.md` says they are deliberately not metered. | Add a second, coordination counter. Leave lease renewals exempt. |
| D7 | Should tailnet mode refuse to start without an explicit anonymous-read setting? | Yes. |
| D8 | Commit a `.claude/` directory with a session bootstrap and an allow-list for the gate? | Yes, with the same bootstrap as a plain script for other lanes. |
| D9 | Should Athena's own development be tracked in Athena? | Yes, if someone will run the instance. It creates a live service: decide who operates it and how cloud lanes reach a product built for one machine or a tailnet. The self-merge carve-out in `AGENTS.md` would still cover the code and would not cover that instance. |
| D10 | Tag a release, set the 2026-Q4 prune verdicts, close PR #438, delete merged branches. | The owner's acts. The evidence for each is in the report under "Process and hygiene". |
| D11 | May a request that observes an expired lease record a `lease_lapsed` event, and who is its actor? This touches the reader-snapshot rule in `COORDINATION_RELIABILITY_SPEC.md`. | Yes, in a separate transaction after the read. If not, give the write to an existing background loop. |
| D12 | May pause, offboard or token revocation release the account's active leases? See K1 and the guide's "Never sweep leases". | Yes, as an explicit operator option, recorded per lease. |
| D13 | Should the approval command refuse every agent account as decider, as its "human-only" comment implies? Should an account be refused changes to its own budget? | Yes to both. An agent that can approve for another agent is the same hole one step removed. |
| D14 | A3's one-time repair rewinds cursors that are past the newest event, by dropping and recreating the advance-only trigger inside a migration. | Accept. The alternative leaves those seats blind. |
| D15 | After B1, are four CI coverage runners still needed? | Decide from the re-measured shard. |

## Verification and delivery

Standing constraints, unchanged from the earlier guides:

- Never push `main`. Branch, PR, gate, merge.
- Every fix lands with the regression test that would have caught it.
- Never lower a coverage floor and never widen `ALLOWED_PRAGMAS`.
- Do not create or push a tag.
- MCP and REST reach the same command behaviour. A fix applies to both.

For each requirement: write the failing test from the baseline cell first and
confirm it fails on `99a0286`; make the change; run the product gate and
record its receipt in the PR. Until B1 lands, the product gate does not fit a
15 GB container; run the four coverage shards one after another and then the
fan-in, as hosted CI does. For Track A, add or extend a scenario in the
contract agent once A16 exists.

A requirement is done when the Definition of Done in `AGENTS.md` holds and, in
addition, its test fails without the change and passes with it, its scorecard
row, if it has one, was re-measured, and the documents that describe the
behaviour were updated in the same PR.
