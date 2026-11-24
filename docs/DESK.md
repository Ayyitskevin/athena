# The Desk — one call, full orientation

An agent starting a session used to discover its own situation through five or
six reads: `whoami` for identity and limits, the delegation inbox for work, the
run-control inbox for asks, notifications for mentions, the worker registry for
kill requests, its budget for what it may spend. Each with different bounds,
none of them saying what changed while it was away, and a fresh context
continuing someone else's run had no single place to look at all.

The desk is that place. **Who you are, what is asked of you, what you are
holding, and what changed since you last looked** — one bounded read.

The cubicle inside it — one chair, fenced paths, checkout hint — is the
[Office](OFFICE.md). `GET /office` / `my_office()` is that packet alone.

```text
GET  /desk                          → the whole board
POST /desk/cursor  {"after_id": N}  → record how far you have drained
```

MCP: `my_desk()` and `advance_desk_cursor(after_id)`.

## The loop

```text
my_desk()                        → orient: asks, work, signals
   ↓ act on what you found
recent_events(after=cursor.after_id) → drain the trail from your position
   ↓
advance_desk_cursor(after_id=<the last event you actually handled>)
```

Advance to the last id you **processed**, not the newest you saw. The desk's
`signals.latest_visible_event_id` is what a fully drained reader would use.

**Waiting instead of polling.** Once drained, do not sleep and re-read. Call
`wait_for_events(after=<your cursor>, wait=25)` (REST: `GET /events?after=N&wait=S`):
the server parks the request up to `wait` seconds (0 to 25) and answers with the
first batch you may read, or empty at the timeout with `next_after` still your
cursor. The answer carries `wait = {requested_seconds, elapsed_seconds, outcome}`;
`outcome: "capacity"` means every parking place was taken and this is the immediate
answer, so back off a little before waiting again. The visibility rule is the
immediate read's: an event you may not see neither appears nor wakes you. On
entry, before it reads, the feed records any lapsed lease you answer for (you are
the issue's assignee or delegated it to the holder) as one `lease_lapsed` event,
so a wait loop learns of a lapse within one wait
([ACTIVE_WORK.md](ACTIVE_WORK.md#a-lapsed-lease-is-an-event)). The caps
live in `src/athena/core/event_wait.py` (32 parked requests per process, 25 s each,
a half-second wake bound).

**Waiting on what you asked about.** `project_id=P` keeps the project's own events
and those of the issues that were in it when they were recorded; `mine=true` keeps
the events of the issues you are assigned, delegated or created, and those you
delegated to another account, as they stand at the read. Either answers, and wakes a wait, only with a matching event: work in an
unrelated project no longer wakes you. A project you cannot see answers exactly as
one that does not exist, an empty batch and a wait that runs to its timeout. Both
are filters, so a filtered read cannot justify advancing the cursor: when one wakes
you, drain unfiltered from your cursor before you advance it.

`mine` is the set at the read, not at the event. Once an issue is reassigned away
from you, or you are removed as its delegate, its events leave your `mine` feed,
the event that took it included; only the unfiltered feed shows that one. A
subtask someone else files under your issue is not yours. A lapse the feed records
for you is always in `mine`, because the accounts that answer for a lease are its
issue's assignee and the account that delegated it. Both filters are cheap from a
cursor near the newest event, which is where a wait loop reads. A drain from an old
cursor reads every later event of your issues, or of the project, before it
returns a page, so drain unfiltered and keep the filters for waiting.

**Facts are fields.** A lease event carries its `generation` (claimed,
lease_renewed, lease_lapsed, claim_yielded, claim_handoff_resumed, claim_completed,
lease_released, holder_budget_exhausted, and a delegation_declined that released a lease); a yield and a handoff resume carry
the `handoff_token`; a completion that reported one carries its `verdict`. Each is
the value the command recorded, and the prose `detail` names the same values for a
person. An event with none of them has none of the keys, and an event recorded
before migration 0086 has them only in its `detail`.

## The lanes

| Lane | Holds | Bound |
|---|---|---|
| `identity` | id, name, role, agent flag, paused, scopes, budget, action kinds needing approval; `notebook` (see below) | `notebook.excerpt`: the first 12 lines |
| `asks.run_controls` | controls addressed to you that nobody has answered | 20 |
| `asks.worker_kill_requests` | your workers the operator asked to stop, still unconfirmed | 50 matching workers, with exact `total`, `limit` and `clipped` |
| `asks.review_requests` | visible current requests for you and superseded targets on review issues you still hold; present and empty when there are none | 20, `visible_total`, `returned`, `clipped` |
| `asks.claim_handoffs` | context handed to you on issues you hold, unacknowledged | 20 |
| `work.delegations` | your delegation inbox | 20, `has_more`/`next_offset` |
| `work.leases` | `items`: issues you hold NOW, with `active` and the 0057 generation. Each item's `progress` gives your own `last_work_at`, `work_age_seconds` and `work_state` (`recent` / `stale` after 7 days / `unknown` when you have recorded no work), the same block as active work ([ACTIVE_WORK.md](ACTIVE_WORK.md)). Holding is not working: a check-in or renewal never resets it, and any qualifying event counts however small. `lapsed`: your expired rows on open or done issues, labelled `issue_done`; holders settle their own rows deliberately with `complete_claim` and the observed generation. Reading never settles a row; only an admin may settle another actor's expired row, naming its generation | 20 each, `total` / `lapsed_total`; `lapsed_clipped` says whether rows are omitted; `lapsed_oldest_expires_at` is null when empty or clipped (unknown when clipped); total/clipped distinguish these cases, and claim order is not expiry order |
| `work.ready` | issues you could claim now, in claim order (see below); each says why it is yours, the previous holder of a lapsed lease, and any open handoff. Also `GET /desk/ready?limit=` alone | 20 (REST 1-50), `total` and `clipped`; `held_by_you` counts your live leases |
| `work.waiting_on` | issues you delegated that the delegate has neither claimed nor worked on for 24h | 20, `total` |
| `work.waiting_on_operator` | what **you** asked a human that is still open: your blocking questions nobody has resumed (`kind: question`) and your pending approvals (`kind: approval`), oldest first. Read it before asking again. Nothing enforces a response time. `not_offered`: issues the ready lane holds back from you until another account answers (see below), each with `reason` `awaiting_answer` or `completed_by_you`, `settled_by_you`, and `settled_at` only when the settlement was yours | 20, `total` across both kinds; `not_offered` 20 with its own `total` |
| `signals` | unread notifications, `events_since_cursor`, `latest_visible_event_id` | 10 notifications, count capped at 500 |
| `cursor` | your stored position, or `null` | — |
| `office` | your chair, fence overlaps and what to sit in next, as `GET /office` answers it ([OFFICE.md](OFFICE.md)) | one chair |

**Over MCP the desk fits its budget.** `my_desk` answers within 16,000
characters whatever the lanes hold. A desk over it is cut by shape: the largest
lists and texts give up rows and characters first, the ready lane keeps at least
its first three rows and the identity block stays whole. Each cut is named under
`_truncated` with its whole size and `next`, the call that reads on: the lane's
own tool for delegations (`list_my_delegated_work`), notifications
(`list_notifications`) and run controls (`list_run_controls`), and otherwise
`my_desk(section=<the cut's key>, offset=<what was kept>)`, which answers that
one place of a fresh desk read: a list's rows or a text from `offset`. REST
`GET /desk` is not cut.

**One dialect.** Every lane is present when empty, every timestamp is RFC 3339
UTC (`2026-11-21T18:19:13Z`), and every `<x>expires_at` (a lease, a chair, a run
control, `lapsed_oldest_expires_at`) has `<x>expires_in_seconds` beside it, counted
from the one clock read the whole desk is built at
([conventions](AGENT_GUIDE.md#conventions-every-agent-packet-keeps)).

**It composes, it does not compute.** Every lane calls the reader that already
owns that surface with *your* visibility applied — the same run-control inbox
`list_run_controls` serves, the same delegation list, the same worker registry,
the same notification inbox, the lease table's own holder read. So the desk
cannot show you anything the owning tool would hide, and it cannot disagree
with the surface that owns each number.

## The ready lane

An issue is **ready for you** when all of these hold:
- you can see it;
- its status category is to-do or doing, and it is not archived;
- nobody holds a live lease on it;
- no issue that is not done blocks it;
- you may claim it under the claim command's own policy: you are its assignee, a
  delegated contributor, or an admin;
- it is not awaiting an answer. Athena reads the issue's newest settlement, the
  latest `complete_claim` or `yield_claim` on it. After a `needs_input` yield the
  issue is ready for no account. After a completion, with or without a report, it
  is not ready for the account that completed it; other accounts are unaffected.
  Either hold lifts when a newer comment, status change, assignment change or claim
  is made by an account other than the one that settled it. The Automation actor's
  events and imported history never count. `blocked` and `capacity` yields hold
  nobody back. A completion is also answered by a review request: when the
  issue's newest request names you, was made by another account after your
  completion, and asks about the source's current candidate, the issue is ready
  for you again. It lifts no `needs_input` hold, answers for nobody it does not
  name, and a request you cannot see answers nothing
  ([decision](decisions/2026-11-22-review-request-answers.md)).

The lane never offers work a claim would refuse. Three rules worth knowing:
- **A blocker you cannot see still blocks.** The lane does not say what it is.
- **A lapsed lease makes the issue ready again.** The row shows who held it
  last. Nothing is swept: the next claim is the deliberate re-acquisition.
- **A yielded issue carries its open handoff.** After claiming it, resume the
  handoff with `resume_claim_handoff`.
- **Held-back work is listed, not hidden.** An issue kept out only by the
  awaiting-answer rule appears in `work.waiting_on_operator.not_offered` with its
  reason, so the lane's `total` is exactly what `claim_next_ready` can serve you.
  A direct `claim_issue` ignores the hold, and is itself an answer: it is how
  another account takes over a `needs_input` handoff.

Order: delegated to you first, then priority (urgent first), then oldest. The
`is:ready` query atom ([QUERY.md](QUERY.md)) uses the same rule. The
awaiting-answer rule is recorded in
[the 2026-11-22 decision](decisions/2026-11-22-ready-rule-awaiting-decision.md).

`claim_next_ready(limit=1, lease_seconds=None, project_id=None,
idempotency_key=None)` (MCP) uses one `POST /issues/claim-next`. The server reserves
the writer, checks the account's current possessions, selects eligible ready work,
and invokes the ordinary claim command in that transaction. Readiness and the
returned metadata come from that acquisition. Later changes can still block work.

Omit `project_id` to include all eligible projects and backlog, or supply one
visible project ID. An explicit missing or hidden project returns the same 404.
`limit` (1–5, default 1) bounds ordered candidates attempted, never leases acquired.
An ordinary claim conflict can advance to the next candidate within that bound;
its attempt records only the visible issue ID and generic `conflict` outcome.

The response has `schema: athena.ready_claim.v1`, `observed_at`, `claimed` and
`attempts`. A successful acquisition includes the lease (with `generation`),
`issue_etag`, `work_context_etag`, `previous_lease` and `open_handoff`. It has no
top-level ETag. No caller `If-Match` is accepted on this selection route; a direct
`claim_issue` against a revision you chose still requires that revision's tag.
No-acquisition outcomes are:

- `already_holding`: the account has any active lease, across all projects,
  including hidden, done and archived issues. Only its count is returned.
- `nothing_ready`: no eligible candidate exists in the requested scope.

- `candidates_conflicted`: all attempted candidates conflicted. This replaces the
  old client-only `lost_race` result; selection and acquisition are now serialized.

Every no-acquisition outcome also carries `review_request`: null, or the newest
review request naming you that `asks.review_requests` shows as `requested`, with
the call that claims its review issue. Pickup does not claim a review: the recipe
links a review issue `blocked_by` the build it reviews, and that blocker keeps
its meaning, so the answer names the review instead.

```json
{"request_id": 7, "review_issue_id": 12, "candidate_id": 3, "source_issue_id": 11,
 "pending_total": 1,
 "next": {"tool": "claim_issue",
          "arguments": {"issue_id": 12, "if_match": "\"<the review issue's ETag>\""},
          "hint": "pickup does not take a review; ..."}}
```

`if_match` is the review issue's ETag when the answer was made; a replayed receipt
carries it as it was, so a 412 means read the issue again. `pending_total` counts
the waiting requests among the desk lane's newest, not every request ever made.

Overlapping pickup calls for one account cannot acquire different issues while
its first possession remains active, including calls using different tokens or
runs. Ordinary direct claims still permit several possessions; this is a queue
pickup rule, not a global lease quota or evidence of a running worker.

Choose an `idempotency_key` before a retryable call and keep the exact request
and credential; the run may differ, so a restarted session's retry still
replays. Success and no-acquisition outcomes both replay as
historical receipts, including the original expiry and `lease.active` value.
Replay never renews or reacquires. A new polling intent needs a new key; reconcile
an uncertain outcome before changing its key. Completed receipts have finite
retention (default 24 hours), after which reuse is a new request. An indeterminate
request fails closed instead of automatically executing again.

Fetch current full work context after claiming or recovering a receipt, before
starting work; explicitly resume an open handoff. Context tags do not prove you
read that context. See [ATOMIC_PICKUP_SPEC.md](ATOMIC_PICKUP_SPEC.md) for the
concurrency and retry boundaries. `GET /desk/ready` remains a read: two agents
can see the same available item before either acquires it.

Each desk, ready and office response uses one database snapshot and one lease
clock. Live counts include all held leases even when the displayed list is
bounded; handoff clipping is evaluated across all visible live claims before
the handoff limit. An office chair retains your own lease metadata after
project access is revoked, but its hidden issue title, key, project and blocker
details are withheld. Backlog issues remain visible.

## Two distinctions the desk refuses to blur

**`null` is not `0`.** A cursor you have never set reads as `null`, and
`events_since_cursor` is `null` with it. "I have never looked" and "nothing has
happened since I looked" are different facts; collapsing them would tell a
fresh agent it was caught up.

**A capped count says it is capped.** `events_since_cursor` stops counting at
500 and sets `events_since_cursor_capped`. An exact five-figure backlog is
noise; "500+, drain from here" is the number you can act on.

## The cursor

Your cursor is **personal bookkeeping**: it records no activity event, tells
nobody else anything, and belongs to exactly one reader (the
personal-state category in [`COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md) is
the citation for the missing audit event). It is stored per `(user, name)`;
`desk` is the only name v1 knows.

It **moves forward only.** Re-acknowledging the same id is a harmless no-op, so
a retry is always safe. A lower id is refused with `409` — and migration 0073
refuses it again at the database, for any writer that skips the command.
Unsaying an acknowledgement would be a claim about history, not a position.

It **cannot pass the present.** An `after_id` above the newest event id is
refused with `422` and code `cursor_ahead`, and the cursor does not move:

```json
{"detail":"after_id 999999 is past the newest event; the newest event you can see is 22",
 "code":"cursor_ahead","latest_visible_event_id":22}
```

`latest_visible_event_id` is the newest event *you* can see (`null` if none), the
same value as the desk's `signals.latest_visible_event_id`; it is never the
trail's overall maximum, which would disclose activity you cannot read. The valid
range is therefore your current position up to the newest event. Before this
bound, one mistaken value could not be corrected (advance-only) and hid every
later event; migration 0085 rewound any such cursor once to the newest event id
(or forgot it, on an empty trail) and recreated the 0073 trigger unchanged.

### Your notebook

An agent keeps one notebook page: notes it wants its next session to read. It is
a Mentor page labelled `agent-notebook`, in a private space the agent owns, so
only that agent and the operator (an admin) can read it.

- **Created on first use, never by reading.** `POST /desk/notebook` (MCP
  `open_my_notebook`) finds or creates it: 201 the first time, 200 after, always
  the same page.
- **On the desk.** `identity.notebook` gives `page_id`, `updated_at`, and the
  first `NOTEBOOK_EXCERPT_LINES` (12) lines with `total_lines` and
  `excerpt_truncated`. It is `null` until the notebook exists.
- **Appending.** Edit the page with `If-Match` set to its ETag (`update_page` with
  `if_match`). A parallel session holding the old tag gets a `412` instead of
  clobbering the page: re-read, merge, retry. Every revision stays in the page's
  version history.
- **What it is not.** Nothing summarises it automatically, and it is not a
  runbook; issue runbooks stay the vetted store.

### Since you last looked (the operator too)

The desk serves the operator as well: the cursor table takes any user, so a human
or admin uses the same `desk` cursor with no migration. `GET /desk/since`, and the
"Since you last looked" card on the dashboard, count what arrived after your
cursor:

| Kind | Counted from |
|---|---|
| new questions from runs | claims yielded with a blocking question |
| claims completed | each with the counts its report carries, or "no report"; never an overall verdict |
| new blockers | `blocks` or `blocked_by` links |
| approvals raised | `approval_requested` events |
| newly stalled | in-progress issues whose last work (creation when none) crossed the 7-day stale threshold since you last marked seen |
| other events | everything else in the window |

- **Reading never moves the cursor.** Only the card's **Mark seen** button does
  (or `POST /desk/cursor` with `last_shown_event_id`).
- **It moves to the newest event the card examined.** The read takes the oldest
  500 events after your cursor, so marking seen never skips one you were not
  shown, and anything that arrived after the page rendered is still new.
- **Native events only,** behind the trail's own visibility gate. Stalled rows
  apply project visibility.
- **When there is nothing,** the card says "Nothing new since <time> UTC".

## What the desk does not claim

- **Not a lock, a lease, or a queue.** Seeing work here reserves nothing. Two
  agents can read the same contents at the same instant; claim work through
  the delegation and lease tools, which own that.
- **Not liveness or health.** An open control is a request nobody answered. A
  worker's `kill_state` and `last_reported_at` are what the worker *reported* —
  Athena does not observe processes ([`WORKERS.md`](WORKERS.md)).
- **Not a delivery guarantee.** `events_since_cursor` counts what is visible to
  you at this read. Events on targets you cannot see are not counted and never
  will be — the count and the drain use the same visibility gate, so they
  agree.
- **Not a fleet view.** There is no parameter naming another reader; you get
  yours. Operators read the admin surfaces, which stay admin-only.
- **Not a snapshot you can hold.** It is true at read time. A lease shown as
  `active` may lapse a second later; `active` is the clock's verdict at that
  read, never a stored state.


For exact-version build/review work, fetch **full** source-issue work context to
read `review_tracking`; brief mode omits it. Submission and review use optional
fields on the existing `complete_claim` tool under your own active generation.
They do not acknowledge events, change issue status or grant acceptance. A
conductor can inspect current visible candidates with `get_project_review_queue`.
See [CLOSEOUT_RECIPE.md](CLOSEOUT_RECIPE.md#exact-version-submission-and-review).
