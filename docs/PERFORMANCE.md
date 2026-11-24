# Hot-path performance: the seeder, the benchmark and the medians

Principle 12 of the [Athena-in-use specification](plans/ATHENA_IN_USE_SPEC.md)
says a hot statement does not read history: the desk, pickup, the inbox and
every write take time that depends on open work, not on everything that ever
happened. This page says how that is held and how it is measured. They are two
different things.

- **The gate holds query plans and work, never timings.** A timing moves with
  the machine and its load; a plan does not. Each hot statement that was found
  reading history has a test that asks SQLite for its plan and names what must
  not appear in it (a scan of `activity`, a walk of every issue) and the index
  or key it must use; where statistics could change the plan, it asks again
  after ANALYZE. Where a plan cannot show how far a read goes, a test counts
  SQLite's virtual-machine operations for the read on a small and a large
  history and holds the ratio. Those tests are listed below.
- **The medians are for people.** [PERFORMANCE_MEDIANS.json](PERFORMANCE_MEDIANS.json)
  is where the benchmark below records the median of each hot path at 10,000
  issues, with the commit, the machine and the load average they were measured
  at. They are recorded on a quiet machine from a committed tree, and
  `--record` refuses otherwise. A number in it describes that machine: compare
  a change by running the benchmark twice on the same machine, before and
  after.

## Seeding a workspace

`scripts/seed_benchmark.py --shape fleet` writes a workspace that a small fleet
has worked in for months, through the application commands, so every
projection, notification and event is the one the product writes. Six agent
seats and an admin operator; five public projects, one private project no seat
belongs to, and the backlog. Of the issues a seat can see, 8% stay open and
assigned: some plainly ready, some in progress, some delegated to a second
seat, some blocked by another open issue, yielded (for a question, or as
blocked), completed and left open, or archived. The rest are claimed,
commented on, completed and closed. One open issue carries 300 comments.

It seeds no live leases, review requests, project or space members, desk
cursors or custom statuses: the ready rule's clauses for those read no rows in
this workspace, and `tests/test_ready_open_candidates.py` builds them instead.

```
.venv/bin/python scripts/seed_benchmark.py /tmp/fleet1k.db --shape fleet --issues 1000
.venv/bin/python scripts/seed_benchmark.py /tmp/fleet10k.db --shape fleet --issues 10000
.venv/bin/python scripts/seed_benchmark.py /tmp/fleet50k.db --shape fleet --issues 50000
```

The three sizes are the ones the round-2 review measured at: about 6,500,
65,000 and 325,000 events. The review's mapping, which is an assumption, is
that 10,000 issues is ten agents after one to three months and 50,000 is ten
agents after six to twelve. Seeding 10,000 issues took 26 s on a quiet 4-CPU
machine and 70 s on a busy one. The database is about 65 MB at 10,000 issues
and 330 MB at 50,000: seed one size at a time and delete it, and its `.json`,
when you are done.

The seeder writes `PATH.json` beside the database, mode 0600 and ignored by
git: the bearer tokens of the accounts it made, which open that scratch file
and nothing else. It runs ANALYZE only with `--analyze`, which the map and
any medians recorded from it say; no Athena database has statistics unless
someone runs it (the script's docstring says why that matters). `--events`
belongs to the default `trail` shape, and the fleet shape refuses it.

## Running the benchmark

```
.venv/bin/python scripts/benchmark_hot_paths.py /tmp/fleet10k.db
.venv/bin/python scripts/benchmark_hot_paths.py /tmp/fleet10k.db --record docs/PERFORMANCE_MEDIANS.json
```

It copies the database with SQLite's backup API, starts the installed
`athena-serve` on the copy with the token rate limit and the background loops
off, and asks each read 30 times over one keep-alive connection after three
unrecorded calls: the desk as a seat and as the operator, the ready lane,
"since you last looked" with no cursor, the work-context packet of the
300-comment issue, the event feed 50 events behind, the operator's priority
inbox and a project brief. It then times the same desk, ready and since reads
in process, without HTTP, and runs the write loop: each seat in turn picks work
with `POST /issues/claim-next`, comments, completes and closes it, ten rounds.

The copy and the server's log go in a directory of their own under
`--scratch-root` (the system's temporary directory by default). The server
runs in its own process group, and a watcher process stops it and removes the
directory when the benchmark ends, however it ends.

`--record` writes the medians, the commit, the machine and the load average to
the file. It refuses when the one-minute load average is above the CPU count
or when the checkout differs from the commit it would name; `--record-anyway
REASON` records regardless and writes the reason and those conditions into the
file.

## The plan tests

Each row is a hot statement and the test that holds its plan. A change that
makes a hot statement stop reading history adds its row here, with the test.

| Statement | Route | Test |
|---|---|---|
| The lapsed leases the event feed's observers record | `GET /events` | `tests/test_lease_lapsed.py` |
| The newest event the caller can see (`desk.latest_visible_event_id`), one visibility arm at a time | `GET /desk`, `POST /desk/cursor` | `tests/test_desk_trail_reads.py` |
| The oldest events after the desk cursor (`operator_since._events_after`), one arm at a time | `GET /desk/since` | `tests/test_desk_trail_reads.py` |
| The ready rule (`ready_queue._evaluate`), over open candidates, once per desk read | `GET /desk`, `GET /desk/ready`, `POST /issues/claim-next` | `tests/test_ready_open_candidates.py` |
| The `is:ready` query atom (`ready_queue.ready_clause`), over the same open candidates | `GET /issues?q=is:ready`, saved filters | `tests/test_ready_open_candidates.py` (its work; its plan is the calling query's) |

## Where the bounds do not hold

Each is a condition, with what it costs; none changes an answer.

- **A caller whose newest events of a kind are all hidden** (`GET /desk`,
  `GET /desk/since`). Each visibility arm walks newest first and stops at the
  caller's first visible event of its kind, so it walks past every hidden one
  first: a seat whose projects went private, or quiet, while the rest of the
  workspace kept working pays what the OR-joined read paid. Not bounded; no
  worse than before.
- **An issue closed under a project's own closed status** (`shipped`). The
  same name on an issue in the backlog, or in a project that does not list it,
  is open (it falls to the neutral `doing`), and no existing index tells those
  apart from the closed ones. So the ready rule reads each issue carrying that
  name once, by its index entry and its project, and drops it before the rule
  runs on it. That is linear in such issues, at about a tenth of what the rule
  cost per issue before; an index over (status, project) would remove it and
  needs a migration.
- **A project that makes a closed name open** (`done` as a to-do status). Its
  issues under that name are open by definition, so the rule reads that
  project's issues; every other project's closed issues stay unread.
- **The `is:ready` atom on a database with statistics.** The atom's clause
  offers the open candidates as a key lookup, but the plan is the calling
  query's: after ANALYZE SQLite may scan a small issues table instead. Its
  work test passes with and without statistics.
