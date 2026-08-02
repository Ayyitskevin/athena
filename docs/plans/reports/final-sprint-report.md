# Run Report — The Final Sprint

Guide: [`docs/OPUS_FINAL_SPRINT_ATHENA.md`](../../OPUS_FINAL_SPRINT_ATHENA.md)
(written against `eafd039`). Branch: `claude/athena-buzz-integration-4hkyx8`.
Starting SHA: `e8200d1` (origin/main at session start — the guide's own merge
commit, so the tree is one commit ahead of what the guide was written against;
no drift affecting any stage was found beyond the corrections recorded below).

This report is the handoff artifact. Every stage appends its claims table,
design, deviations, and validation evidence before the next stage begins.

---

## Stage F-1 — The Desk

### Phase 0 — claims verified against the tree

| Guide claim | Verdict | Evidence |
|---|---|---|
| Next migration number is 0073 | CONFIRMED | `src/athena/core/migrations/` ends at `0072_activity_chain.sql` |
| `whoami` shape exists in MCP | CONFIRMED | `mcp/server.py:479` — returns identity + scopes + budget + `approval_required` + run. The desk reuses the same underlying reads rather than re-deriving them |
| `delegations.list_delegations(conn, subject, *, viewer, include_closed, limit, offset)` | CONFIRMED | `aegis/delegations.py:52`; returns a dict (not a bare list) — desk reads its items/total |
| `budgets.observed(conn, user_id)` | CONFIRMED | `core/budgets.py:149`; rolled forward at read time, `None` = unbudgeted |
| `approvals.gated_kinds(conn, user_id)` | CONFIRMED | `core/approvals.py:464` |
| `notifications.unread_count` + `list_notifications` | CONFIRMED | `core/notifications.py:240` / `:211`, both visibility-gated by `actor` |
| `run_control_commands.readable_controls(conn, *, actor, run_id, state, limit, now)` | CONFIRMED | `core/run_control_commands.py:739`; a non-admin gets its own inbox — exactly the desk's need, with `state="open"` |
| `workers.list_workers(conn, *, agent_id, limit, stale_seconds, now)` | CONFIRMED | `core/workers.py:212`; derived `kill_state` per row |
| "leases I hold" reader exists | **CORRECTED** | `aegis/leases.py` has only `get_lease(issue_id)` — there is NO holder-scoped reader. `fleet_work.build_active_work` has a holder-filtered query but it is an admin fleet projection, not a self-read. Deviation D-1 below |
| `claim_handoffs` has a "my pending acknowledgments" reader | **CORRECTED** | `aegis/claim_handoffs.py:146` offers `open_handoffs_for_issues(issue_ids)` only. The desk composes: my leases → their issue ids → open handoffs. No new reader needed |
| `GET /events` exposes `after`/`next_after`/`has_more` | CONFIRMED | `core/events_api.py:50-57` |
| `watches` table is polymorphic with no `target_kind` CHECK | CONFIRMED | migration 0023: PK `(user_id, target_kind, target_id)`, no CHECK — F-3 needs no migration |
| Personal state is a documented category the cursor can use | CONFIRMED, with a limit | `COMMAND_MIGRATION.md:41` — owner-scoped mutation SQL, **no audit events**, exactly one data-module writer each; and explicitly *"an exception, not a lane"*. `check_write_ownership.py:62-68` allowlists `saved_filters.*` and `notifications.mark_read/watch/unwatch` as transport-callable |

### Phase 1 — design

**Deviation D-1 (new reader, table-owning module).** `leases.py` gains
`leases_held_by(conn, *, holder_id, limit, now)` — the holder-scoped read the
desk needs, living in the module that owns `issue_leases` (one table, one
owner). It derives `active`/`expired` from `expires_at` vs an injectable clock
rather than storing state, matching the lease doctrine, and carries the 0057
`generation` so a desk reader can fence a delayed command.

**Decision D-2 (cursor gets a command module, and no activity event).** The
cursor write qualifies as personal state on every criterion in
`COMMAND_MIGRATION.md:41`, and that is the citation for recording **no
activity event**: a read receipt is the reader's own state, not fleet history.
But the category is documented as an exception rather than a lane, and this
write has a genuine refusal to map (`conflict` on rewind), so it still gets a
command owner (`core/desk_commands.py`) with the transport-neutral kind
dialect. Strictly more conservative than the exception allows; needs no
`check_write_ownership` allowlist entry because the path is
transport → command → data module, the ordinary shape.

**Decision D-3 (advance-only, enforced twice).** The command refuses a smaller
`after_id` with `conflict` for a clean 409, and migration 0073 carries a
BEFORE UPDATE trigger refusing the same at the database. Rewinding a read
receipt would let an agent claim it never saw an event; the trigger is the
backstop against a writer that bypasses the command.

**Decision D-4 (`events_since_cursor` is capped and says so).** Counting is
bounded at 500 (`COUNT(*)` over a `LIMIT 501` subquery) and the projection
carries `events_since_cursor_capped: true` when it bites. An exact five-figure
backlog is noise; "500+, drain from here" is the actionable form. With no
cursor set the count is `null`, never `0` — "you have never read" and "nothing
new" are different facts.

**Decision D-5 (the desk is for every authenticated actor).** Humans get a
desk too; agents are the audience. Visibility is the actor's own throughout —
every lane composes an existing gated reader with `actor=` rather than a new
query, so the desk cannot widen what any surface already shows.

**Schema `athena.agent_desk.v1`** — `identity` · `asks` · `work` · `signals` ·
`cursor`, each list carrying its own `limit`/`total`.

**Bounds:** controls 20, delegations 20, leases 20, handoffs 20,
notifications 10, `events_since_cursor` cap 500, `after_id` bounded by
`MAX_SQLITE_INTEGER` (`core/ids.py`).

**Error kinds:** `invalid` (non-integer/non-positive `after_id`), `conflict`
(rewind). No `not_found` — a desk always exists for an authenticated actor.

### Phase 2..n — implementation and validation

**Deviation D-6 (module placement — the guide said `core/`, the tree said no).**
The guide specifies `core/desk.py` and `core/desk_commands.py`. That is
impossible: the desk composes `aegis` readers (delegations, leases, claim
handoffs) and `scripts/check_import_contracts.py` fails the build on
`core → aegis`. Caught by the gate, not by review. Resolved along the house's
own seam, with the precedent being every other cross-module projection
(`aegis/fleet_work.py`, `aegis/fleet_attention.py`, `aegis/work_context.py`,
all in aegis and importing core freely):

- `core/desk_cursors.py` — the `agent_cursors` table's only writer/reader.
  Core owns it because the position is about the **activity trail**, which is
  core's, and it imports no aegis.
- `aegis/desk.py` — the composition.
- `aegis/desk_commands.py`, `aegis/desk_api.py` — command owner and transport.

**Deviation D-7 (two activity readers registered with the guard script).**
`_events_since` and `_latest_event_id` are general readers: the desk's count
must equal what `GET /events` would hand the same reader, and that feed serves
imported rows (labeled). Excluding foreign history would make the count
disagree with the drain it exists to size. Both are recorded in
`scripts/check_imported_at_guards.py`'s exempt list with that reason.

**Three guide claims the tree corrected during implementation** (each found by
a failing test, each a case of the code being right):

1. `claim_handoffs.open_handoffs_for_issues` returns a **dict keyed by issue
   id**, not a list. The desk sorts it into a list.
2. Delegation inbox items **nest the issue** under `item["issue"]`, and the
   inbox pages by `has_more`/`next_offset` rather than reporting a total — the
   desk repeats that honesty instead of inventing a count.
3. Claiming an issue **requires `If-Match`** with the issue's strong ETag
   (`428 precondition_required` without it). The test now carries it; this is
   the optimistic-concurrency contract working as designed.

**One design fact discovered by test, worth stating:** a brand-new agent can
see *none* of the bootstrap/onboarding trail, so its
`latest_visible_event_id` is legitimately `null`. That is the honest answer —
there is nothing for it to acknowledge — and it reinforces why the unset
cursor is `null` rather than `0`.

### Validation (Stage F-1)

| Check | Result |
|---|---|
| `ruff check .` | passed |
| `ruff format --check .` | passed (405 files) |
| `mypy src/athena` | no issues, 165 source files |
| `scripts/check_import_contracts.py` | passed, 165 modules (after D-6) |
| `scripts/check_write_ownership.py` | passed |
| `scripts/check_imported_at_guards.py` | passed (after D-7) |
| `pytest tests/test_desk.py` | **18 passed** |
| Real-HTTP proof | `athena-serve` bootstrap → login → mint bearer → `GET /desk` (null cursor, null since-count) → advance to `latest_visible_event_id` → since-count 0 → idempotent re-ack 200 → rewind refused 409 → `athena-doctor` reports 73 migrations, latest `0073_agent_cursors.sql`, chain verified |

Limitations recorded: the desk has no web surface by design (agent-facing; the
operator's dashboard already exists); `asks.worker_kill_requests` scans the
caller's newest 50 workers rather than paging, which is a bound worth revisiting
only if a single agent ever registers more than 50 processes.

---

## Stage F-2 — Playbooks: BLOCKED on an architecture decision

Implemented in full and passing **17/17 tests** — including the end-to-end loop
proof (page → parent + children → backlinks on the page → a `rollup` embed
counting them) — then parked, because it hit a CI-enforced rule it cannot
legally satisfy.

### The blocker

`scripts/check_import_contracts.py` declares
`LAYERS = (("web",), ("aegis", "mentor"), ("core",))` with the comment
*"Containers sharing one entry are independent peers, not mutually importable
siblings."* A playbook command must **read Mentor** (page, label, body) and
**write Aegis through `issue_commands`** (so the writes keep their audit
events, budget metering, and authorization). That edge is forbidden in both
directions, and `web/` — the only layer that may import both — is barred by the
cardinal rule from owning logic or authorization.

This is friction between the feature and the module contract, which AGENTS.md
says to flag rather than absorb, and the guide's rule 10 says to stop on. The
complete implementation, its tests, and the surface patch are preserved in
[`docs/plans/parked/f2-playbooks/`](../parked/f2-playbooks/README.md) with three
options; the recommendation is a new `workflows/` composition layer, which also
gives F-4 (workspace search, another cross-module read) a legal home.

**Awaiting the owner's decision. Stages F-3 through F-7 are not started.**

### Verified during F-2 (kept for whoever resumes)

| Guide claim | Verdict |
|---|---|
| `playbook` label marks the page, no new table | CONFIRMED — `page_templates.py` is the exact precedent (`template` label, `labels.page_ids_for_label`) |
| `/pages` needs adding to `_IDEMPOTENCY_API_ROOTS` | **CORRECTED** — it is already opted in (`main.py:371`), so retry-safety needs **no domain table and no migration**; the guide's proposed `playbook_starts` table is unnecessary. Proven by a passing test: same `Idempotency-Key` replays to the same parent, four issues not eight |
| `issue_commands` uses the kind dialect | CONFIRMED — `IssueCommandError.kind` |
| `set_issue_parent(conn, *, actor, issue_id, parent_id)` | CONFIRMED |
| icarus mints `secrets.token_hex(16)` | CONFIRMED — but unused here, per the correction above |
| Space create accepts `visibility` | **CORRECTED** — it does not; tests set it with raw SQL (`test_access_mentor_reads.py:65`) |
| Backlinks rows carry `source_id` | **CORRECTED** — they resolve to `{kind, id, title}` |

---

## Stage F-2 — Playbooks: UNBLOCKED (option A) and shipped

The owner chose **option A**: a `workflows/` composition layer. Implemented,
and the parked artifacts are restored and deleted from the parking bay.

### The layer

`scripts/check_import_contracts.py` is now
`LAYERS = (("web",), ("workflows",), ("aegis", "mentor"), ("core",))`, with the
reason recorded beside it. Workflows may import both modules and core; nothing
below may import workflows. `AGENTS.md`'s ownership table gained the row.

**One correction found while wiring it:** the REST route could not stay in
`mentor/api.py` either — `mentor` may not import `workflows` (it sits below).
The transport for a workflow command belongs at the workflows layer, so
`workflows/playbook_api.py` owns the route. It keeps the
`/pages/{page_id}/start-playbook` path deliberately: the resource an operator
names is still the page, and the URL follows the noun rather than the package.

### Design (as built)

- Marker: the `playbook` label, exactly as `page_templates.py` uses `template`.
  No new table, no new concept owner.
- Parser: `- [ ]` / `* [ ]` / `+ [ ]`, indented forms included; `- [x]` counted
  and skipped; empty boxes skipped. The regex uses disjoint adjacent terms so a
  long indent run cannot backtrack polynomially (`core/links.py` discipline),
  pinned by a timing test.
- Writes go through `issue_commands.create_issue` / `set_issue_parent`, in one
  transaction, so a refusal partway leaves nothing behind (test asserts it).
- Idempotency: **no domain table** — `/pages` is already an idempotency root,
  proven by a test (same key → same parent, four issues not eight).
- Error kinds: `not_found` (missing or unseen page — same answer),
  `invalid` (unlabeled, archived, no unchecked steps, blank override title),
  `capacity` → 429 (> 50 steps), `unauthorized` → 401.

### Validation (Stage F-2)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | passed |
| `mypy src/athena` | no issues, 168 source files |
| `check_import_contracts.py` | passed, 168 modules (with the new layer) |
| `check_write_ownership.py` / `check_imported_at_guards.py` | passed |
| `pytest tests/test_playbooks.py` | **17 passed** |
| `pytest tests/test_desk.py tests/test_mcp_client.py` | 265 passed (F-1 intact; `start_playbook` added to both MCP mutation-contract registries) |
| Real-HTTP proof | `athena-serve`: desk → cursor advance → rewind 409 → create space/page → label → start-playbook 201 (2 children, 1 skipped) → backlinks on the page show all three issues → re-run makes a NEW instantiation → unlabeled page refused 422 → `athena-doctor` verified 18 chained events |

Stages F-3 through F-7 remain. F-4 (workspace search) now has the layer it
needs, which was the second reason to prefer option A.

---

## Stage F-3 — Space subscriptions: shared memory that says when it moved

Branch note: PR #327 (F-1 + F-2) merged to `main`. Per the merged-PR rule this
branch was restarted from the new `main` (`37e210c`) rather than stacked on
already-merged history, keeping the same branch name.

### Phase 0 — claims verified

| Guide claim | Verdict |
|---|---|
| `watches` (0023) has no `target_kind` CHECK | CONFIRMED — polymorphic, PK `(user_id, target_kind, target_id)`, index on the target end. **No migration needed** |
| The vocabulary is code-level | CONFIRMED — `notifications.WATCHABLE_KINDS`, enforced once at `notifications_api._validate_kind` |
| `notify_watchers` is the single fan-out site | CONFIRMED — one call, `core/activity.py:336`, inside the event's transaction |
| Watch writes are personal state (no audit event) | CONFIRMED — `watch`/`unwatch` record nothing; the REST routes use `personal_write_actor` |
| REST is `PUT/DELETE /watches/{kind}/{id}` | **CORRECTED** — it is `POST /watches` with a JSON body, plus `DELETE /watches/{kind}/{id}` |
| An MCP watch tool exists to extend | **CORRECTED** — there is **no** MCP watch tool at all. F-3 adds both `watch` and `unwatch`, or agents could not subscribe |
| Pages can move between spaces (so cross-space transfer needs thought) | **CORRECTED** — `pages.space_id` is never updated anywhere; `page_moved` is re-parenting *within* a space. Nothing to reason about |
| Space events need inventing | **CORRECTED (in our favour)** — `space_created/edited/deleted/member_added/member_removed` already record with `target_kind='space'`, so the existing direct pass covers a space's own lifecycle for free |
| Page watches handle "watcher cannot see it" somehow — match it | CONFIRMED and matched — notifications are written **ungated**; `list_notifications`/`unread_count` gate at read via `access.event_visibility_clause` |

**Gap found in Phase 0, not in the guide:** `delete_page` called `purge_page`
*before* `record_page_deleted`. The purge drops the page row **and** its watches,
so at the moment the event existed both routes to a watcher were gone —
deleting a page notified **nobody**, and a space lookup through the page id
would have returned nothing. Silence about the loudest change in a shared space
is not a defensible reading of "the watch dies with the page".

### Design (as built)

- `WATCHABLE_KINDS = ("issue", "page", "space")`. One vocabulary; REST, MCP, and
  web all read it. The MCP `Literal` must be static, so a test asserts the two
  are the same set — the drift guard is CI, not a comment.
- Two passes in `notify_watchers`, one rule: an event reaches you if its target
  **is** the watched thing, or **is a page inside** a watched space. The indirect
  pass lives in the single fan-out owner, never a second call site. Cost: one
  indexed lookup per page event.
- Reading `pages` from `core` is the same read-only borrow `core.access` already
  makes of `spaces`/`pages`; mentor stays the only writer of those rows.
  Resolving the space here is what keeps `activity.record`'s signature honest —
  it knows about targets, not about which module owns a container.
- `UNIQUE (user_id, event_id)` collapses the passes: watching a page **and** its
  space is one notification, not two. Actor exclusion holds on both paths.
- **Ordering fix:** the `page_deleted` event is now recorded before the purge,
  inside the same transaction. Invisible from outside; both the page's watchers
  and its space's watchers get the final delivery, then the page watches are
  purged with the page.
- Refused, on purpose: digest, rollup, per-watcher delivery. `unwatch` is the
  volume control, and the doc says so instead of the code pretending it is quiet.

### Deviations

- **D-8** — MCP `watch`/`unwatch` are new tools, not an extension of an existing
  one (the guide assumed one existed). They carry no `idempotency_key`: the REST
  routes do not consume one, and a watch is idempotent by construction. Precedent:
  `mark_notifications_read`, the other personal-state mutation tool.
- **D-9** — the delete-ordering fix is outside the literal stage scope. Flagged
  rather than absorbed silently: without it, deleting a page notified *nobody*,
  not even an admin. Two tests pin the fix.

  **The real-HTTP proof then refuted the stronger claim I had written**, and
  that correction is the most useful thing in this stage. Over the wire, the
  subscriber's inbox went *empty* after the delete — because
  `access.event_visibility_clause` proves a page event's visibility with
  `EXISTS (SELECT 1 FROM pages …)`, and once the row is purged there is nothing
  left to prove it with, so the gate fails **closed**. Verified directly: the
  ungated row exists, an admin's read renders `page_deleted`, a member's does
  not — and every *earlier* notification about that page stops rendering too.

  So the shipped claim is the narrow one: the event is now **delivered**, and it
  **renders for an admin**. Making it legible to non-admins needs an event-time
  visibility envelope for page targets — the thing issue events carry via
  `activity_visibility_projects` and pages have never had. That is an
  access-model change; flagged here, not smuggled in behind a subscription
  feature. A third test pins the limit so nobody re-reads the docs as a promise.

  This is the second time this sprint that the real-HTTP gate caught something
  no unit test did — the unit tests used the ungated internal read, which is
  exactly the read a real client never makes.
- **D-10** — an existing test (`test_watch_validation_and_unwatch`) asserted
  `space` was **not** watchable. Updated to assert which side of the closed
  vocabulary `space` is now on, and to keep a genuine refusal case (`project`).

### Validation (Stage F-3)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | passed |
| `mypy src/athena` | no issues, 168 source files |
| `check_import_contracts.py` / `check_write_ownership.py` / `check_imported_at_guards.py` | passed |
| `pytest tests/test_space_subscriptions.py` | **17 passed** |
| `pytest` on the impacted suites (notifications, page delete/move, mentor web, MCP client, access) | 313 passed |
| Full coverage-gated suite | **3,275 passed**, line 93.07 / branch 83.57 / combined 90.91 — all three floors cleared, excluded lines still exactly 2 |
| Real-HTTP proof | two identities, two bearer tokens, one `athena-serve`: watch space 204 → admin creates a page → subscriber unread 1 → edit + comment → inbox `{page_created, page_edited, page_commented}` → page in ANOTHER space changes nothing → subscriber's OWN write changes nothing → delete in the other space changes nothing → delete the watched page: member's inbox goes empty (gate fails closed), a watching admin renders `page_deleted` → unwatch holds the count while a new page is created → `GET /desk` reports the same unread count → garbage kind 422 → `athena-doctor` verified 16 chained events |

**A process note worth keeping:** the first full-suite run reported 175 failures
and was a false alarm — `scripts/coverage.sh` defaults to `.venv/bin/python`,
which does not have the `mcp` extra installed, so every MCP test failed on
`ModuleNotFoundError`. The real gate is
`ATHENA_PYTHON=.venv312/bin/python scripts/coverage.sh`. Recorded here because
"the suite is red" and "the suite cannot import an optional extra" look
identical in a summary line, and only one of them is a defect.

---

## Stage F-4 — Workspace search: one ask, everything you may see

### Phase 0 — claims verified

| Guide claim | Verdict |
|---|---|
| `search.search` spans kinds with visibility gating | CONFIRMED — and it already covers **four** kinds (`issue`, `page`, `issue_comment`, `page_comment`), gated in SQL before LIMIT/OFFSET so paging stays correct |
| A query parser exists whose own classification can decide "grammar-shaped" | CONFIRMED — `core/work_query.parse` returns `Query(terms, text, sort, raw)`. `bool(terms)` is the classification; **no second parser needed**, exactly as the guide insisted |
| `issue_query` is the issue-side entry point | CONFIRMED — `run_query(conn, query, *, actor, visible_project_ids, limit, offset)`, with `access.visible_project_filter` supplying the gate |
| Unknown atoms already error rather than return empty | CONFIRMED — `work_query.QueryError` carries `.atom`, and `aegis/api.py::_query_refusal` is the 422 shape of record (`{error, code: "invalid_query", atom}`) |
| `GET /search` requires an authenticated actor | CONFIRMED — `current_actor`; the new route matches that bar |
| Put it in `core/workspace_search.py` | **CORRECTED** — impossible. It reads `aegis.issue_query` *and* `core.search`, and `core` may not import `aegis`. It belongs at **`workflows/`**, the layer F-2 added. This stage is the second inhabitant, and the first one that could not have existed before that layer |
| `PATCH /projects` accepts `visibility` | **CORRECTED** (found by a failing test) — visibility is its own endpoint, `PUT /{container}/{id}/visibility`. A PATCH carrying it answers "no fields to update" |

### Design (as built)

- **Routing.** Atoms → the issue compiler; bare words → full-text search across
  all three groups. `is:open zebra` filters the work *and* finds the page that
  says zebra. Pages and comments are searched with `parsed.text` only —
  feeding `is:open` to FTS would match the literal token and produce confident
  nonsense.
- **One item shape per group, whichever engine found it.** An issue hit is
  `{id, key, title, status, snippet}` from either path; `snippet` is `null` on
  the grammar path, because a structural match has no text excerpt and an empty
  string would read like "matched nothing".
- **Grouped, not ranked.** `grouped_by_kind: true` is in the payload. Two
  engines with two orders cannot be interleaved into one relevance score
  without inventing it.
- **Bounds disclose themselves.** Every group fetches `limit+1`, so `clipped`
  is a measured fact. `limit_per_kind` is 1..25, enforced at the route *and*
  inside the command so a non-HTTP caller cannot slip past.
- **`query.text` is echoed.** A pure-grammar query leaves the doc groups empty;
  saying what was text-searched is what stops that reading as "no matches".
- Error kinds: `invalid` only (empty query, out-of-range limit, or a query the
  grammar refuses). The route re-emits the grammar's own 422 body so a bad
  query reads identically whether asked of `/issues` or of here.

### Deviations

- **D-11** — module placement corrected from `core/` to `workflows/` (see the
  claims table). The guide could not have known: it was written before the
  layer existed.
- **D-12** — the guide left "pages still searched with the raw text — decide and
  pin" open. Decided: pages and comments see `parsed.text`, never the raw query.
  Pinned by `test_grammar_routes_issues_and_still_text_searches_the_docs` and by
  `test_pure_grammar_leaves_the_doc_groups_empty_and_says_why`.
- **D-13** — `QUERY.md` listed "cross-kind queries" under *Deliberately not in
  v1*. That bullet is now rewritten rather than deleted: this stage composes,
  it does not extend the language, so `label:infra` still will not find a
  labelled page. Removing the bullet would have quietly overclaimed.

### Validation (Stage F-4)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | passed |
| `mypy src/athena` | no issues, **170** source files |
| `check_import_contracts.py` | passed, 170 modules — the new module sits at `workflows/`, which is what makes it legal |
| `check_write_ownership.py` / `check_imported_at_guards.py` | passed |
| `pytest tests/test_workspace_search.py` | **11 passed** |
| `pytest` on the neighbours (search, MCP client, playbooks, space subscriptions) | 313 passed |
| Full coverage-gated suite | **3,286 passed**, line 93.08 / branch 83.60 / combined 90.93 — all floors cleared, excluded lines still exactly 2 |
| Real-HTTP proof | `athena-serve`, two identities: plain `zebra` reaches all three groups (2 comments, each naming its parent) → `is:open zebra` routes issues to the grammar (`atoms: ['is:open']`, `text: 'zebra'`) while the page is still found → closing the issue empties `is:open zebra` and `is:closed zebra` finds it → pure grammar leaves the doc groups empty with `text: ''` → `labl:infra` 422 naming the atom → empty q / limit 0 / limit 26 all 422 → `limit_per_kind=2` returns 2 with `clipped: true` → both containers set private: the second identity gets `[]` in all three groups on both the text and grammar paths while the owner still sees 5 pages → anonymous 401 → `athena-doctor` verified 17 chained events |

### CodeQL, mid-stage

While F-4 was in flight, CodeQL reported three new alerts against the F-3 web
routes — one high (reflected XSS in the 404 body) and two medium (untrusted URL
redirection in the two redirects). Neither was reachable: FastAPI coerces
`space_id: int` before the handler runs, so a non-integer path segment 422s. But
both were **deviations from patterns already in that file**, which is why they
alerted at all:

- `_page_visible_or_response` builds its 404 from a **fixed string**; I had
  interpolated the requested id. Now fixed — and better for it, since naming the
  id confirms which id was asked about, the one thing a not-found should stay
  quiet about.
- The space *create* route at `web/mentor.py:282` redirects to `space['id']`
  — the id off the **database row** — which is why that line has never alerted.
  My routes redirected to the path parameter. They now use the row, and stop
  discarding a read they had already paid for.

Fixed and pushed within the same session (`81cbac4`); CodeQL green on the
following run. Recorded because the lesson generalizes: when a scanner flags new
code that sits beside older code doing "the same thing", check whether the older
code is actually doing the same thing. Twice here, it was not.

---

## Stage F-5 — The Field Guide: the workspace documents itself

Branch note: PR #328 (F-3 + F-4) merged. Branch restarted from the new `main`
(`110b97c`), same name, per the merged-PR rule.

### Phase 0 — claims verified

| Guide claim | Verdict |
|---|---|
| `demo.py` seeds through the real commands as a real user | CONFIRMED — that is the pattern reused |
| Package data is declared in `pyproject` and pinned by a wheel gate | CONFIRMED — `[tool.setuptools.package-data]` plus `scripts/verify_wheel.py`'s `RUNTIME_DIRS`, compared against the source tree and asserted in `tests/test_verify_wheel.py`. `field_guide` is now one of them |
| `core/migrations` is the shape to copy for loaded-not-inlined content | CONFIRMED — a data-only directory with **no** `__init__.py`, read via `Path(__file__).parent`. Copied exactly (an `__init__.py` there would put `__pycache__` in the wheel-manifest comparison) |
| Space key uniqueness gives idempotency | CONFIRMED — `spaces.get_space_by_key`; a second seed refuses |
| Extend `athena-demo --field-guide`; never a second seeder | CONFIRMED **and extended** — see D-14 |
| Pages can cross-link by title | CONFIRMED — `[[Title]]` resolves when exactly one live page answers, preferring the source's own space. **But see D-15**: it records nothing when it does not resolve |

### Deviations

- **D-14 — two entry points, one seeder.** `athena-demo` refuses an existing
  database; that is its entire contract. But "every fresh install gets a
  non-empty, useful first screen" means seeding into an instance the operator
  *keeps*, which by definition exists. Putting a writes-into-your-real-database
  mode inside the tool that promises never to touch one is a footgun. So:
  `athena.guide` holds the one implementation, `athena-demo --field-guide` seeds
  it into the disposable workspace, and **`athena-field-guide <db>`** (in
  `ops.py`, beside the other ten operator commands, every one of which takes an
  existing database) seeds it into the one you keep. One content
  implementation, two entry points — the rule being protected is about the
  content, not about which CLI the operator is holding.
- **D-15 — a re-index pass after seeding, which the guide did not anticipate.**
  A `[[Title]]` wikilink resolves only against a page that already exists, and
  records **nothing** when it does not — unlike a numeric `[[page:N]]` ref,
  which is stored broken and lights up later. The guide's pages reference each
  other in both directions, so on the first pass roughly half the references
  pointed at pages the loop had not created yet. Caught by a failing test, not
  by review. `seed_field_guide` now re-runs `links.sync_links` over the created
  pages once they all exist: the same derivation an ordinary edit triggers, on
  unchanged bodies, writing no page and recording no event.
- **D-16 — attribution is printed, never silent.** The pages carry somebody's
  name on the trail. `--as EMAIL` names the author; otherwise the earliest
  administrator is used, and either way the command prints who it attributed
  them to. With no administrator at all it refuses rather than inventing one.
- **D-17 — a naming collision found while wiring the demo.** `seed_demo`
  already binds a local named `guide` (a demo page), so importing the module as
  `guide` would have been shadowed inside the one function that needed it. It is
  imported as `field_guide_content` there.

### Design (as built)

Nine pages, authored as markdown package data and listed in an explicit manifest
(order and titles are decisions, so they live in code rather than in a heading a
content edit could move): the desk · claiming and yielding · recording learnings
· answering a run control · playbooks · searching the workspace · watching
shared memory · what the trail proves · a real example playbook carrying the
`playbook` label. An existing `playbook` label is reused rather than duplicated —
a second label with the same name would split every playbook query in half.

### Validation (Stage F-5)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` / `mypy src/athena` (171 files) | passed |
| all three contract scripts | passed |
| `pytest tests/test_field_guide.py` | **10 passed** |
| `pytest tests/test_verify_wheel.py tests/test_wheel_evidence.py tests/test_demo*.py` | 83 passed |
| Full coverage-gated suite | **3,296 passed**, line 92.95 / branch 83.48 / combined 90.80 — all floors cleared, excluded lines still exactly 2 |
| Real-HTTP proof | CLI seeds 9 pages into a real database and prints its attribution → a second run refuses (`already exists`, rc 1) → `athena-serve` reads the space back: 9 pages, `my_desk()` in the body → `/search` and `/search/workspace` both find guide pages → outgoing links from *Your desk* resolve to *Searching the workspace* and *Watching shared memory*, and the backlinks answer in reverse → `export.html` 200 with the space name → the example playbook instantiates over the wire: 4 children, 1 skipped, all five issues visible as backlinks on the page → the rendered page carries a real anchor to a sibling page → `athena-doctor` verified 22 chained events |

**D-18 — the full suite caught a real defect in F-5, and it was mine.** Adding
`from athena import guide` at `ops.py` module scope broke two config-validation
tests: importing this module must never reach `athena.config`, because config
validates at **import** time and offline recovery (`athena-backup`,
`athena-restore`, `athena-doctor`) has to keep working on an instance whose
service configuration is broken — which is exactly when an operator needs it.
`guide` reaches mentor, which reaches config, so a bad `ATHENA_NETWORK_MODE`
became a traceback from every command in the file, and `athena-serve` stopped
reporting the misconfiguration cleanly. Fixed by importing inside
`field_guide_main`: seeding a guide is not a recovery command and may
legitimately require a loadable app. Third time this sprint the full suite
found something the targeted suites could not — the pattern is that a change's
blast radius is rarely where its diff is.

---

## Stage F-6 — The debt tail (two of three items)

### Item 3 — the `capacity` convention, recorded (commit `1723fe0`)

Phase 0 found the ambiguity was real and live: `capacity` maps to **429** in
`workflows/playbook_commands` and **409** in `core/agent_runs_api`. Both are
shipped. `COMMAND_MIGRATION.md` now states the convention (429: the request was
well-formed and hit a bound; it may succeed later or smaller — which is what 429
says, where 409 would claim a conflict with existing state a bound is not) and
names the exception with its reason: the agent-run check-in's capacity really
*is* a conflict with state the caller already owns — too many distinct run ids,
where the fix is to reuse a run rather than wait — and it is a shipped wire
contract. Changing either is now explicitly its own decision.

### Item 2 — six command modules onto the kind dialect (commit `864a2d0`)

The guide named five. `page_comment_commands` carried the identical legacy shape
beside one of them, so it went too rather than leaving the same refactor to be
done twice. All twelve raise sites collapsed to two kinds (`not_found` ×11,
`invalid` ×1), which is what made this mechanical rather than a redesign.

Maps live **per route module**, not shared. A single global table would have
relocated the coupling the refactor exists to remove; a transport that needs a
different status for a kind must be able to say so locally.

The contract — "the HTTP surface does not change" — is pinned twice:

- **structurally**: a migrated error exposes `kind` and no longer has a
  `status_code` attribute at all, so a regression fails a test, not a review;
- **at the wire**: twelve requests over the real app across all six surfaces
  (vanished comment edit/delete, sprint start/complete/delete, space
  edit/delete, page-comment edit, webhook pause, unknown token scope) asserting
  the exact statuses they answered before.

Four audit tests asserted `exc.status_code`; they now assert `exc.kind`, which
is the fact worth asserting at that layer — the status they cared about is
pinned at the wire instead.

### Item 1 — If-Match on browser edit forms: NOT DONE, and why

Deliberately left. It is not mechanical: it is new conflict-resolution UI whose
hard parts are product decisions — what the losing editor sees, whether their
text survives, and how the notice avoids implying Athena resolved anything. The
drafts `based_on` machinery is the right precedent and is already in place, so
the work is well-positioned; it is not started. Attempting it in the tail of a
long session would have produced exactly the kind of half-considered surface
this repo's contract exists to prevent. Recorded here rather than left to look
finished.

### Validation (Stage F-6)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` / `mypy src/athena` (171 files) | passed |
| all three contract scripts | passed |
| `pytest tests/test_command_error_dialect.py` | **12 passed** (6 structural, 5 map-parity, 1 wire-contract sweep) |
| the four migrated audit suites | 29 passed |
| Full coverage-gated suite | **3,308 passed**, line 92.97 / branch 83.48 / combined 90.82 — all floors cleared, excluded lines still exactly 2 |

---

## Stage F-6 item 1 — If-Match on browser edit forms (the item I had deferred)

Deferred once for session length, then built when the owner asked why it could
not just be done. It could; the deferral was about capacity, not blockers.

**Design decision taken (option A of three offered):** both texts visible,
theirs winning the form fields, yours preserved as your draft. Rejected: a bare
`412` (loses work — a browser buffer is not a store) and auto-merge (Athena does
not claim to have resolved what a person must read to resolve).

### Phase 0 — what already existed

Almost all of it. `page_commands.edit_page` already accepted `if_match` and
compared it **inside the write lock**; `page_etags.current_etag` already existed;
the drafts table already stored an owner-scoped baseline. The browser form simply
never sent a precondition — and `web/mentor.py` said so in a comment: *"the
browser form carries no If-Match, so this stays last-write-wins."* The work was
wiring plus the refusal path, not new machinery.

### The one real design trap

`based_on` (drafts) and `if_match` (the save) look like the same value and must
not be the same field. `based_on` follows the editing **session** — a restored
draft keeps its own baseline, which is what keeps stale work marked stale. The
precondition must be the page **as this form was rendered**. Sharing one field
would mean a restored draft could never be saved at all. Two hidden fields, with
the reason written between them, and a test (`test_restoring_a_draft_can_still_be_saved`)
that fails if anyone merges them.

Second subtlety, same family: the conflict path records the loser's draft against
the tag they **submitted**, never the page's new one. Stamping the new tag would
mark stale work fresh and silence the warning at the one moment it exists for.
Asserted at the row, not through the rendering.

### Deliberate softenings

The hidden field is a concurrency aid, not an authorization check, so: an absent
tag (a tab open across the upgrade) keeps the old behavior rather than refusing
an author over a field they cannot see, and a malformed tag is treated as no
precondition rather than becoming a wall between an author and their own page.

### Validation

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` / `mypy src/athena` (171 files) | passed |
| all three contract scripts | passed |
| `pytest tests/test_page_edit_conflict.py` | **10 passed** |
| `pytest` on the edit path (drafts, etags, versions, mentor web, lifecycle) | 68 passed |
| Full coverage-gated suite | **3,318 passed**, line 92.97 / branch 83.46 / combined 90.82 — all floors cleared, excluded lines still exactly 2 |
| Real-HTTP proof | two real browser sessions against `athena-serve`: both editors open on the same tag → Ann saves (303) → Bob saves (**409**) → the refusal shows both texts and says nothing was overwritten or merged → the page is still Ann's → Bob navigates away entirely and still has unsaved work, still marked stale → Bob restores and deliberately saves over Ann (303) → Ann's editor never shows Bob's unsaved text → `athena-doctor` verified 7 chained events |

**Two test-harness bugs worth recording**, both cases of a test passing while
proving nothing: extracting the ETag from the form without unescaping entities
posted a mangled tag, which quietly took the malformed-precondition path and
"succeeded"; and asserting notice wording against raw HTML failed on an
apostrophe and a line wrap. Both now normalize the way a browser does.

---

## Stage F-7 — Adversarial review, composed proof, ship

### The composed ecosystem proof (the sprint's actual claim)

One script, one agent's working session, each stage's output feeding the next.
If the stages were really separate features, it could not have been written:

```
 1. operator seeds the field guide OFFLINE, before the server is running
 2. agent orients with one call: cursor=None, since=None, unread=0
 3. agent acknowledges where it starts: cursor -> #2
 4. agent finds the guide as an ordinary space: 9 pages in GUIDE
 5. agent watches the space as shared memory
 6. operator edits the handbook -> agent's inbox: ['page_edited']
 7. agent starts the guide's OWN playbook: parent #1, 4 children, 1 skipped
 8. the page now shows the work it started: 5 issues link back to it
 9. workspace search 'is:open test suite' -> finds those very children
10. the desk reports the session's own work: 20 events since the cursor
11. agent drains the cursor (rewind refused 409) -> since=0
12. two browsers on the handbook: second save refused 409, nothing lost
13. activity chain: ok (26 entries verified)
```

Step 9 is the one worth staring at: the search that finds the work is asked in
the *work query grammar*, against issues that a *document* created, discovered
through a space the agent *subscribed* to, and the desk then reports those very
events as what changed. Five stages, one sentence.

**Two of my own assertions were wrong before the code was**, both cases of the
contract being more careful than I was: `desk["cursor"]` is `null` when unset —
not a dict with a null field — and `events_since_cursor` is `null` until a
cursor exists, because "never looked" is not "nothing new". The proof had to
adopt the loop's real shape (acknowledge where you start, work, then ask what
changed) before it could assert anything.

### Findings

**CONFIRMED and FIXED — a partial seed wedged the operator.** Seeding the field
guide is nine committed writes, and re-running refuses if the space exists. Each
rule is right; together they were not. A failure on page five (missing package
data) left a four-page space that the retry then refused, so recovery required
deleting a space by hand. Verified by injecting the failure, observing the
four-page space and the refused retry. Fixed by reading all content **before**
the first write, so the one failure this code can actually cause creates nothing.
Regression test: `test_missing_content_wedges_nothing`. The comment states the
honest bound — this is not a transaction, and a database failure partway through
still leaves a partial guide.

**REFUTED, each verified against the tree rather than argued:**

| Suspicion | Verdict |
|---|---|
| A losing editor is **charged budget** for a save that never happened (`edit_page` charges before checking the precondition) | Refuted — the charge rolls back with the refusal. Measured: `action_used` 1 before, 1 after a `precondition_failed` |
| A **private-space watcher** sees page titles in their inbox | Refuted — notifications are written ungated and the inbox gates at read; a member watching a private space reads `[]` |
| **Workspace search** leaks a private space's pages or comments to an outsider | Refuted — all three groups empty for a non-member, on both the text and grammar paths |
| The **atom echo** in the search response discloses what exists | Refuted — it echoes only what the caller typed |
| `limit_per_kind` is only bounded at the route | Refuted — the command refuses 0 and 26 itself |
| A watch on a **nonexistent space** breaks the fan-out | Refuted — delivers nothing, raises nothing |
| **Drafts dangle** after a page is deleted | Refuted — `page_drafts` is `ON DELETE CASCADE` (0071) |

### Residual risks, named rather than resolved

1. **The issue edit form still has no If-Match.** The plan said "pages first,
   issues second"; only pages shipped. Two people editing one *issue* body in
   browsers can still overwrite each other. The pattern is now established and
   the second application is mechanical — but it is not done, and this is the
   honest place to say so.
2. **A deleted page's notification renders only for an admin** (F-3). The
   access model proves a page event's visibility by looking the page up, and a
   purged row cannot prove it. Fixing it needs an event-time visibility envelope
   for page targets, which is an access-model change.
3. **Seeding the guide is not transactional.** A database failure mid-seed still
   leaves a partial guide requiring manual cleanup.
4. **The review's scope is bounded.** This was a focused adversarial pass over
   the surfaces this sprint added — visibility, bounds, transactionality, and the
   new concurrency path — not an exhaustive six-lens sweep of the full diff. Nine
   probes were run and recorded; one found a real defect. Claiming more coverage
   than that would be the exact failure this repository's doctrine exists to
   prevent.

### Final gate

| Check | Result |
|---|---|
| `scripts/smoke_app.py` | passed — installed launcher bootstrapped, stopped, restarted without bootstrap or actor-header trust, served packaged assets, authenticated by browser session, stopped cleanly |
| Composed real-HTTP ecosystem proof | **passed**, 13 steps, `athena-doctor` verified 26 chained events |
| Full coverage-gated suite | **3,319 passed**, line 92.97 / branch 83.46 / combined 90.82 — all floors cleared, excluded lines still exactly 2 |

### The sprint, closed

F-1 through F-7 are shipped. Four PRs, each gated and each merged: #327 (F-1,
F-2 + the `workflows` layer), #328 (F-3, F-4), #329 (F-5, F-6), and this one
(F-7). Two items are explicitly **not** closed and are recorded above as
residual risks, not as omissions: If-Match on the *issue* edit form, and the
event-time visibility envelope that would let a non-admin read a deleted page's
notification.

**The lesson this sprint kept teaching, stated once:** every defect that
mattered was found by *running* something, never by reading it. The real-HTTP
proof refuted a claim already written into the docs; the full suite caught a
one-line import that broke offline recovery; CodeQL caught two places where new
code differed subtly from the code beside it; a failing test caught wikilinks
that silently pointed nowhere; and the F-7 probes found a wedge that two
individually-correct rules created together. In every case the targeted tests
were thorough and told me nothing — because a change's blast radius is rarely
where its diff is.
