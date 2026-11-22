# Athena in use: review round 2

Date: 2026-11-22. Reviewed commit: `0b42d40` (`main` after Waves 0, 0b, 1 and 2
of [the agentic-use spec](../AGENTIC_USE_SPEC_ATHENA.md)). The first round is
[repo-review-2026-11-21.md](repo-review-2026-11-21.md), at `99a0286`.

This round looks where the first did not: several agents working at once,
operations, performance, and the operator's cockpit. It also re-measures the
fleet requirements the first spec has not built. The specification that follows
from it is [ATHENA_IN_USE_SPEC.md](../ATHENA_IN_USE_SPEC.md).

## Grades

| | Round 1 (`99a0286`) | Round 2 (`0b42d40`) |
|---|---|---|
| As a repository | 7 | **7.5** |
| For agentic use | 6.5 | **7.5**: 8 for one seat, 6 for a fleet |

### As a repository

| Dimension | Round 1 | Round 2 | Why it moved |
|---|---|---|---|
| Tests | 8 | 8.5 | 5,771 tests; a scripted seat plays the documented loop; entry-document examples are executed; no sleeps; the coverage floors ratchet. |
| CI and gate | 7.5 | 8 | A stage for commits inside a transaction, a JSON receipt, an inner loop that runs the tests a diff reaches. |
| Architecture | 7.5 | 8 | The ownership check reaches every router and one registry owns refusals. 42 commands still take a bare actor id and 23 writes sit outside a command, now listed. |
| Data | 8 | 7.5 | Crash safety held under two hard kills. But issue text is unrecoverable once edited, one assignment voids every stored idempotency receipt, and nothing is ever pruned. Round 1 did not look here. |
| Web | 7 | 6 | Fast, and approvals are good. The work views key on the assignee while agents work by lease, four operator actions are missing, and the roster is hard-coded. Round 1 did not walk the cockpit. |
| Documentation | 7.5 | 8 | Agent documents are executed and three inventories are generated. The operations documents have a broken container path and no upgrade or logs section. |
| Craft | 7.5 | 8 | |
| Security | 7 | 7 | Self-approval and the mention leak are fixed. Anonymous reads still serve lease generations and attachment bytes by default, and nothing reports it. |
| Operations | not graded | 6.5 | Install, backup, restore, upgrade and recovery are sound. Observability is the weak part, and nothing enforces one owner for the background loops. |
| Performance | not graded | 6.5 | Fine for a small fleet today. The desk, the inbox and every write degrade with history, each from one identifiable statement. |

The ten average 7.4. The grade is 7.5 and not 8 because six written
statements are false by execution, and four of them were introduced by the
waves this review follows:

- `AGENT_GUIDE.md` says write bodies refuse unknown fields; `POST /projects`
  drops `visibility` (Wave 1 scoped the rule to routes an MCP tool calls, and
  the guide's sentence says more).
- The Wave 1 results say every seat tool answers within 16,000 characters;
  `my_desk` returned 23,546 with 23 ready issues.
- The desk asks a seat to do a review that `claim_next_ready` will not offer
  (Wave 0b's rule).
- A document and a code comment call benchmark numbers "reproducible" with
  `scripts/seed_benchmark.py`, which no longer runs (Wave 2 made `commit` a
  required keyword and no test runs the script).
- `docs/DOCKER.md` gives a backup command that fails for want of an argument.
- `DESIGN_SYSTEM.md` says "Claim, not assignment"; the issue page's Claim card
  shows the assignee.

None is a security guarantee, which is what capped round 1.

### For agentic use

| Dimension | Round 1 | Round 2 | Why it moved |
|---|---|---|---|
| API ergonomics | 6 | 8 | One result shape, a code on every refusal, a response budget, hints, closed input. Three timestamp shapes and six paging styles remain. |
| The loop | 7 | 8 | The three loop defects are fixed and a test plays the loop. The review pickup gap above is new. |
| Coordination | 6.5 | 5.5 | Graded with a trial for the first time. Path fences do not work on a default instance, a lapsed lease is silent, the review gate can be bypassed, and one outcome costs 142 calls and 382 KB. |
| Contributor experience | 6.5 | 8.5 | An inner loop, a committed bootstrap, change recipes, generated inventories. |

The four average 7.5. For one seat following the documents Athena is now
about an 8. For a fleet it is about a 6, and the fleet is the product's stated
purpose.

## Method

Four reviewers, one lens each, worked against their own throwaway instances
and changed nothing in the repository. Each was briefed in six parts (goal,
context, inputs, action, format, success criteria) and returned findings
labelled as reproduced, read from code, or suspected. The lead then re-ran the
most serious findings on separate instances with its own scripts, or re-ran the
reviewer's script on a fresh instance where the scenario was long. The
verification ledger at the end says which is which.

Counts and timings are from a 4-CPU, 15 GB container. Agents in the fleet
trial were scripted, not model-driven, so call counts are what the documented
loop costs when followed exactly; a real model will spend more.

## The fleet

### The trial

One conductor on the `conductor` profile and three seats on the `seat`
profile, each through the tool surface `athena-mcp` builds, with an operator
on REST. One outcome with three slices, following `CONDUCTOR_RECIPE.md`
literally: plan, delegate, pick up, a yield with a question that the operator
answers, a review that reports HOLD and then PASS on the reworked candidate, a
close gated behind approval, and the outcome closed.

| Role | Tool calls | Bytes received | Identifiers carried between calls | Calls made only to obtain one |
|---|---|---|---|---|
| Conductor | 73 | 141,185 | 41 | 5 |
| Seat, design (yield and resume) | 19 | 62,658 | 9 | 1 |
| Seat, build (HOLD, rework, gated close) | 27 | 76,646 | 15 | 1 |
| Seat, review (HOLD, then PASS) | 23 | 101,880 | 17 | 0 |
| **Total** | **142** | **382,369** | **82** | **7** |

The trial met no refusal and all four issues ended done. Without the yield,
the HOLD and the gate it took 90 calls and 207,955 bytes.

- 39 of the 142 calls (27%) acknowledge one notification each. The conductor,
  as assignee of every slice, had 23 unread after one outcome, and the desk
  previews ten.
- `get_issue_work_context` accounts for 122 KB over 14 calls and `my_desk` for
  85 KB over 11.
- Setting up the outcome took the 21 requests the first review counted. Four
  of them read a 5.9 KB packet only to obtain an ETag.
- The tool list an agent loads at start is 38,351 bytes on `seat` and 48,567
  on `conductor`.

### Findings

| ID | Severity | Finding |
|---|---|---|
| FLEET-1 | Blocks the loop | On an instance with no root catalog, which is the default, only one lease in the whole instance may declare paths. A second seat declaring an unrelated path on another issue is refused with the same 409 `conflict` as one declaring the same path, with no reason and no `next`. It holds across projects, and for a holder the refused seat cannot see. Claiming with no paths succeeds, but `ACTIVE_WORK.md` tells agents not to drop the fence and the conductor recipe tells workers to declare paths. |
| FLEET-2 | Blocks the loop | A lapsed lease produces no event and no notification. The conductor, who assigned and delegated the work, saw four wait timeouts over 100 seconds and nothing in its attention list. This is C4, unchanged. |
| FLEET-3 | Blocks the loop | After the requested reviewer reports HOLD, an account nobody asked can complete an unrelated issue naming the same candidate with PASS, and the queue's state becomes `reported_pass`. The request row carries no verdict. This is C5, unchanged. |
| FLEET-4 | Costs every session | `claim_next_ready` does not offer a review the desk is asking the seat to do. With the recipe's own `blocked_by` link it answers `nothing_ready` while the desk shows one review request. After the reviewer's own HOLD completion and a second request, the issue sits under `not_offered: completed_by_you`: the Wave 0b rule does not count a review request by another account as an answer. A direct `claim_issue` works. |
| FLEET-5 | Costs every session | Acknowledging notifications one at a time is the largest single call cost, and each lease renewal notifies the assignee. No seat profile can acknowledge a set. |
| FLEET-6 | Costs every session | `create_issue` takes no parent, assignee, delegate or blocker, and `set_issue_parent` changes the ETag without returning it. This is C9, unchanged. |
| FLEET-7 | Costs every session | Waiting is a long poll on an instance-wide feed with no project filter: the conductor was woken by 19 events from two unrelated projects. An event carries the verdict, the lease generation and the handoff token only inside a prose `detail` string. |
| FLEET-8 | Costs every session | A conductor cannot resolve an account id. Its tool text names `list_users`, which the profile lacks and a member is refused. The answers of `delegate_issue` let a member enumerate accounts through a write. Onboarding prints the `seat` profile for an account meant to conduct. |
| FLEET-9 | Costs every session | Pause, token revocation and offboarding leave the account's lease and fence active and say nothing about them (C8). An expired lease of a dead agent cannot be cleared by an admin's `complete` (409 `claim_required`); the admin has to claim it and complete it. |
| FLEET-10 | Costs every session | Any eligible account can replace an issue's body with no precondition, and the old text is in no event, no history and no undo. A seat that has lost its lease can still edit. The conductor recipe keeps the contract and acceptance criteria in issue bodies. |
| FLEET-11 | Friction | A seat that is refused or finds nothing ready leaves no trace the operator can read from the API. The attention vocabulary has no signal for a HOLD or BLOCKED verdict. |
| FLEET-12 | Friction | Renewing and heartbeating without progress is derived as `pinging` and never raised (C7). |
| FLEET-13 | Friction | A budget exhausted mid-task gives the seat no `next`, the accountable conductor is not told, and the operator gets one row per refused request. |
| FLEET-14 | Friction | A restarted MCP session cannot replay its own idempotency key: `athena-mcp` mints a new run id per session unless `ATHENA_RUN_ID` is set, and the key then answers 409 `idempotency_mismatch`. |
| FLEET-15 | Friction | Several refusals carry their facts only in prose. `claim_held` names the holder and expiry in `detail`, and its `next` answers `nothing_ready` for a seat with one delegation. |
| FLEET-16 | Friction | `POST /projects` silently drops `visibility`: a project sent as private is created public. 25 of the 67 JSON write bodies in the agent OpenAPI document still accept unknown fields, while `AGENT_GUIDE.md` says write bodies refuse them. |
| FLEET-17 | Friction | A gated conductor closing an outcome and three slices needs four operator decisions. |

Racing itself is sound: eight of eight `claim_issue` races gave one winner and
one `claim_held`, and eight of eight `claim_next_ready` races gave one claim
and one `nothing_ready`.

### The unbuilt requirements, re-measured

All nine reproduce at `0b42d40`: C4, C5, C6, C7, C8, C9, A11, A12 and A13.
Waves 0 to 2 fixed none in passing. C6 and A12 now answer with registered
codes; nothing else about them changed. A13 is wider than first recorded: an
agent packet uses three timestamp shapes, and the agent OpenAPI document has
six paging styles.

## Operations

One person can install, bootstrap, serve, back up, restore, upgrade and
recover a non-container instance from the documents. Each was run. SQLite
integrity, the activity chain and attachments survived an online backup under
writes and two hard kills, a lease survived a kill and lapsed on time, and a
failing migration rolled back whole.

| ID | Severity | Finding |
|---|---|---|
| OPS-1 | Exposure | With defaults, 74 of 156 GET routes answer without a credential, 48 of them REST reads: comments, attachment bytes, lease generations, the work-context packet and the activity CSV. The anonymous rate limit defaults to off. Tailnet validation passes with the default, and `athena-doctor` says nothing about anonymous reads in any mode. This is C11, unchanged. |
| OPS-2 | Exposure on a shared host | The documented bootstrap leaves the data directory 755 and the database 644, while backups are 600. The database holds webhook secrets in the clear, CSRF tokens and password hashes. |
| OPS-3 | Operator blocked | The container first run cannot be completed as written: `compose.yaml` does not forward the bootstrap token its own comment says to pass, the first-admin step points at a host `curl` with no published port, and the documented `athena-backup` command is missing an argument. Read and run in parts; no Docker daemon was available. |
| OPS-4 | Operator blocked | A database whose schema differs from the packaged one refuses to start and names no object. The migration-state messages (missing, future, checksum mismatch, read-only) state the condition without the remedy, and no document mentions them. |
| OPS-5 | Friction with side effects | A duplicate `athena-serve` start runs the background loops before it fails to bind: 30 events produced 47 webhook deliveries. A second process on another port starts with no warning. `OPERATIONS.md` says one process must own the loops; nothing enforces it. |
| OPS-6 | Friction | After a hard kill, a keyed write that was in flight answers `idempotency_in_progress` for about a minute with no owner, then `idempotency_indeterminate` permanently, whether or not it committed. Those rows never expire, by design, and no route or tool lists or resolves them. |
| OPS-7 | Friction | The log cannot answer "what did agent X do, and what failed": access lines have no timestamp, request id, actor or duration; a 500 carries no correlation id and prints its traceback twice; a failed webhook delivery is not logged; there is no metrics endpoint; an agent's 401, 404, 409, 422, 429 and 500 leave no attributable record. |
| OPS-8 | Friction | `/readyz` fails with no reason logged, and a missing database file is silently recreated empty. |
| OPS-9 | Friction | There is no headless way to mint the first token: only the browser form. |
| OPS-10 | Friction | Every start logs "set `ATHENA_COOKIE_SECURE=1` for HTTPS", and setting it is refused by `athena-serve`. A misspelled `ATHENA_` variable is ignored silently. |
| OPS-11 | Friction | `OPERATIONS.md` uses bare command names without saying to put the environment on `PATH`, covers restore in place only, and has no upgrade or logs section. `RELEASE_READINESS.md` says the schema is at migration 0074; the code has 85. |
| OPS-12 | Friction | Nothing documents growth or retention. Notifications, activity, run bindings and approval requests are never pruned, and expired sessions are removed only on logout. |

## Performance

Measured over HTTP against the repo's `athena-serve`, one process, at three
workspace sizes seeded through the real commands: 1,000, 10,000 and 50,000
issues (6,700, 64,300 and 320,300 activity events). The reviewer's rough
mapping, which is an assumption: 10,000 issues is ten agents after one to
three months, and 50,000 is ten agents after six to twelve. All rows were
written within minutes, so queries bounded by a time window see everything as
recent.

| Hot path, median ms | 1k | 10k | 50k |
|---|---|---|---|
| `GET /desk` as a seat | 45 | 259 | 1,285 |
| `GET /desk/ready` | 11 | 50 | 162 |
| Work-context packet, issue with 300 comments | 15 | 15 | 15 |
| `GET /events` | 9 | 9 | 7 |
| `POST /issues/claim-next` | 26 | 52 | 179 |
| `POST /issues/{id}/comments` | 18 | 27 | 75 |
| Operator's priority inbox | 33 | 357 | 1,875 |
| Project brief | 16 | 78 | 368 |
| Throughput with four or more seats, requests per second | 82 | 65 | 37 |

What holds: the work-context packet and the event feed are flat at every
size; 32 parked long-poll waiters cost 5% of a core; `authored_by` adds 0.15
to 2 ms; in three 16-seat races every issue was claimed exactly once; the
process idles at 106 MB and peaked at 172 MB. At the shipped rate limit of 120
requests a minute per token, the limiter and not the database is the ceiling
for a fleet of ten.

| ID | When it bites | Finding |
|---|---|---|
| PERF-1 | From day one | One unrelated assignment voids every stored idempotency receipt: a retry of a keyed write answers 409 `idempotency_authorization_changed`, and the row is kept forever. A trigger bumps one workspace-wide revision on any change to any issue's assignee, project or creator. In sustained runs 30% of receipts ended permanent. `OPERATIONS.md` documents the breadth as deliberate; what is new is its cost in a fleet, where assignment is routine. |
| PERF-2 | From day one | `my_desk` answers above its 16,000-character budget when the ready lane is full: 21,136 characters in the reviewer's run and 23,546 in the lead's. |
| PERF-3 | From day one | `scripts/seed_benchmark.py` fails with a `TypeError`: Wave 2 made `commit` a required keyword and no test runs the script. |
| PERF-4 | Months of history | The desk scans the whole trail on every call for a seat. One statement, the latest visible event id, takes 15, 171 and 840 ms; asked one visibility arm at a time, as the event feed already asks, it takes 0.02 ms and returns the same id. |
| PERF-5 | Months of history | Every issue create and every comment scans the whole full-text index inside the write transaction (0.7, 9.1 and 48.9 ms), because the columns it deletes by are not indexed. Write throughput falls with it. |
| PERF-6 | Months of history | The ready rule reads every issue that is not archived, closed ones included, and `claim-next` does so holding the writer lock (4.5, 35 and 156 ms). The desk runs the rule four times. |
| PERF-7 | Months of history | Notifications are never deleted and every inbox read walks all of a user's rows. |
| PERF-8 | Months of history | The project brief, a search for a common word and the idle sort are linear in history. |
| PERF-9 | Months of history | Three REST lists return everything. The page list returned 2.65 MB for 1,250 pages, and `list_pages` fetches it again for every continuation: 31 calls and 82 MB to list them all. The comment list has no limit. |
| PERF-10 | Bursts and imports | Every bearer-authenticated read takes the writer lock, because the token's last-used stamp is written and committed per request. A wait beyond five seconds is a 500, not a 503 with a retry hint. |
| PERF-11 to 14 | Beyond a small fleet | One process saturates at about four concurrent seats; the automation engine drains ten events a second; a connection per request sets the journal mode each time; the first agent OpenAPI request takes 2.3 to 2.5 seconds. |

## The operator's cockpit

Every step of the operator's loop was walked in Chromium against a workspace
with six agents and 26 issues, then 226 issues and about 55 live leases. Every
page loaded in under 250 ms. Approvals are two clicks to the queue and one to
decide, and the dashboard's ranked queue put an agent's question first.

The main structural finding: the cockpit's work views key on the assignee,
while the agent protocol runs on delegation and the lease.

| ID | Severity | Finding |
|---|---|---|
| COCKPIT-1 | Cannot be done | The operator cannot release another agent's live lease in the browser, so reassigning claimed work does not move it: the new agent is refused `claim_held`. The same admin can do it over REST. |
| COCKPIT-2 | Cannot be done | The Fleet roster and "Assign to a seat" work only for eight seats written into `core/fleet_roster.py`. An empty instance opens with a "Roster drift" alert, agents onboarded through the cockpit are listed as drift, and nothing in the cockpit declares a seat. |
| COCKPIT-3 | Cannot be done | A playbook cannot be started from the browser, though `VISION.md` names playbooks under Direct. |
| COCKPIT-4 | Cannot be done | A removed agent cannot be restored in the browser, and an existing agent cannot be given a new token on either surface. |
| COCKPIT-5 | Every day | The issue page's "Claim" card shows the assignee, not the lease: it read "unclaimed" while an agent held a live lease. Boards and the issue list filed 16 of 18 agent-worked issues under "Unassigned". `DESIGN_SYSTEM.md` says "Claim, not assignment". |
| COCKPIT-6 | Every day | Login lands on a page with no attention on it, and the navigation badge and the dashboard show different "needs you" totals. |
| COCKPIT-7 | Every day | The attention queue is dominated by check-in noise: 54 of 54 fresh claims read "needs attention", a claim with no heartbeat is "high" at once, and the one pending approval sat at row five. Rows are labelled in internal codes and name "issue #12", not its key and title. |
| COCKPIT-8 | Every day | The inbox held 34 unread rows after about 15 agent actions, as raw verbs, generation hashes and whole reports as JSON, with no filter or grouping. |
| COCKPIT-9 | Every day | A question the operator has answered stays "critical" and "waiting on you" until the agent resumes. |
| COCKPIT-10 | Every day | The agents page is one page 7,500 px tall; every action reloads to the top with a message that names no agent. |
| COCKPIT-11 | Every day | An approval row does not say what is being approved: no title and no verdict. |
| COCKPIT-12 | Every day | A completion report is raw JSON on five surfaces, and Undo lives on the activity page only, by numeric target id. |
| COCKPIT-13 | Every day | Errors from plain form posts are bare unstyled pages, and a comment submitted after the session expired is lost. |
| COCKPIT-14 | Every day | Only three panels update live. The badge, the issue page, the questions list, the inbox and the board do not. |
| COCKPIT-15 | Every day | With 226 issues the board was 41,477 px tall with no paging. |
| COCKPIT-16 | Every day | Mission Control's "Copy command" does nothing: its inline script is blocked by the page's own content security policy. Every page load logs one blocked inline style. |
| COCKPIT-17 to 20 | Polish | Layout breaks on long titles and at 390 px; the default button uses the accent the design system reserves for "an agent is waiting on the human"; developer notes ship in the interface; there is no skip link; empty pages give no next step. |

Capabilities found on one surface only (C10): the fleet roster, assign-to-seat,
reported-usage rollups, rooms and space export exist only in the cockpit;
releasing another holder's lease, starting a playbook, restoring an agent, bulk
edit, the review queue and the project brief exist only in the API.

## Verification ledger

| Finding | Reviewer's label | Lead's check |
|---|---|---|
| FLEET-1 | Reproduced | Re-run with the lead's own script on a separate instance: a disjoint path is refused with a body identical to the same-path refusal. |
| FLEET-3 | Reproduced | The reviewer's scenario re-run on the lead's fresh instance: HOLD becomes `reported_pass` by an unrequested account. |
| FLEET-4 | Reproduced | The reviewer's probe re-run on the lead's instance: `nothing_ready` with a review request pending. |
| FLEET-10 | Reproduced | Own script: a delegate's edit without `If-Match` succeeds, and the old text is in no activity row and not in the history. |
| FLEET-16 | Reproduced | Own script: `POST /projects` with `visibility: private` answers 201 with `public`. |
| OPS-5 | Reproduced | Own instance: 30 events and three duplicate starts gave 47 deliveries, 30 distinct. |
| OPS-3 | Reproduced in parts | The backup command run and fails; the compose environment read. Not run in a container by anyone. |
| OPS-6 | Reproduced | Read only: the code says executing and indeterminate rows never expire. |
| PERF-1 | Measured | Own script: the control retry replays; after an unrelated assignment the retry is 409. |
| PERF-2 | Measured | Own stdio call: 23,546 characters with 23 ready issues. In the lead's run `_truncated` was present, where the reviewer saw none. |
| PERF-3 | Measured | Run: `TypeError`. |
| PERF-4 | Measured | The lead seeded 10,000 issues and ran the comparison: 152.88 ms against 0.024 ms, same answer. |
| COCKPIT-2, COCKPIT-5 | Observed | Read only: the eight seats in `core/fleet_roster.py`, and the Claim card's template. |
| Everything else | As labelled above | Not re-run by the lead. |

## Not covered

- Real model-driven agents: every agent was scripted.
- Anything inside a container, a real tailnet bind, OIDC, socket activation.
- Import and export under load, automation with rules enabled, webhook
  delivery to real subscribers.
- History spread over months: all benchmark rows were written within minutes.
- Runs longer than four minutes, a slow disk, more than one process.
- The dark theme by eye, a screen reader, a browser other than Chromium.
- Fleets of more than four agents, or two conductors on one outcome.

