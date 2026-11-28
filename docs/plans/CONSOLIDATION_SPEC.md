# Athena, consolidated: a faster loop, two front doors, a smaller surface

Date: 2026-11-28. Baseline: `e4ee42f` (`main` after the dogfood verdict, #496).
Status: **specified; decisions accepted by the owner's delegation of
2026-11-28 ("I trust your judgement"). Built: K1 (this change).**
Requirement IDs begin `K`. Decision IDs continue the agent-day
specification's, from D36.

## Why this specification

The three specifications before this one each added to Athena. Since August the
surface went from 124 to 148 MCP tools, 362 to 424 routes and 65 to 73 tables,
and November alone has 394 commits. The one usage measurement in the repository
([PRUNE_LEDGER.md](../PRUNE_LEDGER.md), 2026-08-22) found 64 issues, 6 pages and
no automation rule or webhook in the deployment.

A review of `e4ee42f` found the code healthy: `core` imports no feature
package, Aegis and Mentor do not import each other, there are seven runtime
dependencies, and 7,057 tests hold 94.8% line coverage. What it found wrong is
proportion and friction, not correctness:

- **the loop is slow**: the local product gate takes 34:54, so every change,
  by a person or an agent, waits half an hour to learn it is green;
- **the two front doors are unfinished**: an operator signs in to an empty
  page, and an agent still meets three `next` values that are prose and two
  timestamp shapes;
- **nothing has ever been removed**, and no evidence newer than August says
  what could be.

This program adds no feature. It makes the loop fast, finishes the two front
doors, and sets up the first removals on evidence.

## Principles

1. **Nothing is added.** No new route, tool or table while this program runs.
   `scripts/surface_counts.toml` records only removals.
2. **Every item has a number before and after**, measured the same way.
3. **A removal follows the prune ledger.** A verdict needs evidence from the
   deployment, and is recorded so it can be reversed. Prefer park to cut.
4. **A wire change must pay an agent or the operator**, and is listed under
   Compatibility.

## Measured on the baseline

| measure | value |
|---|---|
| source, tests, checker scripts, docs | 99,557 / 172,814 / 14,642 / 23,897 lines (104 docs) |
| surface | 424 routes, 148 tools (92 in no working profile), 73 tables |
| local product gate, `-n 3` | 34:54 for 7,057 tests |
| a test's app: migrate, then first two requests | 0.18 s + 0.74 s; a later request is 0.01 s |
| `next` values that are prose, not a call | 3 (`complete_claim`, its close, the ready lane's open handoff) |
| timestamp shapes in MCP answers | 2 (`2026-11-27 14:14:41` and RFC 3339) |
| what a signed-in operator sees at `/` | two link cards |

## Requirements

### K1. The loop

**K1.1 Routes are built once per process.** FastAPI works out each route's
dependency graph and models per included-router object, on first match: 0.74 s
for Athena's routes. `create_app` included 62 routers into every app, so every
test paid it again. The routers are included once into a process-wide site
router, and each app takes those same included-router objects. An app still
owns its state, middleware, exception handlers and lifespan. The Jinja
environment is built once per process the same way.
`app.dependency_overrides` cannot reach shared routes, so setting one is
refused with the name of the seam that works
(`athena.main.SITE_DEPENDENCY_OVERRIDES`).

**K1.2 A test database is copied, not rebuilt.** A test worker runs the
migrations once per migration inventory and fills each later empty database
from that through SQLite's backup API (about 10 ms for 0.18 s). `db.migrate`
still runs on the copy, so the checksum ledger is validated on every database.
A database that already has a table, and a test with its own inventory, take
the real migrations. Test harness only: nothing in `src/` changes for K1.2.

*Done when* the local product gate is under 15:00, the route counts and the
generated agent API document are unchanged (the OpenAPI document is what it
was), two apps over two databases see only their own rows and limits, and a
test proves the copied database equals a migrated one.

### K2. The agent's contract, finished

**K2.1 `next` is a call, a list of calls, or null.** A call is
`{tool, arguments?, hint?, why?}`. The three prose values become calls; the
sentence each carried moves to `hint`.

**K2.2 One timestamp shape in every MCP answer.** REST packets already take
RFC 3339 through `core/packet_time.py`; issue, approval and comment answers
still carry SQLite's shape. The MCP server stamps every answer at its one
exit. REST is unchanged.

**K2.3 One seam for the MCP SDK's private parts.** The six references to
FastMCP internals move behind one module with a test that fails by name when
the SDK moves them.

*Done when* a test walks every answer of the scripted agent's day and finds no
prose `next` and no timestamp outside the one shape.

### K3. The operator's first minute

**K3.1 A signed-in operator lands on the dashboard.** `/` shows it; signed out,
`/` stays the landing page.

**K3.2 Names, not ids or internals.** The issue page says who created the
issue, not `Created By 1`, and a claim in the history reads as a claim, not a
generation hash and `coordination_root null`.

**K3.3 The attention card reads as sentences.** `Open owning surface` and
`examined 1 of 1 considered candidates, not a full input population` become
words an operator would use. The design system's rule stands: every count
states its scope in the same block.

*Done when* before and after screenshots of `/`, the dashboard and an issue are
in the pull request, and `tests/test_cockpit_polish.py` holds the new copy.

### K4. Subtraction

**K4.1 Evidence from the deployment.** The owner runs two read-only commands
against the dogfood deployment and pastes the output:

```bash
python scripts/prune_evidence.py <database> --markdown
journalctl -u athena --since "90 days ago" | grep -o 'route=[^ ]*' | sort | uniq -c | sort -rn
```

The second is new evidence: the request log names every route served, so the
rows the ledger marks "n/a, leaves no trace" (graph view, answerability,
search, export, recovery) become measurable.

**K4.2 Verdicts by the ledger's own rule.** Cost by subsystem, source plus
tests, attributed by file name:

| subsystem | lines | ledger status |
|---|---|---|
| automation rules | 5,997 | watch: 0 rows in August |
| outbound webhooks | 2,399 | watch: 0 rows in August |
| graph view | 1,716 | watch: leaves no trace |
| answerability page | 469 | watch: leaves no trace |
| saved filters | 3,033 | keep; reached by neither scripted story |
| sprints | 2,445 | keep |
| fleet metrics | 2,851 | keep |

A watch-list row with no use in the new evidence and no runtime dependant is
parked. A parked subsystem that is still unused at the next review is cut.

**K4.3 Tools nobody calls.** Of 148 tools, 92 are in no working profile. A
tool in none, whose route the request log never shows, is retired with its
client method and tests.

**K4.4 Documents.** Specifications of shipped features and finished plans move
to `docs/archive/`, listed as a directory. `docs/` keeps what an operator or a
contributor reads to use or change Athena today.

*Done when* the surface counts are lower than 424, 148 and 73, each removal is
a ledger entry, and `docs/` has no more than 45 top-level documents (61 today).

## Considered and not done

- **Generating the MCP client and tools from one declaration.** 125 of 151
  client methods are one REST call, 1,272 lines in all (1.3% of the source),
  and 157 test files drive the client by name. The rewrite costs far more than
  it saves.
- **Splitting the large files** (`ops.py`, 2,922 lines). It changes no
  behaviour and no cost.
- **Merging the parallel issue and page stacks** (drafts, undo, ETags,
  comments). A shared abstraction over two things is a risk with no measured
  gain.
- **One shared set of test helpers** (93 files define their own `_bootstrap`).
  K1 gets the speed without touching them.

## Corrections to the review that preceded this

- "`next` has about seven shapes." It has one call shape with optional keys,
  a list of calls on the desk, null, and three prose exceptions (K2.1).
- "Generate the MCP layer from one declaration." Measured, and not worth it.
- "The gate is slow because of its machinery." It was app construction (K1).

## Compatibility

- K2.1 changes three `next` values from a string to an object.
- K2.2 changes timestamps in MCP answers that were in SQLite's shape.
- K3.1 changes what `/` returns to a signed-in browser.
- K4 removes routes and tools; a parked route answers 410 with a pointer.

Not changed: REST bodies other than the three `next` values, visibility
(hidden reads as missing), migrations (forward only), coverage floors.

## Decisions

Accepted by delegation, each reversible:

- **D36.** No additions while this program runs.
- **D37.** Shared routes refuse `app.dependency_overrides` instead of ignoring
  it; the site-wide mapping is the seam.
- **D38.** Timestamps are unified at the MCP exit, not in REST.
- **D39.** `/` is the dashboard for a signed-in session.
- **D40.** No subsystem is parked or cut before K4.1's evidence arrives.

## Delivery

One pull request per requirement group, merged when hosted CI is green and the
local product gate passes on its head: K1 with this specification, then K2,
K3, and K4.4. K4.2 and K4.3 follow the owner's evidence.

## Scorecard

| measure | baseline | target | now |
|---|---|---|---|
| local product gate | 34:54 | under 15:00 | (K1) |
| prose `next` values | 3 | 0 | 3 |
| timestamp shapes over MCP | 2 | 1 | 2 |
| signed-in `/` | link cards | dashboard | link cards |
| top-level documents | 61 | 45 or fewer | 61 |
| routes, tools, tables | 424, 148, 73 | lower | 424, 148, 73 |
