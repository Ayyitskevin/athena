# Changelog

Notable changes to Athena are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and package version
markers follow semantic versioning while the project remains pre-1.0. Version-like
headings are milestones in a version line; a heading becomes a *published release*
only once a matching git tag exists. See
[`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md) for the evidence behind
the newest one and for what tagging still requires.

## [Unreleased]

### Security

- **`cryptography` pinned up to 50.0.0.** PYSEC-2026-3552 was published against
  the pinned 49.0.0 after it was frozen, which turned CI's
  audit-the-pinned-inputs step red — exactly what that gate is for. The only
  consumer in the graph is `PyJWT[crypto]` (OIDC RS256/ES256 verification,
  `>=3.4.0`), and the OIDC/JWT suite passes against 50.0.0 unchanged.

- **The last three trusting command families now own their authorization.**
  Mentor's page commands (eleven entry points), its page-comment commands, and
  the event-source commands took a bare actor id and relied on checks the routes
  ran beforehand — so a caller reaching a command directly (undo, a workflow, a
  script) was trusted, and a transport-side check and the write it guarded could
  straddle a transaction boundary. Each now takes a resolved actor and checks
  inside its own write transaction: page/space visibility for every page write
  (hidden reads as missing, never 403, so a write path confirms nothing);
  author-ownership for page comments, with the delete-only admin moderation
  override (rewriting words is not moderation); and the admin role + admin token
  scope for event sources, exactly as run controls already did. Page delete also
  owns the no-cascade children rule and takes the audit title from the row under
  its own lock instead of trusting the caller's copy, and a refused
  attach-label-by-name now rolls back its find-or-create so a hidden page cannot
  grow the label vocabulary. Wire statuses are unchanged and pinned; the checks
  just moved to where a direct caller cannot skip them. `page_undo`'s
  compensators — which re-applied the boundary's checks precisely because the
  commands did not — now delegate visibility to the commands and keep only the
  role/scope half. This erases the "authorization still in some transports" line
  from the release risk list.

- **A read-scoped token could reach two writes.** The rule is that a token
  minted with only the `read` scope must never mutate anything, and two routes
  had drifted from it: `POST /playbooks/{page_id}/start` ran on plain
  authentication, letting a read-only token create a parent issue and its
  children, and `POST /desk/cursor` did the same for the reader's own desk
  cursor — personal state, but still a durable write. The playbook route now
  requires `issue:write` (it creates issues; it needs what issue creation
  needs) and the cursor route requires any write scope, matching every other
  personal-state write. Regression tests pin both refusals and both grants.
  Found by auditing every route dependency while building the scope map below —
  the audit the map now makes permanent.

- **Supported deployments now start through a fail-closed launcher.**
  `athena-serve` preflights absolute storage paths, SQLite and attachment
  integrity, an active administrator's durable recovery credential, direct
  numeric loopback or explicit Tailscale binds, exact `Host` authorities, and
  positive tailnet-facing limits before Athena/Uvicorn accepts traffic. It
  refuses wildcard, LAN, public, link-local, and hostname binds; legacy actor-header trust;
  bootstrap credentials during normal startup; HTTPS-only cookies on its
  direct-HTTP server; proxy-header trust; reload; and additional workers.
  Bootstrap is a separate loopback-only mode whose first administrator must set
  a nonblank, bounded password. The launcher also rejects a body cap too small
  to carry the supported bootstrap/login envelope and validates observable OIDC
  recovery URL/callback coherence. Valid legacy password hashes retain the prior
  1 MiB request envelope until a verified login records whether the credential
  is bounded; incompatible bootstrap migrations are rehearsed and rejected on
  an in-memory copy before the real file is written.

- **The application enforces the declared deployment boundary before doing
  request work.** An outer ASGI guard requires an allowed accepted-socket address
  and listener port plus exactly one allowlisted `Host` authority for every HTTP
  or WebSocket request, ignoring forwarded host/address claims and failing before
  body, session, limiter, route, or database work. The installed-wheel smoke now
  proves bootstrap, stop, credential removal, normal restart, browser login,
  packaged assets, and bounded shutdown over the same parent-held listener.
  The body cap also sits outside browser-session resolution, so an oversized
  request with an attacker-controlled cookie cannot force a SQLite lookup.
  Public and proxy-terminated exposure remain explicitly unsupported because
  Athena cannot infer external publication.

### Fixed

- **First-admin bootstrap now requires an explicit one-time credential.** An empty
  `ATHENA_BOOTSTRAP_TOKEN` disables HTTP bootstrap instead of granting
  administrator authority to the first network caller. A configured 32–255
  character visible-ASCII token is accepted only for the first user through
  `X-Athena-Bootstrap-Token`; missing, malformed, wrong, unconfigured, and
  post-bootstrap attempts collapse to the normal anonymous `401`. Duplicate
  credential headers fail closed, and unsupported bootstrap idempotency is
  rejected without inspecting database state. The token never becomes a durable
  administrator credential, and the existing immediate transaction still decides
  concurrent valid attempts.

- **First-admin bootstrap is atomic.** Concurrent credentialed `POST /users`
  requests can no longer both turn themselves into administrators after observing
  the same empty database. Bootstrap eligibility, forced-admin role selection,
  insertion, and its self-attributed audit event now share one immediate
  transaction; exactly one request succeeds and every loser is rejected without a
  user row or activity event. User command refusals now also use the project's
  transport-neutral error-kind dialect, with REST and browser adapters retaining
  their own status semantics. Fresh-account OIDC provisioning is refused until
  local administrator bootstrap completes, preventing a default-role SSO member
  from consuming the only bootstrap opening.

### Added

- **The MCP server registers only the tools its token can use.** A session used
  to carry all ~123 tools regardless of scopes — a read-scoped agent hauled
  roughly 10k tokens of mutation docstrings whose only possible answer was 403.
  At startup `athena-mcp` asks `whoami` for the token's scopes and registers
  the matching surface: a `read` token gets the 57 reads, an `issue:write`
  token adds Aegis writes and personal state, `docs:write` adds Mentor's, and
  `admin` sees everything — including the admin-gated reads that are otherwise
  hidden, because a tool that can only answer 403 is not a capability, it is
  noise.

  This is **presentation, not authorization** — the REST layer remains the
  boundary, and a wrong map entry can hide a tool but never permit a call the
  server would refuse. That is why the probe **fails open**: if Athena is
  unreachable at MCP startup the full surface is presented, and the tools
  answer 403 later exactly as before. `ATHENA_MCP_ALL_TOOLS=1` skips the probe
  for the same full surface on purpose. The map itself is fail-closed at build
  time — registering a tool with no declared scope raises, so every
  server-building test doubles as a completeness check.

- **`athena-field-guide --check` reports whether a seeded guide has drifted.**
  The guide ships as package data and is seeded once, and re-seeding refuses so
  an operator's edits are never overwritten — which meant an upgraded install
  silently kept a guide describing an older product, with no signal and no path.

  The check keeps **two facts apart**: whether *Athena's* guide changed since you
  seeded (shipped text vs the seeded text) and whether *you* edited a page
  (current text vs the seeded text). Collapsing them would let the tool call an
  operator's own writing stale. A page that is both is reported as both — the
  case where an automatic update would destroy work.

  Both are **derived at read time with no new table and no migration**:
  `update_page` snapshots the superseded revision into `page_versions` and numbers
  them from 1 without pruning, so version 1 is the seeded text, and a page with no
  versions has never been edited. It reports only — it never rewrites a page, and
  exits 0 even when drifted, because drift is information rather than a failure.

### Fixed

- **A browser could silently overwrite another browser's ISSUE edit.** The page
  editor was fixed first; this closes the same gap on the other half of the
  product. The issue edit form now carries the issue's ETag as rendered, and a
  save that would land on someone else's is refused instead of winning.

  The refusal renders the **opposite way round from a page**, because of a
  missing store rather than a change of mind: a page puts the winner's text in
  the fields since the loser's copy is safe in `page_drafts`, but issues have no
  draft store, so doing that would leave the loser hand-copying out of a `<pre>`.
  On an issue your text stays in the fields, theirs is shown beside it, and the
  notice states the part that is genuinely worse — it is not saved anywhere, and
  leaving the page loses it. Giving issues a draft store would let the two match;
  that is the change this asymmetry waits on.

  Note the collision this actually protects: issue writes are gated to the
  creator or current assignee, so the realistic pair is those two editing one
  description. See [`docs/EDITING.md`](docs/EDITING.md#issues-differ-on-purpose).

- **Seeding the Field Guide could wedge on a partial run.** Found in the sprint's
  adversarial pass: seeding is nine committed writes and re-running refuses if the
  space exists — each rule right, but together a failure partway through left a
  partial space that the retry then refused, so recovery meant deleting a space by
  hand. Content is now read before the first write, so the one failure this code
  can cause (package data that did not ship) creates nothing at all. It is still
  not a transaction: a database failure mid-seed can still leave a partial guide.

- **A browser could silently overwrite another browser's page edit (Stage F-6).**
  The Mentor edit form carried no precondition, so when two people edited one
  page the second save simply won and the first author's work vanished with no
  notice and no trace. The form now carries the page's ETag as rendered, and
  `page_commands.edit_page` compares it inside the same write lock the edit runs
  in — the optimistic lock REST and MCP have always had.

  The refusal is where the design lives. **Nothing is overwritten** (that is what
  the refusal means), **nothing is merged** (Athena will not claim to have
  resolved something a person has to read to resolve), and **nothing the author
  typed is thrown away** — their text is written to their own draft, because a
  bare 412 leaves work living only in a browser buffer and "it is still in the
  form" stops being true the moment they navigate. The form re-renders with
  *their* version in the fields, *yours* displayed beside it, and one click to
  put yours back.

  The draft is recorded against the baseline the author was editing **from**, not
  the page's new tag, so it stays marked stale and the warning survives a closed
  tab — and it is theirs alone, since drafts are owner-scoped personal state.

  Two deliberate softenings, both because the hidden field is a concurrency aid
  rather than an authorization check: a form rendered before the field existed
  sends no tag and keeps the old behavior instead of being refused over something
  its author cannot see, and a malformed tag is treated as no precondition rather
  than becoming a wall between an author and their own page. See
  [`docs/EDITING.md`](docs/EDITING.md#two-people-one-page).

### Added

- **The Field Guide: the workspace documents itself (Stage F-5).**
  `athena-field-guide <db>` seeds nine pages into a `GUIDE` space, addressed to
  the agents who work here: your desk, claiming and yielding work, recording
  learnings, answering a run control, playbooks, searching the workspace,
  watching shared memory, what the trail proves — plus a real playbook you can
  instantiate, so F-2 is demonstrable out of the box rather than described.

  They are **ordinary pages**. Readable through the same MCP tools, findable
  through the same search, linkable from issues, exportable as HTML, editable.
  The guide is a space, not a special surface, so everything true of a space is
  true of it. The content ships as package data (`field_guide/*.md`, the same
  shape `core/migrations` uses) and the existing wheel-manifest gate pins it, so
  a build that drops a page fails rather than seeding a manual with a hole.

  Seeding goes through the real commands as a real author, so every page carries
  genuine provenance and lands on the activity chain. It is **idempotent by
  refusal**: a second run refuses rather than overwriting, because once an
  operator has edited these they are theirs. `--as EMAIL` names the author;
  otherwise the earliest administrator is used, and the command prints who it
  attributed the pages to — an attribution is somebody's name, so it is never
  silent.

  `athena-demo --field-guide` seeds the same content into a throwaway workspace.
  One seeding implementation, two entry points: the demo tool's contract is a
  NEW database, and every other operator command works on one you already have.

- **Workspace search: one ask, everything you may see (Stage F-4).**
  `GET /search/workspace?q=&limit_per_kind=` (MCP `search_workspace`) answers
  across issues, pages, and comments in one call, so an agent no longer has to
  know which module holds the answer before it can ask.

  **The work query grammar works here.** Atoms (`is:open label:infra`) filter
  issues through the issue query compiler; bare words in the same query go to
  full-text search across all three kinds — so `is:open zebra` filters the work
  *and* finds the page that says zebra. Which path applies is decided by the
  parser the issue list already uses (`work_query.parse`), never a second notion
  of "grammar-shaped". An unknown atom is still an error naming the atom, not an
  empty result set.

  Two honesty rules in the shape: results are **grouped by kind, never globally
  ranked** (two engines with two orders cannot be interleaved without inventing
  a score, and the payload says so), and **every group discloses its bound** —
  `clipped` is measured by fetching one row past the limit rather than inferred.
  A pure-grammar query leaves the page and comment groups empty and echoes
  `query.text` so the silence reads as "nothing was text-searched", not "nothing
  matched". Every group is gated by the caller's own visibility.

  It composes the two searches that already exist and adds no third: no new
  index, no ranking invention, no migration. It lives in `workflows/` because it
  reads Aegis's grammar and core's full-text search together, and neither module
  may import the other — the layer added in F-2 is what made this stage possible
  at all. `QUERY.md` records what this deliberately is *not*: the grammar is
  still issue-only, so `label:infra` will not find a labelled page.

- **Space subscriptions: shared memory that says when it moved (Stage F-3).**
  `space` joins `issue` and `page` as a watchable kind, and a space watch is a
  subscription to the whole container: the space's own lifecycle events *and*
  every event on every page inside it — created, edited, archived, restored,
  labelled, moved, commented, deleted. A fleet can now treat one space as
  shared memory and hear it change without polling its page tree. REST is the
  existing `POST /watches` / `DELETE /watches/{kind}/{id}`; MCP gains `watch`
  and `unwatch` (there were none before); the space page gains a Watch toggle;
  the Desk already reports the unread count, so the loop closes with no new
  read.

  **No migration** — `watches` has been polymorphic since 0023 and the
  vocabulary was always code-level. The indirect fan-out lives inside
  `notifications.notify_watchers`, the single place an event becomes inbox
  rows, so there is no second call site to drift: one indexed lookup per page
  event, and `UNIQUE (user_id, event_id)` means watching both a page and its
  space delivers one notification, not two. Your own action still never
  notifies you, on either path.

  It is deliberately loud, and `unwatch` is the only volume control: no digest
  and no rollup, because a quieter second summary of what happened is a second
  source of truth. Notifications stay written ungated and gated at read, so a
  watcher who cannot see the space renders nothing from it.

  **Fixed on the way:** deleting a page notified *nobody* — not even an admin.
  `purge_page` dropped the page row and its watches before `page_deleted` was
  recorded, so both routes to a watcher were gone by the time the event existed.
  The event is now recorded first, inside the same transaction; the ordering is
  invisible from outside. What *renders* is bounded by the existing access
  model: the inbox proves a page event's visibility by looking the page up, so
  once the row is gone the gate fails closed and only an admin's ungated read
  shows the deletion. Documented as a limit, not papered over — making it
  legible to non-admins needs an event-time visibility envelope for page
  targets, which pages do not have and this change does not invent. See
  [`docs/SUBSCRIPTIONS.md`](docs/SUBSCRIPTIONS.md).

- **Playbooks: docs that start work (Stage F-2).** A Mentor page carrying the
  `playbook` label turns its markdown checklist into real work —
  `POST /pages/{id}/start-playbook` (MCP `start_playbook`) creates one parent
  issue plus one child per unchecked `- [ ]` step. Ticked steps are counted and
  skipped, never created: a tick is the author saying it is already done.

  This completes the loop between the modules. Embeds already let docs SHOW
  work and run learnings let work WRITE BACK to docs; playbooks let docs START
  work — and the tie-together costs no new machinery, because every created
  issue cites the page with an ordinary `[[page:N]]` wikilink. The existing
  indexer builds the backlinks, so the page shows the work it started and a
  `kind: rollup` embed there counts its progress, with nothing in the playbook
  command knowing what a link or an embed is.

  A template is not a live mirror: instantiation SNAPSHOTS the page, later
  edits change nothing already created, and starting again makes a second
  independent instantiation. Retry-safety reuses the `Idempotency-Key` contract
  `/pages` already honors rather than inventing a second replay mechanism.
  Bounded at 50 steps (429 above), one transaction, and every write goes
  through `issue_commands` so a playbook is not a second way to create an
  issue. See [`docs/PLAYBOOKS.md`](docs/PLAYBOOKS.md).

- **A `workflows/` layer, for commands that span both modules.** Aegis and
  Mentor are peers — neither may import the other — and `web/` may not own
  authorization, so a command that must read a page and write issues had no
  legal home. `src/athena/workflows/` is that home, enforced by
  `check_import_contracts.py`: workflows may import both modules and core, and
  nothing below may import workflows. Playbooks are its first inhabitant.

- **The Desk — one call, full orientation (Stage F-1).** An agent starting a
  session had to discover its own situation through five or six reads, none of
  which said what changed while it was away. `GET /desk` (MCP `my_desk()`) now
  answers all of it at once: identity with scopes/budget/approval-gated kinds;
  the asks addressed to you (open run controls, unconfirmed kill requests on
  your workers, unacknowledged claim handoffs); the work you hold (delegation
  inbox, leases with the clock's `active` verdict and their 0057 generation);
  and signals (unread notifications, how many visible events sit past your
  cursor). Every lane is the owning surface's own read with the caller's
  visibility, so the desk cannot show more than the tool that owns it and
  cannot disagree with it.

  A durable per-reader **events cursor** (migration 0073) makes "since I last
  looked" real: `POST /desk/cursor` records how far you have drained, moves
  forward only (a lower id is refused 409, and the trigger refuses it again
  below the command), and records no activity event — a read receipt is
  personal state, not fleet history. Two distinctions the desk refuses to blur:
  an unset cursor reads `null`, never `0` ("never looked" is not "nothing
  new"), and the since-count stops at 500 and says it is capped rather than
  reporting a precise-looking total it did not compute. See
  [`docs/DESK.md`](docs/DESK.md).

- **The trail can prove itself.** Every activity row recorded after migration
  0072 gets a same-transaction hash-chain entry (`activity_chain`): SHA-256
  over the row's stored facts plus the previous entry's hash, genesis-anchored,
  with DB triggers making the chain itself immutable and side branches
  unappendable, and the entry's foreign key making chained rows undeletable
  (edits are detected by verification, deliberately not trigger-blocked —
  prevention would be theater against a writer holding the file). Bounded,
  resumable verification reports the FIRST broken link — `GET /activity/chain`
  and `/activity/chain/verify` (admin), MCP `activity_chain_status` /
  `verify_activity_chain`, a full walk in `athena-doctor`, and a tail check on
  `/admin/security`. Imported history is chained *as* imported history
  (`imported_at` is hashed). Rows recorded before adoption sit below the anchor
  and are reported as such, never claimed. Adapted from Buzz's signed event
  log, deliberately without signatures — the honest boundary (rebuild and tip
  truncation detectable only against an externally noted head hash) is
  documented in [`docs/TRAIL_INTEGRITY.md`](docs/TRAIL_INTEGRITY.md).

- **Run controls joined the exception surfaces.** The dashboard's
  fleet-attention rollup now counts **Run controls awaiting an agent** —
  standing, never window-bounded, using the identical `open` predicate the new
  admin-only `/admin/run-controls` page lists by, so the count cannot disagree
  with the page it links to. The page shows every recorded control fleet-wide
  with its truthful state wording, each row linking back to the run's lineage
  panel that owns creation and settlement detail. Closes the "controls live
  only on lineage pages an operator must already know about" limitation
  recorded when Run Controls v1 shipped.

- **Answerability: asks and answers per agent, never a score.** A derived,
  admin-only ledger (`core/answerability.py`, zero tables) lays each agent's
  recorded asks beside their answers: run controls (open / expired-unanswered /
  completed / declined, by the controls page's own predicates), worker kill
  requests (told-to-stop vs confirmed — a worker acknowledging while still
  reporting running stays *unconfirmed*), approvals its gated actions raised,
  and how many of its events an undo reversed. `GET /fleet/answerability`
  (+ `agent_id` filter), MCP `agent_answerability`, and an Answerability
  section on `/admin/agents`. Adapted from Buzz's web-of-trust idea minus the
  reputation scalar, on purpose; the non-claims are in
  [`docs/ANSWERABILITY.md`](docs/ANSWERABILITY.md).

- **Run controls: steering a live run by recorded request.** Between "let it
  run" and "kill the worker" there was nothing. An admin can now record a
  bounded control against a live run — `steer` with guidance, `request_cancel`
  for cooperative wind-down, or `request_fresh_context` for a structured
  handoff — which only the run's bound agent can read and settle (acknowledge,
  decline, or complete; unanswered requests expire by the server clock, an
  observation rather than an event). One row per control (migration 0070) with
  transition-only triggers, at-most-one-live per (run, kind), domain
  idempotency keys, and fail-closed admission resolving the run's owner from
  its binding or sole check-in. REST under `/run-controls`, six MCP tools, a
  panel on the run lineage page, and the same epistemic honesty as the worker
  kill: acknowledgement proves receipt, completion is the agent's claim,
  nothing proves an OS effect (see [`docs/RUN_CONTROLS.md`](docs/RUN_CONTROLS.md)).

- **Editing that keeps its promises, and a way out that needs no Athena.** Page
  and issue editors gained a live side-by-side preview rendered by the same
  function the view itself uses (`render_page_body` / `render_issue_body`), so a
  preview cannot drift from what readers get — including where the two surfaces
  differ, since embeds stay unresolved on issues and preview as the box the
  saved issue will show. Page editors now autosave **drafts**: user-private
  state in their own table (migration 0071) that writes no activity event, is
  visible only to its author, is never exported, and becomes a page only through
  the ordinary audited save, which then clears it; a draft left behind by
  someone else's save is flagged rather than silently restored over their work.
  Attachment **images render inline**, served from a type sniffed out of the
  bytes rather than the uploader's claim, with SVG deliberately excluded because
  it can carry script, and image attachments now show a thumbnail and the
  markdown that embeds them. A space **exports to one self-contained HTML file**
  with images inlined, rendered through the same renderer, in which every embed
  is visibly dead and carries the directive it came from — plus a footer naming
  what was left out. See [`docs/EDITING.md`](docs/EDITING.md).

- **A project timeline, and live parent rollups.** `/aegis/projects/{id}/timeline`
  draws a project's sprints as lanes in date order (undated ones after dated,
  the backlog last), places each issue in its sprint, and draws the declared
  dependencies between drawn issues — a solid arrow for *blocks*, a dashed line
  for *relates*. Lane width is deliberately not a duration: sprint dates are
  nullable and unvalidated, so order is the only time claim made, and the view
  says so. It is read-only — placement still changes through the issue's own
  sprint form — and it states what it left out, including a count of
  dependencies whose other end is off the picture. The same structure is served
  by `GET /projects/{id}/timeline` and the `project_timeline` MCP tool.
  A parent issue now shows its sub-issues' status-category distribution as a
  live bar computed on every read from `aegis/rollups.py`, with buckets taken
  from each child's own project status configuration so a custom done state
  counts correctly; archived children are excluded and said aloud, and children
  the viewer cannot see are excluded silently so the bar cannot become an
  existence oracle. The same computation is available in a page as a new
  `kind: rollup` embed. Per-issue target dates were considered and deliberately
  not built — see [`docs/PLANNING.md`](docs/PLANNING.md).

- **Wheel-bound release-candidate evidence.** The required test gate now
  precedes one fail-closed evidence job that builds a single source distribution
  and its derived wheel with hash-locked tooling. It snapshots the sdist once
  before extraction and carries that SHA-256 through promotion. Bounded raw and
  semantic archive inspection rejects unsafe, noncanonical, or oversized visible
  members and hidden control metadata, then binds the sdist's project metadata
  and complete installable source payload to the wheel. Fresh Linux/CPython 3.12
  base and MCP installs, resolved under
  `constraints/ci-py312.txt`, must match the wheel's recursively evaluated
  metadata closures, including dependency extras. `pip-audit` checks those exact
  third-party name/version sets for known advisories; Athena's verifier then
  creates CycloneDX documents rooted at the exact wheel SHA-256 with matching
  dependency edges. The evidence job uploads its candidate bundle only after
  every verification step passes; it does not sign, attest, tag, publish,
  hash-lock runtime downloads, or claim that `pip-audit` analyzed Athena's
  first-party code.

- **Fail-closed Python supply-chain evidence.** CI now installs its package
  installer from a hash-verified pin and runs the evidence job per change and
  weekly after the required test gate. A hash-locked evidence toolchain audits
  itself, then scans the exact 61-package CI graph plus pip and the setuptools
  build backend with no ignore list or soft-pass path. The resulting CycloneDX
  input SBOM must contain exactly those 63 normalized name/version pairs and zero
  reported vulnerabilities before it is retained as workflow evidence.

- **Sprint lifecycle parity for MCP agents.** Agents can now read one sprint and
  create, edit, start, complete, or delete sprints through the same REST routes,
  creator-only authorization, audited commands, and durable idempotency boundary
  as every other client. `update_sprint` keeps omission distinct from clearing a
  date through explicit `clear_start_date` / `clear_end_date` flags; lifecycle
  state remains reachable only through `start_sprint` and `complete_sprint`.

  Conflicts retain structured status, route, and detail across MCP. Starting a
  second active sprint therefore stays a visible 409. Sprint/project selectors
  reject booleans, numeric strings, floats, zero, and out-of-range integers before
  REST dispatch. Deleting a sprint is explicitly permanent, requires a strict
  `confirm_permanent=true` acknowledgement, and is refused until its issues have
  been moved elsewhere. Sprint descriptions remain last-write-wins because the
  existing REST surface does not emit ETags; this parity slice does not invent a
  divergent lock.

- **Forge integration: evidence flows in** (migration 0069). A registered event
  source can deliver signed GitHub webhooks to `POST /forge/{name}`, and an event
  naming an issue key — in a commit message, a branch name, or a PR title — lands
  on that issue's trail. Your commits and pull requests are finally visible from
  the work item that caused them. See [docs/FORGE.md](docs/FORGE.md).

  **Inbound only.** Athena never calls the forge: no polling, no API calls, no
  stored third-party token. The secret held against a source is *Athena's* — the
  value an inbound request must prove knowledge of — so there is no new egress
  surface and no forge credential in the database to leak. The honest cost is
  that Athena cannot backfill; it knows only what was delivered while a source
  was enabled.

  **Every landed event is imported history** (`imported_at`, migration 0041), and
  that one decision is the whole safety argument: undo already refuses imported
  events, and the lifecycle facts, claim handoffs, assignee facts, fleet metrics,
  and attention rollup all already filter `imported_at IS NULL`. So a forge
  **cannot move an issue's status**, cannot shift a completion-cycle median, and
  cannot be undone into a native write. A merged PR saying "closes ATH-12" is
  recorded as *the source said so*. Imported rows also carry no run coordinates,
  enforced in `activity.record` rather than in each caller, so a delivery can
  never be spliced into an Athena run's replay.

  **Key matching proposes; the database disposes.** `UTF-8`, `SHA-256`, and
  `ISO-8601` are shaped exactly like issue keys; a candidate lands only when its
  prefix is a real project key and its number a real issue. An event matching
  nothing is **counted, not stored** — landing it nowhere would be silent, and
  storing it somewhere would be noise.

  **The signature is verified before the payload is parsed.** The handler takes
  the raw request and declares no body model, because a declared model would put
  FastAPI's JSON parsing ahead of authentication on the one route an
  unauthenticated stranger is expected to hit. A bad signature, a missing
  signature, and an unknown source name all return an identical 401, so the
  endpoint is not a directory of your integrations. Bounded at 512 KB per
  delivery, 20 commits per push, and 10 issues landed per delivery.

  Source registration, pause, resume, and revocation are audited commands. The
  secret is shown once and never again; revoking a source keeps the history it
  already landed, because those events were authentic when recorded.

- **The knowledge graph earns its name.** Athena has stored a link graph since
  migration 0012; it is now traversable, growable, and honest about its bounds.
  See [docs/GRAPH.md](docs/GRAPH.md). Four things, reachable from a
  **Connections** link on any page or issue:

  **Unlinked mentions.** Documents whose text names a thing without linking to
  it — a page by its title, an issue by its key (`ATH-12`, never its title:
  issue titles are sentences that recur in prose, and matching them would bury
  the operator in false positives). Finding a mention **proposes** an edge and
  never creates one; "Link it" rewrites the *source* document through the
  ordinary page or issue command, so the edge arrives attributed, versioned, and
  on the activity trail like any other edit. Full-text search narrows and Python
  confirms against the real body, because returning an unconfirmed prefix-token
  hit would be inventing a mention. Code is not prose: an occurrence inside a
  fence or an inline code span is a literal, not a mention, while a blockquote
  still is one. Text already inside `[[...]]` is skipped even when it resolved to
  no link. If the mention is gone by the time you click, the edit is **refused**
  rather than applied somewhere you did not see.

  **A bounded ego graph**, server-rendered as SVG with no JavaScript — one focus,
  depth ≤ 2 by default, a node ceiling, and a "showing N of M" line whenever the
  ceiling bites, because an unlabelled partial graph reads as the whole
  neighbourhood. Layout is pure arithmetic over a stable ordering, so there is no
  seed to fix and the same graph renders identically every time. Visibility is
  applied **during** traversal: a node you cannot see is not a node and does not
  conduct a path — filtering a finished graph would leave its edges shaping the
  picture and a gap at a known position, which is an existence oracle.

  **Page templates**, with **no new table and no `is_template` column**: a
  template is a page carrying the `template` label, which makes marking one an
  already-audited, already-reversible write and makes "which pages are templates"
  a query Athena already answers. Creating from a template copies the body and
  never the labels — inheriting them would make every page created from a
  template a template itself. Substitution is `{{title}}` and `{{date}}` and
  nothing else; an unknown `{{...}}` is left as written, because it is content.

  **The operator's daily note**: one button per space for today's page, seeded
  from that space's `Daily Note Template` if it has one. Idempotent by
  construction — the lookup and the insert share one transaction, so a
  double-click cannot produce two notes — and revisiting writes **nothing at
  all**: no event, no budget charge, because visiting an existing note is a read
  and an event per visit would make the trail lie about when the page came to be.

- **Live embeds: a page can show real work.** A Mentor page may carry a fenced
  ` ```athena ` directive — `kind: issues` with a [work query](docs/QUERY.md),
  `kind: count`, or `kind: issue` — that renders at view time as real rows. A
  runbook can now *display* its issue's live state rather than describing it, and
  a page stops being a place where copied-in status goes stale. See
  [docs/EMBEDS.md](docs/EMBEDS.md).

  **Nothing is stored.** The page holds the directive; the data resolves fresh for
  whoever is looking. A snapshot in page content would be a staleness lie the
  moment the work moved and a visibility leak the moment someone else opened the
  page — so **visibility is the reader's, never the author's**. An embed written
  by an admin shows a member only what that member could already see, and two
  people opening one page can legitimately see different rows. A single issue the
  reader cannot see is reported exactly like a missing one, so an embed is not an
  existence oracle for private work.

  **A directive that cannot render says so, in place, with the reason.** A bad
  query, an unknown kind, a typo'd key — each renders a visible error box, one
  broken directive never breaks the others, and a page never fails to load because
  an embed was mistyped. Bounded and honest about it: at most 10 rows by default
  (50 ceiling) and 10 directives per page, with a truncated list saying "Showing
  10 of 42" rather than implying the window is the whole answer.

  Directives are extracted **before** Markdown and substituted back after
  sanitizing, the same way `[[ref]]` tokens already survive the pipeline. That is
  not a preference: the sanitizer strips the `class="language-athena"` that would
  identify the fence afterwards (verified, not assumed). The approach also means
  embed HTML is built by Athena from escaped values and never passes through the
  sanitizer as author markup at all. The placeholder carries a **per-render
  nonce**, so an author cannot write a literal token and have Athena substitute
  someone else's embed into it — with a test that fails if the nonce becomes
  predictable.

  Agents get **data, not markup**: `read_page_embeds`, `resolve_embeds`, and
  `embed_help` over MCP, plus `POST /embeds/resolve`. That endpoint takes *text*
  rather than a page id because the import contract makes Aegis and Mentor peers;
  the MCP client composes the page read and the resolve so an agent still makes
  one call, and gains the ability to preview a body it is about to save.

- **One query language over all work.** Athena had filters but no *language*: an
  operator wanting "open infra issues in ATH assigned to me, most urgent first"
  clicked three controls and could neither save, share, nor hand that sentence to
  an agent. Now `is:open label:infra project:ATH assignee:@me sort:priority-desc`
  works in the browser, over REST (`GET /issues?q=`), from MCP (`search_work`),
  and inside a saved filter. The shape is GitHub's, not Jira's — space-separated
  `field:value` atoms joined by AND with `-` to negate — because a grammar with no
  operators has no precedence for a human *or an agent composing from a docstring*
  to get wrong. See [docs/QUERY.md](docs/QUERY.md).

  Three properties are the design, not details. **An unknown atom is an error
  naming the atom**, never an empty result: a query box that answers "no results"
  to a typo has invented an answer, and the operator concludes there is no
  matching work when really there was a missing `s`. **Visibility is composed into
  the SQL**, never filtered afterwards, so a bounded page is a full page of
  visible rows rather than a partial one with the hidden results silently removed
  — the difference is observable in `limit`, and there is a test that fails
  without it. **`is:open` is category-based**: it resolves through the same
  status-category expression the fleet views use, so a project whose done state is
  called `shipped` behaves correctly and closed-ness has one definition.

  The parser lives in `core` and is pure — no database, no domain imports — so the
  grammar is testable without fixtures and the same parse result drives every
  surface; what an atom *means* lives in the domain layer with the tables it
  queries. `q` and the structured filters are mutually exclusive (a 422, not a
  merge). Saved filters validate their query at write time, so one that could
  never run cannot be stored.

### Fixed

- **Private issues no longer leak through executor dispatch reads.** Authenticated
  outsiders could receive a private issue's repository, commit, run, policy,
  idempotency, evidence, and error metadata from `GET /dispatches` and
  `GET /dispatches/{id}` even while the issue itself correctly returned `404`.
  Dispatch list and detail now inherit issue/project visibility in one SQLite
  statement; hidden and missing detail reads are indistinguishable, and visibility
  is applied before `LIMIT` so newer hidden rows cannot under-fill a visible page or
  become a pagination oracle. The same rule reaches MCP through its REST client.
  Dispatch reads now also enforce `read` or `issue:write` bearer scope, and oversized
  dispatch/work-item identifiers fail validation instead of overflowing SQLite.

- **Icarus callbacks now have a real authentication and replay perimeter.**
  Anonymous callback attempts charge the direct-peer-IP limiter before body work;
  HMAC verifies raw bytes before JSON parsing or SQLite; malformed/non-ASCII
  signature text is a 401 instead of a 500; and authenticated malformed Unicode
  cannot escape into SQLite. Mounted deployments classify the route after removing
  ASGI `root_path`, so a path prefix cannot bypass the limiter, idempotency
  exemption, or cookie/SQLite guard. A non-ASCII policy digest is recorded as a
  safely labelled mismatch rather than crashing. Outbound dispatch creation now
  checks mutable issue visibility after reserving SQLite's writer slot, closing the
  membership-revocation race between authorization and the dispatch/audit record.

  The first evidence pointer is now canonical and immutable. Exact callback
  replays produce no duplicate activity, a different pointer conflicts while the
  dispatch is open, and terminal state absorbs outcome changes and evidence
  overwrites. A legacy terminal report that omitted evidence can still have its
  null evidence slot filled once by delayed progress. Evidence, terminal state,
  digest warnings, and their events are owned by one atomic callback command. The
  reference executor sends cumulative terminal callbacks so network reordering
  converges without a schema migration, retries the finite pre-acceptance 404
  race, and single-flights repeated in-process delivery keys. Delivery acceptance
  and failure state now commit atomically with their audit event and use
  first-writer-wins predicates under concurrency; executor run ids round-trip
  exactly, while malformed or cross-dispatch duplicates become visible
  `undeliverable` outcomes. The remaining need for a sequenced multi-evidence
  callback protocol is documented explicitly.

- **Wave H-2: documentation reconciliation, and one real bug found while
  verifying it** (`docs/OPUS_REMEDIATION_GUIDE_ATHENA.md`).
  - `POST /labels` answered 500 on a duplicate-name race: two concurrent creates
    both passed the pre-check, and the loser surfaced the UNIQUE-constraint
    `IntegrityError` unhandled. It now maps to the same 409 the pre-check would
    have given, with a regression test that stands in for the lost race.
  - The docs named by the review's honesty pass are reconciled with the code:
    FORGE.md (event-source secrets are stored plaintext for HMAC, unlike hashed
    API tokens; the github.com webhook shape needs a tunnel or scoped reverse
    proxy, and says so), RELEASE_READINESS.md (gate re-run at HEAD — 148
    modules, 69 migrations, 2,633 tests — with the Stage M–P risks added and
    HOLD re-affirmed), COMMAND_MIGRATION.md (saved filters, watches, read-marks,
    standalone label create, and the Stage M–P surfaces now have rows; the
    transport-side authorization debt the "None known" columns hid is recorded),
    UNDO.md (eleven reversible verbs, not seven), this file's 0.1.0a1 section
    (status/assignee undo and the approval-kind count no longer contradict the
    entries beside them), AGENT_BUDGETS.md (dispatch, template, and daily-note
    charges added to the metered-writes table), and ARCHITECTURE.md (the stale
    "Phase 3 (current)" marker and the phase-numbering collision with
    ROADMAP.md are resolved).
- **Wave H-0: the five stop-ship defects from the adversarial review**
  (`docs/OPUS_REMEDIATION_GUIDE_ATHENA.md`).
  - Event-source CRUD now enforces the admin **token scope** through the same
    `admin_actor` dependency as every parallel admin surface, not just the admin
    role — an admin's read-scoped token can no longer register a source and walk
    off with its signing secret.
  - A non-ASCII signature or CSRF token is a wrong credential, not a 500:
    `hmac.compare_digest`/`secrets.compare_digest` raise TypeError on non-ASCII
    str operands, and the crash made `POST /forge/{name}` a one-request oracle
    for which source names are registered. Unknown sources now also pay for the
    same HMAC work, so known and unknown refuse identically.
  - The automation engine's event scan excludes imported rows in SQL — imported
    `forge_commit` history no longer fires wildcard rules and moves issues.
  - **The security refusal counters and the attention rollup now filter
    `imported_at IS NULL` — a guard the forge docs and the 0069 entry below
    claimed already existed. It did not; it was added after the review.** A
    hostile import bundle can no longer back-date security verbs into the 24h
    window to plant fake refusals on `/admin/security` or inflate the attention
    card.
  - The browser label-attach route authorizes before it find-or-creates: the
    shared label vocabulary no longer grows on a refused (viewer-role or
    hidden-issue) request. Find-or-create moved into the
    `issue_commands.attach_label_by_name` command, one transaction with the gate
    and the audit event.
- Supplied MCP issue-list sprint IDs now accept only JSON integers within
  SQLite's bounds; booleans, numeric strings, and floats fail before REST dispatch.
- MCP read-tool failures now preserve the same machine-readable HTTP status,
  error code, retry delay, and current ETag metadata as mutation-tool failures.

### Changed

- **The default-status→category mapping now has exactly one definition.**
  `fleet_work.py` carried two hand-written SQL copies of it; the query compiler
  would have been a third. `statuses.category_sql()` generates the expression from
  `DEFAULT_STATUSES`, so adding a default status updates every SQL site at once
  instead of leaving the copies to drift — the failure mode Stage I spent a whole
  slice fixing.

## 0.1.0a1 — 2026-07-26

The release candidate of the `0.1.0a1` line. The complete documented release gate
— dependency freeze, lint, formatting, whole-runtime typing, import contracts,
the full suite with enforced coverage floors, the process smoke, the field
exercise, and the sdist → wheel → external-boot packaging recipe — was run at this
exact tree and is recorded in
[`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md). Applying the tag is the
release owner's decision, and until one exists this stays an untagged milestone
like the two below it.

### Added

- **The operator loop is field-exercised against a real counterparty.**
  `examples/icarus_executor.py` is a reference Icarus executor — one stdlib-only
  file that never imports Athena (a test pins both), verifies the envelope
  signature before parsing a byte, echoes the policy digest, signs its
  callbacks, and retries them, because the callback endpoint is idempotent and a
  one-shot report would be lost to any transient failure.
  `scripts/field_exercise.py` (run in CI by `tests/test_field_exercise.py`)
  boots Athena and that executor as real processes and drives the whole loop —
  onboard → delegate → claim against the reviewed ETag → heartbeat → work under
  a run → a gated dispatch refused, approved, retried, delivered, and completed
  by signed callbacks → a learning promoted into the runbook the work-context
  packet then serves → an operator undo — over real loopback HTTP with real
  HMAC on both sides, nothing stubbed. Its first run found both items under
  Fixed below, which is the argument for its existence.
- **Operators can allow egress to hosts they own.** The SSRF guard refuses any
  host resolving private/loopback/link-local — correct against
  attacker-registered webhook URLs, but it also made `ATHENA_ICARUS_URL`
  pointing at the operator's own machine, LAN, or tailnet impossible: every
  dispatch to a local executor landed `undeliverable`.
  `ATHENA_EGRESS_PRIVATE_HOSTS` is an exact-hostname, case-insensitive,
  no-wildcard allowlist set in the process environment — the same trust channel
  as the shared secret itself, not the attacker-reachable API. Empty (the
  default) keeps the policy absolute; delivery still pins the connection to the
  resolved address either way.
- **An assignment can be undone.** `assigned` events recorded only the new
  assignee's display name — not unique, not a prior value, and a re-assign read
  identically to a first assign — so unlike status (whose prior state 0055 had
  recorded all along) there was genuinely nothing trustworthy to restore.
  Migration **0068** (`issue_assignee_facts`) records the typed before/after ids
  in the same transaction as the event, keyed 1:1 to it and append-only — the
  assignee twin of the lifecycle facts. The compensator applies the same scalar
  discipline as status undo: the assignment must still be in force (undoing a
  superseded event refuses rather than silently unseating a newer assignee), a
  pre-0068 event refuses rather than guessing from prose, and the ordinary
  command — run as the undoing actor — owns authorization, budget, and gates.
  The assignee columns keep a plain `REFERENCES users(id)`, like
  `activity.actor_id`: Athena never hard-deletes a user, so the reference always
  resolves.

- **Configuring a project's statuses is a command, and is audited.** A project's
  status set *is* its issue lifecycle: adding a `done`-category status changes what
  "closed" means for its board, its dependencies, and its blocked-close policy.
  Both writes took no actor, committed on their own, and recorded nothing, so the
  append-only trail could not answer who changed the vocabulary — the last
  unaudited durable write in the Aegis project surface. Add and remove now own the
  row change, a `project_status_added` / `project_status_removed` event, and the
  authorization, all in one transaction, with the credential re-resolved from live
  rows inside the write lock. **Authorization is relocated, not changed**: the rule
  is still visibility-first then creator-only, so a private project stays a 404
  rather than a 403 that confirms it exists, and an agent that created a project may
  still configure it. Narrowing that is a product decision, not something a
  migration slice should do silently. The in-use guard and the `DELETE` now share a
  transaction, closing the window where an issue could be moved into a status
  between the check and its removal.
- **An issue's status change can be undone.** `changed_status` is the first verb
  whose inverse needs prior state — and it needed **no new storage** to get it:
  migration 0055 has recorded `before_status` structurally and immutably, in the
  same transaction as the event, since long before undo existed. `docs/UNDO.md` and
  the migration inventory both said otherwise; both are corrected. The compensator
  reads that row and never parses `activity.detail`, which carries the same
  transition as prose. Two gates matter more than the reader: a scalar field has no
  domain idempotency, so the engine's "recorded nothing means nothing to undo" net
  cannot catch a stale undo. Without them, undoing an old status event would
  overwrite a **newer** value and stamp the result as a reversal — a false entry on
  an append-only trail. So the change must still be in force (else `undo_no_effect`)
  and the issue must not have moved projects, which *remaps* statuses (0024). See
  [docs/UNDO.md](docs/UNDO.md).

### Changed

- **Every automation rule action now reaches a command owner.** `add_label` and
  `add_contributor` were the last two composing their own transaction, data-layer
  write, and activity event beside the command that already owned those writes.
  They route through `attach_label_as_automation` / `add_contributor_as_automation`,
  twins of the existing `update_issue_as_automation` — same narrow bypass, same
  identity assertion, and now re-made *inside* the write transaction, so a caller
  cannot reach the per-issue policy bypass by passing another user's id. The
  changed-or-not contract the schedule receipts depend on is preserved, so a
  replayed firing still records exactly one event. **One behavior change worth
  naming: pausing the Automation account now stops label and contributor firings
  too.** It already stopped assign and set_status; label and contributor bypassed
  that check entirely and kept writing. Rule firings stay deliberately unmetered
  and ungated — a budget or an approval gate must never silently stop a rule the
  operator configured.
- **Athena can hand work to an external execution fleet.** It is a control plane;
  an executor is a separate system with its own store. They share no database and
  neither imports the other — they reconcile over an asynchronous HTTP contract,
  and `icarus_dispatches` (migration 0067) is Athena's half of it. A dispatch
  records what Athena **asked**, under a tamper-evident digest of the authorization
  in force; the executor reports evidence and an outcome through a signed callback.
  **Every state is what Athena was told, never what is happening on the far side**:
  `accepted` means the executor said it accepted, not that work is running, and
  nothing here pretends to see a system it cannot. Evidence is *referenced*, never
  copied. The outbound call is a **post-commit side effect** — a network call inside
  a transaction would hold SQLite's single writer for as long as a stranger's
  server takes, and the durable fact "Athena decided to dispatch this" must survive
  a far side that never answers, so a failure is recorded as `undeliverable` with
  its reason. Dispatch is **metered and gated like any other write** — under its
  own `dispatch.request` approval kind (see Fixed below), because otherwise a
  gated actor could route around approvals by asking an executor instead. Callbacks carry **no Athena credential** — they are
  authenticated by HMAC over the exact body, checked before any lookup so the
  endpoint cannot be used to probe which dispatches exist — and can do exactly two
  things: attach evidence and report an outcome. A digest mismatch is **recorded and
  flagged**, not discarded, because destroying the evidence would defeat the point
  of computing it. Egress reuses the webhook SSRF hardening in full, and `icarus:`
  joins `automation:` as a reserved run namespace so nobody can forge control-plane
  evidence of what an executor did. Off unless `ATHENA_ICARUS_URL` and
  `ATHENA_ICARUS_SECRET` are both set, refusing with a 503 rather than accumulating
  undeliverable rows. See [docs/DISPATCH.md](docs/DISPATCH.md) — including what
  Athena never verifies.
- **Run learnings close the memory loop.** VISION's fifth step promised that
  corrections "feed back into Mentor as durable context the agents read next
  time"; Mentor pages were already read by agents, but nothing ever wrote back, so
  every run started from the knowledge the last one had. A human or an agent can
  now promote what a run learned into the issue's **runbook** — one Mentor page
  bound to that issue (migration 0066). The entry references the issue, so the link
  index, the issue's backlinks, and the next agent's work-context packet pick it up
  **through machinery that already existed**; nobody had to wire the feedback path.
  Three constraints are deliberate. **Promotion is explicit** — nothing fires on
  completion or yield, because handoff text is untrusted advisory input and the
  operator decides what earns a place in the knowledge base. **Promoted text is
  quoted, not merged** — it is stored as a blockquote under an attribution header
  Athena writes, so a summary containing its own headings renders inside the quote
  instead of forging a second attribution beside the real one, and nothing in it is
  ever executed. **Provenance is verified** — a named `run_id` must be a run that
  actually exists and is visible to the recorder, or the promotion is refused
  rather than recorded with invented attribution. The runbook binding is a row
  rather than a title lookup, so renaming the page cannot silently fork the memory
  in two. Surfaced through `POST /issues/{id}/learnings`, `GET
  /issues/{id}/runbook`, MCP `record_run_learning` / `get_issue_runbook`, and a
  form on the run lineage view — where looking at what a run did is exactly the
  moment an operator knows what the next one should be told. See
  [docs/RUN_LEARNINGS.md](docs/RUN_LEARNINGS.md).

- **Security signals have a surface.** Failed logins, revoked tokens still being
  presented, scope denials, and paused-account refusals have been recorded on the
  activity trail since they were added — and were readable only by an operator who
  already knew the four verb names and thought to filter for them. Probing before
  compromise is exactly the signal that must not require knowing to grep, so it now
  has `GET /security/events`, `GET /security/counts`, MCP `list_security_events`,
  and an admin **Security signals** page. Admin-only, with a closed verb vocabulary
  so the surface cannot quietly become a general activity reader wearing a security
  name, and zero-filled counts so a quiet fleet reads as an explicit zero.
- **One fleet-attention rollup on the dashboard.** Athena's exception surfaces grew
  one at a time and each landed on its own page, so an operator expected to steer
  by exception had to know six places to look. An admin-only card now counts claims
  needing attention, approvals waiting, workers told to stop, failing automation
  rules, failing webhooks, budget ceilings hit, and boundary refusals — each linking
  to the surface that owns it. **The card computes nothing**: every number comes
  from the surface that owns it, so it can never disagree with the page it sends
  you to. Standing state is not window-bounded (an unanswered approval is still
  unanswered a month later); event-counted signals are, because a probe from months
  ago is not this morning's alarm. A quiet fleet reads "nothing is asking for you
  right now — that is what the last N hours recorded, not a promise that every agent
  is healthy". `base.html` also gained the Mission Control and Security links it
  never had. See [docs/EXCEPTION_SURFACES.md](docs/EXCEPTION_SURFACES.md).
- **A worker registry with a cooperative kill request** — Athena models *who* an
  agent is, and check-ins prove a credential reported a *run*; neither answers
  "which of my agent processes are up, on what box, and can I tell one to stop?"
  A worker now registers itself by heartbeat (`PUT /workers/heartbeat`), declaring
  a node label and self-declared capabilities, and an admin can ask it to stop.
  **The kill is cooperative, and the schema says so.** Athena cannot signal a
  foreign OS process, so it records three separate facts — the operator *asked*,
  the worker *said it heard*, the worker *said it stopped* — rather than one
  `killed` flag that would invent certainty. The worker learns of the request on
  its next heartbeat (`kill_requested: true`); honoring it is the worker's job.
  A worker that goes quiet is **stale**, never terminated; one that acknowledged
  and kept reporting is surfaced as `acknowledged_but_reporting`, because hiding
  that would undo the point. A restart does not cancel the instruction, asking
  twice does not reset how long it has been ignored, and a request can be
  withdrawn only until it is acknowledged. Registration requires an agent account
  holding a live write-scoped bearer token, re-resolved inside the write
  transaction; a browser session, a human's token, and a read-only token are all
  refused. First registration is audited, refreshes are not, and every kill
  request, cancellation, acknowledgement, and stop is. Surfaced through
  `GET`/`POST`/`DELETE` on `/workers`, MCP `worker_heartbeat` / `list_workers` /
  `request_worker_kill` / `cancel_worker_kill`, and a per-agent worker list plus a
  "Told to stop" queue in the cockpit. Worker events are admin-only on the trail.
  See [docs/WORKERS.md](docs/WORKERS.md) — including how pause interacts with a
  kill request, and why there is still no process-level kill.
- **Undo by compensation** — VISION promises the operator can undo what an agent
  did; ARCHITECTURE promises the trail is append-only. Undo never deletes or edits
  a row: reversing event *N* runs the inverse as a new, fully audited forward
  command whose event carries `reverses_event_id = N` (migration 0064), so history
  gains two rows and a replay shows the action *and* its reversal. Authorization is
  **re-evaluated, never inherited** — the compensator runs as the undoing actor
  through the ordinary command owner, so undo is not a back door into someone
  else's write, and it can itself be refused by a budget or an approval gate like
  any other write. An undo that would change nothing is a **refusal**, not a
  cheerful no-op: every compensator is idempotent at the domain layer, so "nothing
  was recorded" means the effect is no longer in force. Single use is enforced by a
  partial unique index rather than a read-then-write check, so two concurrent undos
  cannot both compensate. Reversible when this shipped: issue archive/unarchive
  and label/unlabel, page archive/unarchive and label/unlabel — four pairs whose
  inverse needs no prior state. Everything else is refused *with its class*:
  one-way (a comment people read, a published attachment), trapdoor (a destroyed
  row), or unclassified. Imported history and events the actor cannot see are never
  undoable. Surfaced through `POST /activity/{event_id}/undo`, the
  `reverses_event_id` field on every event read, MCP `undo_action`, and an Undo
  control on reversible rows of the activity feed. See [docs/UNDO.md](docs/UNDO.md).
  **This entry described the first slice and its tail is corrected here:** it
  originally closed by explaining why `changed_status` and `assigned` were *not*
  reversible. Both became reversible later in this same cycle — status from the
  structured prior state 0055 had recorded all along, assignee from the new 0068
  facts — and are documented under Added above. The reversible set at this
  milestone is eleven verbs, not four pairs. This is undo by compensation for a
  bounded set of actions, not general undo.
- Human-in-the-loop **approval gates** — VISION's Intervene step promised the
  operator can "approve/reject risky actions"; the only gate before this was the
  per-project blocked-close policy, which can refuse but cannot *ask*. An admin can
  now require operator approval before a chosen actor takes a chosen action kind
  (`issue.close` when this shipped; `dispatch.request` joined the vocabulary when
  dispatch stopped borrowing the close kind — see Fixed below, so the kinds at
  this milestone are two, each naming one intent). The gated write is refused with `202` and a recorded ask
  naming who wanted to do what to which target, carrying the requesting run's id;
  the operator approves, and the **agent retries** the same write.
  Deliberately **gate + retry, not deferred execution**: Athena never stores a
  mutation and replays it later on the agent's behalf, which would re-execute a
  stale payload under authorization evaluated at a different moment. Because the
  retry is an ordinary write it re-validates everything — visibility, scope, the
  blocked-close policy, the budget, `If-Match` — so an approval authorizes an
  intent, never a stored side effect. Approvals are **single-use** (consumed inside
  the retry's own transaction) and bound to both requester and target. A rejection
  answers with `409` and the `approval_rejected` code so an agent stops rather than
  waits. Gating is **opt-in**: applying migration 0063 changes nothing until an
  operator gates someone, and automation rule firings are never gated — the
  operator's own automation must not wait on the operator. Surfaced through
  `GET /approvals`, `POST /approvals/{id}/decision`, the
  `/approvals/policies/{user_id}` policy routes, the `approval_required` field on
  `/users/me`, the MCP tools `list_approvals` / `decide_approval` /
  `set_approval_policy`, and a "Waiting on you" queue in the agent cockpit. Every
  step is audited (`approval_requested`, `approval_approved`, `approval_rejected`,
  `approval_consumed`, `approval_policy_set`, `approval_policy_cleared`). See
  [docs/APPROVALS.md](docs/APPROVALS.md) for the guarantees and the limitations —
  two action kinds, no expiry, no bulk decide, no un-reject.
- Durable per-agent **action budgets** — the bounded half of "attributable,
  reversible, and bounded". An admin caps how many metered writes an agent may
  make per fixed window (`hour` or `day`); the charge is folded into the metered
  command's own transaction, so a refused or failed write spends nothing and two
  concurrent writes cannot both spend the last unit. Unlike the in-process token
  rate limiter, the counter survives restart. Metering is **opt-in**: a user with
  no budget is unlimited, so applying migration 0062 changes nothing until an
  operator sets a ceiling. Exhaustion returns `429` with the stable
  `agent_budget_exhausted` code, a `Retry-After`, and the budget itself, and lands
  on the activity trail. Surfaced through `GET`/`PUT`/`DELETE /users/{id}/budget`,
  the `budget` field on `/users/me` (so an agent learns its ceiling by asking
  rather than by being refused), the MCP tools `get_agent_budget` /
  `set_agent_budget` / `clear_agent_budget`, and the agent cockpit. Budgets meter
  **actions, not tokens or dollars**: Athena never observes an agent's model
  spend, so it deliberately carries no cost column it could not honestly populate.
- `PUT /users/{id}/password`, the REST parity surface for the admin password
  reset that previously existed only in the browser. Authorization (admin role
  plus the admin scope for a bearer token) is enforced inside the command, so the
  two transports cannot drift.
- MCP `label_page` / `unlabel_page` tools, so agents can manage the shared label
  vocabulary on Mentor pages through the same REST behavior the browser uses —
  closing an API/MCP parity gap for an existing durable write.
- A visibility-safe Fleet Throughput page, strict REST endpoint, and MCP tool
  backed by one bounded snapshot service. New append-only lifecycle facts preserve
  event-time terminal categories, project scope, predecessor chains, and actor type;
  monotonic issue identities prevent deleted-target rebinding, legacy/imported
  ambiguity is reported as coverage, valid typed history survives manual target
  deletion for full-visibility admins, partial-visibility cycle timing is withheld,
  hidden work affects no aggregate,
  and query-plan tests pin the targeted time-window index.
- An admin-only active claimed-work projection across Mission Control, REST, and
  MCP, joining durable leases to exact claim runs, cooperative reports, current
  holder access, blockers, and replay evidence without inferring process health.
- Generation-fenced issue leases across REST, MCP, and Active Work. Every fresh
  acquisition receives a new opaque generation, renew/release mutations require
  the exact current generation, and delayed commands from an earlier possession
  fail without exposing the replacement token.
- Typed claim handoffs across yield, claim, work context, delegation inboxes,
  Active Work, and browser views. A holder yields bounded attempted work,
  evidence, a blocking question, and resume instructions atomically with its
  lease release; the next holder must explicitly acknowledge the untrusted
  advisory context before completing the claim.
- Project- and sprint-scoped fleet boards with explicit agent, human, and
  unassigned swimlanes; filter state survives HTMX, drag, and no-JS moves while
  private project and sprint names remain visibility-gated.
- An additive `assignee_is_agent` issue projection plus optional sprint
  filtering on the MCP `list_issues` tool, so agent clients can query the same
  sprint and actor dimensions as the web board.
- A public roadmap (`docs/ROADMAP.md`) grounded in a full-codebase review:
  phased plans for the agent loop, run integrity, docs-as-agent-memory, and
  fleet operations.
- Deterministic attachment reconciliation for local/tailnet operations, reporting
  missing, checksum-tampered, size-mismatched, unreadable, non-regular, and orphan
  storage without following symlinks or hashing FIFOs/devices/sockets. Doctor can
  run the reconciliation against a selected database and attachment directory and
  reports bounded category counts rather than blob names or content.

- Claim acquisition and same-holder renewal now require exactly one strong root
  issue ETag across REST and MCP, with explicit missing, malformed, oversized, and
  stale-precondition responses and durable exact-retry replay.
- The test gate runs in parallel (`pytest -n 4` via pytest-xdist), cutting the
  suite from ~16 minutes to ~3 — the suite was already deterministic
  (no sleeps, per-test databases), so no test changed.
- Corrected four stale docstrings/comments that no longer matched the code
  (issue status "canonical set", webhook payload/event parity, JWKS caching,
  comment cross-link rendering), and removed operator-environment references
  from the contributor docs. Research planning notes moved to `docs/research/`.
- Made Python 3.12 the only supported runtime and changed boolean, numeric,
  floating-point, log-level, and partial OIDC configuration errors to abort startup
  instead of silently accepting an unsafe value or incomplete identity setup.
- Hardened `/readyz` and `athena-doctor` to validate the exact packaged migration
  inventory and applied checksums. Doctor additionally runs SQLite integrity and,
  when `--attach-dir` is supplied, attachment reconciliation.
- Staged database restore candidates through SQLite `quick_check`, durable atomic
  replacement, and automatic recovery of an existing target after sidecar cleanup or
  swap failure. Recovery names are directory-synced before destructive work and all
  candidate stages are cleaned on failure. Operations guidance now treats SQLite plus
  its matched attachment-directory snapshot as the complete stopped-service recovery
  unit.
- Graceful application shutdown now cancels both in-process background runners,
  awaits both, and surfaces non-cancellation failures. The supported deployment
  remains one process/runner on a trusted local machine or tailnet.

### Fixed

- **The source distribution shipped a gate it could not run.** `MANIFEST.in`
  included `scripts/` but not `examples/`, so an sdist carried
  `scripts/field_exercise.py` while omitting the `examples/icarus_executor.py`
  it spawns by path — the release gate was unrunnable from a source
  distribution. Found by building the sdist during the 0.1.0a1 packaging run,
  one stage after the exercise was added. The exercise now passes **from an
  extracted sdist**, and a test asserts `MANIFEST.in` ships whatever directory
  the exercise spawns from, so the two cannot drift apart again.
- **A real executor's acceptance is now actually read.** The hardened delivery
  poster drained and **discarded** every 2xx response body, so
  `{"icarus_run_id": ...}` — the id the executor announces itself under, and the
  key its callbacks use — never reached Athena. Every real dispatch was silently
  correlated by the fallback idempotency key while the executor called back
  under the run id it had actually declared, and every callback answered 404.
  No stubbed test could see this: the injected posters all returned their
  bodies; only the real `http.client` path dropped them. Found by the field
  exercise's first run. The poster now returns a bounded (64 KiB) success body,
  which webhook delivery — the other caller — never reads on success.
- **A close approval can no longer be spent by a dispatch.** Dispatch borrowed
  `issue.close`'s approval policy row when it shipped: gating an agent's closes
  silently gated its dispatches too, and — the sharp edge — an approval the
  operator granted for *closing* an issue could be consumed by a *dispatch* of
  that issue instead. An approval authorizes the intent the operator read on the
  ask, so two actions must never share a kind. Dispatch now has its own,
  `dispatch.request`, in the closed vocabulary (no migration — 0063 deliberately
  left `action_kind` as code-owned free text). **Behavior change to note:** an
  operator who relied on the borrowed coupling must now gate `dispatch.request`
  explicitly; a close gate no longer touches dispatch, and vice versa.
- **Attention-bearing claims are no longer the rows most likely to be hidden.**
  The active-work window sorted active leases before expired ones, so on a busy
  fleet the expired — attention-bearing — claims were exactly the rows the limit
  dropped, while the returned-items summary could truthfully report `0 need
  attention` about a page it had filtered clean of them. An exception surface that
  hides exceptions is worse than none, because it reads as reassurance. The window
  is still bounded but now fills from the urgent end, the exact per-row attention
  state sorts the returned page, and a new `attention_state` filter (web, REST, and
  MCP) returns precisely the rows needing a human. `examined_count` was added
  because "0 need attention" is otherwise ambiguous between "none do" and "none of
  the ones we looked at do" — different statements on a clipped fleet. The SQL
  ordering is explicitly a bias, not a second definition of attention: it does not
  reproduce the check-in, blocker, or token reasons, since two implementations of
  one predicate eventually disagree.

- The sprint lifecycle is now audited. Creating, editing, starting, completing,
  and deleting a sprint were all bare data-layer writes with no activity event on
  any transport, so the trail could not answer who started an iteration or who
  deleted the one that held last week's work — and the surface was absent from the
  command-migration inventory entirely. `aegis/sprint_commands.py` now owns each
  write: the row change and its event commit or roll back together, from both REST
  and the browser. The one-active-sprint serialization is preserved, and a no-op
  edit still records nothing.
- Project visibility and membership writes are now audited-atomic commands
  (`project_commands.set_project_visibility` / `add_project_member` /
  `remove_project_member`). The visibility flip previously ran as three
  independent commits — the flip, the creator's roster row when going private,
  then the activity event — so a crash mid-sequence left a permanently unaudited
  access-control change: because the mutation is idempotent and the event is
  recorded only on a real change, a retry never backfills it. REST and the browser
  now call the same commands, which no longer emit activity from either transport.
- Run-lineage coordinates must now name something real. `parent_run_id` and
  `forked_from_event_id` arrived as client headers and were stored verbatim, so
  any writer could fabricate ancestry out of thin air — naming a run nobody ever
  wrote, or a fork point that is not an event of that run — and `run_lineage()`
  plus the replay artifact build their trees from exactly those columns. A parent
  is now kept only when that run has actually been written, and a fork point only
  when it is a real event belonging to the surviving parent; a failing coordinate
  is stored as `NULL` rather than rejected, since the headers remain correlation
  hints, not authorization. Cross-actor lineage stays legitimate on purpose (the
  fork contract and automation both depend on it), and a rule firing on an
  untagged trigger keeps its real fork point while having no parent run.
- Password writes are now audited-atomic commands
  (`user_commands.change_own_password` / `reset_user_password`). Both paths ran
  the hash write and the session revocation as separate commits with no audit
  event on any transport: a crash between them left a rotated password with live
  sessions, and an admin could reset any account — taking it over, since later
  writes are attributed to the target — with nothing on the append-only trail.
  The hash write, the revocation that rotation forces, and the event now commit or
  roll back together, and the password and its hash never reach the trail.
- The automation engine's per-firing idempotency guard can no longer be forged
  from a client run-id header. A rule's firing run id is predictable
  (`automation:rule-N:event-M`) and the guard treats any activity row carrying it
  as proof the firing already happened; client `X-Athena-Run` values were stored
  verbatim, so a write-scoped client could pre-stamp that id and silently suppress
  the rule. The request edge now drops a client run id in the reserved
  `automation:` namespace (the engine still mints those ids in-process); forking a
  child run from an automation run is unaffected.
- Pausing an account now also freezes its stored idempotency receipts. The
  idempotency middleware previously authenticated by credential liveness alone
  and served stored replays without reaching the identity-layer pause gate, so a
  paused agent credential could still read completed mutation responses for up
  to the receipt TTL; `paused_at` was also missing from the authorization-
  revision fence triggers. A paused account's keyed request now skips
  claim/replay entirely and receives the same audited 403 refusal as its other
  actions, and any pause-state flip bumps the global authorization revision
  (migration 0061), permanently fencing pre-pause receipts even after resume.
- Page label attach/detach is now an audited-atomic command
  (`page_commands.attach_page_label` / `detach_page_label`): the join write and
  its `page_labeled`/`page_unlabeled` event commit or roll back together, where
  they previously committed separately and a crash between them could label a
  page with no trail entry. REST and the browser call the same command; MCP
  reaches it through REST. This was the last Mentor write listed as
  command-migration debt.
- Fleet-board moves now submit the card's canonical issue ETag and fail closed
  when the board is stale, preserving the newer status and showing an explicit
  refreshed-board conflict notice in both HTMX and no-JS flows.
- Saved-filter assignee ids now reject invalid types and values outside SQLite's
  integer range before persistence; malformed JSON, non-object JSON, unknown keys,
  and invalid stored criteria fail closed instead of widening the query or raising
  an overflow while running it.
- Searching within a saved filter now preserves the filter's own title/body text
  constraint instead of replacing it with the ad-hoc query.
- Attachment publication now uses a private same-directory stage, file and
  directory fsync, and atomic replacement before one metadata-plus-audit commit.
  Audit, notification, run-binding, write, and commit failures roll back and
  attempt blob cleanup; a cleanup failure is surfaced alongside the primary error
  for reconciliation. Deletion commits metadata plus audit before observable
  post-commit unlink. Downloads open regular blobs through descriptor-anchored,
  no-follow paths, and hard page delete
  attempts each blob/link/search cleanup independently.

## 0.1.0a1 development milestone (untagged) — 2026-07-18

### Added

- AGPL-3.0-only licensing and package metadata.
- Contributor and security policies.
- A bounded peer-review guide and an explicit command-migration inventory.
- Transparent documentation of Athena's AI-assisted development process.
- A safe, loopback-only seeded demo command for a five-minute product tour.
- A pull-request template that records scope, verification, risks, and AI help.
- Atomic, audited application commands for the credential/privilege/content
  writes that previously split mutation from audit (or recorded nothing):
  dependency links, user role/agent-flag changes, user creation, API-token
  mint/revoke, webhook lifecycle, SSO identity link/unlink, automation-rule
  lifecycle, and issue/page comment create/edit/delete — each write and its
  activity event now commit or roll back together.

### Changed

- Reframed the README around the operator loop, a fast demo, architecture
  evidence, and honest alpha boundaries.
- Moved agent credential authorization into the command boundary as defense in
  depth; REST and web adapters remain early gates.
- Advanced package metadata to the `0.1.0a1` review-candidate line.

## 0.0.1 development milestone (untagged)

Initial local-alpha development line: Aegis issues, Mentor documentation,
cross-links, scoped agent access, MCP, activity/run lineage, portability,
webhooks, automation, OIDC, and operational tooling.
