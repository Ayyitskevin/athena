# Opus Performance & Adoption Guide — Athena

Produced from the 2026-08-12 external review (Fable) of `main` at `6235ec9`.
Method: four independent deep passes — architecture/code quality, security,
test engineering, product surface — plus a measured benchmark run against a
seeded 10k-issue / 100k-event database and a full-suite cold-clone run
(3,433 passed, 0 failed). Where a finding below carries a number, that number
was **measured in the review, not estimated**; re-measure before and after each
fix with the same shape (seeded 10k issues / 100k chained events).

> **Seed with `scripts/seed_benchmark.py`, and never with ANALYZE.** Nothing in
> the product runs `ANALYZE` or `PRAGMA optimize`, so no Athena database has a
> `sqlite_stat1` table and SQLite plans from heuristics rather than statistics.
> That is not a detail: seeding a benchmark with ANALYZE makes the F-0.1 feed
> read measure **0.4 ms where production measures 229 ms**, because with
> statistics SQLite walks the rowid index backwards and stops at LIMIT, and
> without them it resolves the visibility OR by MULTI-INDEX OR and sorts every
> survivor through a temp B-tree first. The F-0.1 fix below was nearly abandoned
> as a non-issue on the strength of a benchmark that ran ANALYZE. Measure in the
> state the product ships.

**Review grades: architecture 8/10, security 8.5/10, tests 9/10.** The quick
fixes from that review (CGNAT/site-local egress refusal, the 0075 verb-window
index, the `mcp<2` bound, the vendored-htmx digest pin, the AGENTS.md dialect
inventory) landed separately — this guide is the rest: the work that needs
design judgment, not just a patch.

The one-line thesis of this campaign: **the trail is the product, so the trail
must survive its own success — and the demo must show the differentiator.**

---

## Standing constraints (unchanged, non-negotiable)

- Never push `main`. Branch as `claude/<topic>` (or your fleet prefix), PR,
  gate, merge. One wave-item = one PR unless two items share a file for real.
- Every behavioral fix lands with the regression test that would have caught
  it; every performance fix lands with a plan-pinning or bounded-measurement
  test (the house pattern: `tests/test_activity_actor_index.py`,
  `tests/test_links.py`'s relative-timing bounds).
- NEVER lower a coverage floor. If coverage fails, the gap is missing tests.
- Do NOT create or push a git tag; RELEASE_READINESS.md still says HOLD and
  tagging is the human release owner's act (F-2.4 prepares it, a human does it).
- Migrations are forward-only; the next free slot when this guide was written
  is 0076.
- No JS build chain, one process, one SQLite file — VISION.md rule 4 survives
  every item below. If an item seems to need a second process, the item is
  wrong, not the rule.
- Visibility semantics are frozen: every fix to a gated read must leave the
  `tests/test_access_*` family green and add its own leak test if it touches
  composition. Hidden reads as missing, never 403.
- MCP and REST reach the same command behavior; fixes apply to both transports.
- Sections marked **[OPERATOR DECISION]** need Kevin's explicit choice before
  code is written. Everything else is buildable as specified.

---

## Wave F-0 — measured performance ceilings (highest leverage)

### F-0.1 The gated activity feed is O(n) per page — **DONE** (0076)

> Landed. The predicate is unchanged and now also available as its disjoint arms
> (`access.event_visibility_arms`); `core/activity._paged_feed_sql` asks each arm
> for its own bounded page and merges them, seeking
> `idx_activity_kind_id (target_kind, id)`. Measured at 100k events, no ANALYZE:
> 10-row page 233 ms → **0.25 ms**, 50-row page 229 ms → **0.71 ms**, `GET /events`
> backfill 226 ms → **1.17 ms**; admin and ungated reads unchanged. The review's
> diagnosis was exactly right, including "the LIMIT is inert" — a 10-row page cost
> what a 50-row page cost. Equivalence is pinned against a reference implementation
> of the old shape across the actor/filter matrix, plus a full cursor walk proving
> per-arm limits cannot drop or duplicate a row at a page boundary.
> Original finding follows.


`access.event_visibility_clause` (`core/access.py:439-510`) is an OR across
four target-kind arms; SQLite answers it with MULTI-INDEX OR over essentially
every activity row, evaluates the correlated subqueries per row, then sorts
through a temp B-tree before `LIMIT`. Measured: **non-admin
`list_activity(limit=50)` = 244 ms at 100k events vs 0.1 ms ungated**; a
`GET /events` backfill page = ~260 ms (a caught-up poll is fine at ~6.5 ms) —
so one agent draining 100k events costs ~9 minutes of DB time, times N agents.
Linear in trail size: ~2.5 s per page at 1M events.

Fix shape (pick after profiling, but these are the two known-good options):
restructure the read as a **per-arm UNION with per-arm ORDER BY id / LIMIT**
(each arm seeks its own index, the outer query merges four bounded lists), or
add an **id-window pre-filter** (walk the feed in bounded id ranges, applying
the visibility predicate inside each window, descending until the page fills).
The predicate itself is correct and stays: this is an access-pattern fix, not a
policy fix. Acceptance: same rows in the same order as today for a mixed
public/private fixture (write the equivalence test first, against the current
implementation); the `tests/test_access_content_leaks.py` and
`test_access_activity.py` families stay green; a plan or timing test pins the
new shape; measure before/after at 100k events in the PR description.
`GET /events` (`core/events_api.py`) and `/activity` both ride the fix.

### F-0.2 The web issue list and board hydrate everything — **DONE**

> Landed. Paging, sorting and counting moved into the data layer
> (`issues.count_issues`, `issues.statuses_in_use`, and a `sort`/`order` pair on
> `list_issues` from a closed vocabulary); the board caps at 500 cards with a
> "Showing N of M" line. Measured at 10k issues: unbounded fetch 99 ms → **7.0 ms**
> for a page (1.6 ms count + 5.4 ms sorted page), status dropdown 44 ms → **2.9 ms**.
> The priority-sort drift is fixed at the root: the rank CASE now lives once in
> `issues.PRIORITY_RANK_SQL` and the grammar imports it, with a test pinning that
> the two surfaces produce one identical ordering.
>
> **Follow-up left open:** the default `created_at` sort has no supporting index,
> so the page still sorts the matched set before slicing it — that is the residual
> 5.4 ms. An index would need `(created_at, id)` with the tie-break running in the
> sort's own direction, which changes the observable order of same-timestamp rows;
> worth doing, worth deciding deliberately.


`web/router.py:494-526` fetches every matching issue (no SQL limit), attaches
labels to all of them, sorts in Python, slices a page — then `_statuses_in_use`
(`web/router.py:153-173`) runs a second full unbounded `list_issues` just to
build a dropdown. Measured: **109 ms per page view at 10k issues vs 0.3 ms for
the REST endpoint**, which already passes `limit`/`offset` correctly
(`aegis/api.py:557-632`). The board (`web/boards.py:104-118`) is the same
disease plus a per-card ETag hash.

Fix: push paging and sorting into SQL through the same data-access calls REST
uses (`aegis/issues.py` already supports it); replace `_statuses_in_use`'s
unbounded fetch with a bounded `SELECT DISTINCT status` shaped query owned by
the data module (respecting visibility); bound the board per swimlane with an
explicit "showing N of M" line (the house pattern for every other capped
surface). While in there, fix the **priority-sort drift**: the web list sorts
priority alphabetically (high, low, medium, urgent) while the query grammar
sorts by rank (`aegis/issue_query.py:43-58`) — the grammar's CASE rank is the
correct one, reuse it. Acceptance: pinned equal ordering between
`sort:priority-desc` in the grammar and the web list's priority sort; a page
of 50 renders with bounded queries at 10k issues (plan test or timing bound).

### F-0.3 The admin nav rollup taxes every request — **DONE** (no cache, deliberately)

> Landed as a measurement and a comment, not a cache. The note below said to
> re-measure before building one, and the measurement said don't: on a
> 10k-issue / 100k-event no-ANALYZE fixture `build_attention` costs **0.11 ms
> empty, 0.50 ms at 5 agents, 1.21 ms at 25, 3.81 ms at 100** — it scales with
> **fleet size, not trail size**, which is exactly what the 0075 index bought.
> The guide's 12.9 ms was pre-0075 and is no longer reachable. At 100 agents the
> 4.33 ms breaks down as active-work projection 1.73 ms, workers 1.29 ms,
> approvals 0.48 ms; at the scale the product is actually for (a one-person
> fleet, single digits of agents) the whole rollup is half a millisecond.
>
> A TTL cache would trade a live attention badge for that half millisecond, and
> staleness in the number whose entire job is "look at this now" is the worse
> side of that trade — the badge exists to beat human reaction time, so a cache
> tuned to stay under it saves nothing worth having. So the deliverable is the
> honest comment: `main.py`'s stale `~0.1 ms measured` is replaced with the real
> numbers, the fixture they came from, and an explicit "there is deliberately NO
> cache here" with the reasoning, so the next reader re-opens the question with
> data instead of re-deriving it. A test pins the property that made this
> answer possible — the rollup's activity inputs must seek `idx_activity_verb_window`
> and never `SCAN activity` — because if that regresses the cost starts growing
> with the log forever and the trade flips. The measurement harness that F-0.4
> shares is in the same PR. Original finding follows.

`attach_session_user` builds the full fleet-attention rollup on every
authenticated admin request (`main.py:1502-1509`): active-work projection +
rules + webhooks + approvals + workers + two activity scans. Measured:
**12.9 ms per admin request at 100k events before the 0075 index**; the code
comment "~0.1 ms measured" predates the data and must be corrected either way.
The 0075 index cut the two activity scans; the projection work remains.

Fix: give the nav badge a short-TTL in-process cache (seconds, not minutes —
it is an attention count, staleness must stay under the human reaction time it
serves), invalidated on the writes that change it or simply expired by time;
document the staleness bound in the template's title text. This is process-local
state like the rate limiters, which are the precedent for "in-process is
acceptable and documented" (`core/rate_limits.py:1-11`). Update the stale
comment with the measured number and its fixture. Acceptance: an admin page
render performs zero rollup queries within the TTL (assert by query counting,
the house has `conn.set_trace_callback` precedents); the badge still changes
within one TTL of a new approval (test with a forced-expiry hook, no sleeps).

### F-0.4 Static assets are never browser-cached for signed-in users — **DONE**

> Landed, and it turned out to be the expensive half of F-0.3 rather than a
> separate bandwidth item. The premise held (`/static/styles.css` really did
> come back `private, no-store` with a cookie), but the finding underneath was
> that the session middleware ran for those fetches too — so for a signed-in
> **admin**, every stylesheet, htmx bundle and confirm-script request opened
> SQLite, resolved the session, and built the entire fleet-attention rollup.
> Verified by patching `build_attention`: one rollup per static fetch, and the
> no-store policy guaranteed the browser asked again on the very next page load.
> Two lines fix both: `/static` returns before session resolution and before the
> private-cache policy. Assets now serve `public, max-age=31536000, immutable`,
> busted by a startup content fingerprint (sha256 over every static file's path
> **and** bytes, truncated to 12 chars) appended as `?v=` in the templates — no
> build chain, per VISION rule 4. The path is in the hash on purpose so a rename
> busts too. Tests pin: public policy with no `Set-Cookie`, pages still
> `private, no-store`, **zero** rollups per static fetch and still exactly one
> per page render, and a changed byte or a renamed file changing the
> fingerprint — that last one is what keeps a year-long `immutable` from
> becoming a trap. Original finding follows.

`_apply_private_cache_policy` (`main.py:1084-1125`) marks every cookie-carrying
response `private, no-store`, including `/static/*`, so every page load
re-fetches CSS/JS/htmx and each fetch runs the session middleware. Fix: static
mounts get `Cache-Control: public, max-age=...` with a content-hash or
mtime-based cache-buster in the template URL (no build chain — a query-string
hash computed at startup is enough), and the session middleware skips `/static`
entirely. Acceptance: `/static/styles.css` response carries the public policy
and no `Set-Cookie`; authenticated page responses keep `private, no-store`;
the cache-buster changes when the file changes (test by editing a temp copy).

---

## Wave F-1 — security hardening before any non-loopback exposure

(From the security pass; the SSRF range fix already landed with the review.)

### F-1.1 Per-account brute-force protection — **DONE**

> Landed. A second fixed-window limiter keyed by the SUBMITTED EMAIL rather than
> the resolved account — that keying is the finding worth keeping: throttling by
> user id would have made a real address return 429 while an unknown one returned
> 401, a free membership oracle undoing the opacity the dummy PBKDF2 verify and
> the background-task audit write already provide. A test asserts the two status
> sequences are identical rather than merely both bounded. New `login_throttled`
> verb in the closed SECURITY_VERBS set; the DoS trade is written down in
> SECURITY.md. Original finding follows.


Login throttling is per-IP only (`auth.py:176-189`) — distributed credential
stuffing against one account is unbounded per-account. Add a per-account
counter with exponential backoff or a bounded lockout window, audited like
every other refusal (`security_events`), zero-filled in the counts, and immune
to the timing-oracle regression (the dummy-verify path must burn the same cost
whether the account is locked, unknown, or real). Lockout state is operational
state, not content — but a lockout *event* is a security event and belongs on
the trail. Careful: the lockout must not become a denial-of-service lever
against a known email; cap the lockout duration and document the trade in
SECURITY.md.

### F-1.2 **[OPERATOR DECISION]** A supported TLS shape

`athena-serve` refuses `ATHENA_COOKIE_SECURE=1` today (`deployment.py:495-498`)
because the supported contract is direct HTTP on loopback/tailnet, so anything
beyond a WireGuard/Tailscale transport carries session cookies in cleartext.
The decision Kevin has to make: either (a) bless one reverse-proxy recipe
(Caddy or nginx, exact config shipped in OPERATIONS.md) and teach the
preflight to accept `COOKIE_SECURE=1` + proxy-terminated HTTPS in a named
mode with its own Host/authority contract, or (b) declare tailnet-only
transport encryption permanent and say so in SECURITY.md in one loud sentence.
Do not build (a) speculatively — it widens the deployment claim
RELEASE_READINESS.md holds the line on. Whichever way the decision goes, the
launcher's refusal message should point at the decision's documentation.

### F-1.3 A global default-private visibility switch — **DONE**

> Landed. `ATHENA_ANONYMOUS_READS=0` is enforced at two choke points, not route
> by route: `identity.optional_actor` (all 55 optional-identity REST reads) and
> the session middleware (browsers). The enumeration the finding asked for is a
> test rather than a list — it walks the route table and asserts that exactly one
> route uses the ungated resolver, which is the first-user bootstrap. Two
> deliberate openings, because a switch that locks the operator out of their own
> login page or empty database is an outage: the sign-in path, and that bootstrap.
> `ATHENA_DEFAULT_VISIBILITY=private` is the ergonomics half. Original finding
> follows.


Projects and spaces are public-by-default (`core/access.py:3-5`); an
accidental tunnel or Funnel exposes every public container with zero
credentials. Add `ATHENA_DEFAULT_VISIBILITY=private` (default remains `public`
for compatibility): new containers are born private, and — the fail-closed
half — a config flag `ATHENA_ANONYMOUS_READS=0` that turns off unauthenticated
reads entirely regardless of per-container visibility. The second flag is the
one that makes exposure fail closed; the first is ergonomics. Acceptance: with
anonymous reads off, every read surface (web, REST, search, feeds, exports)
requires an authenticated actor — enumerate the optional-identity dependencies
(`core/identity.py`) rather than testing routes one by one, and add the leak
test to the `test_access_*` family.

### F-1.4 Session lifecycle bounds

14-day sessions with no idle timeout and no per-user session cap. Add a
rolling idle timeout (config, default generous — this is a solo tool) and a
bounded per-user session count (oldest evicted, audited). Password change
already revokes other sessions; this extends the same hygiene to quiet decay.

### F-1.5 Drop `style-src 'unsafe-inline'` — **DONE**, and it was ~3x this estimate

> Landed, but re-scope this one before trusting the estimate on anything like it.
> The finding below says the exception "exists for the rollup width styles". The
> real surface was **34 inline `style=` attributes across 15 templates and two
> Python emitters**.
>
> The binding constraint was not volume, it was a CSP detail worth knowing: **a
> nonce does not license inline `style=` ATTRIBUTES**, only `<style>` elements and
> scripts. So there was no way to permit the hard cases and convert the easy
> ones — every attribute had to go, which is what pulled in the whole template
> tree. Three mechanisms, because they are three different problems: static
> declarations became utility classes; bounded numbers (bar percentages, tree
> depth) became stepped classes — which is also what keeps `web/render.py`
> working, since it builds embed HTML outside any template and so has no nonce to
> reach for; and arbitrary values (a user's `#RRGGBB` label hex, a computed SVG
> width) became per-response nonce'd `<style>` elements.
>
> Two things the gate taught along the way: `graph-wrap`/`timeline-wrap` were on
> check_template_styles' ALLOWLIST *because* their overflow lived inline, so
> removing it made those entries stale and the build refused until they had real
> rules. And `web/html_export.py` keeps its inline style deliberately — that
> export is downloaded as an attachment and opened from `file://`, where no CSP of
> ours applies. Original finding follows.


The CSP exception (`main.py:97`) exists for the rollup width styles. Replace
inline `style=` widths with a small set of stepped width classes (the design
system already tolerates a bounded scale) or a nonce. Acceptance: CSP has no
`unsafe-inline` anywhere; the rollup bars still render (template contract
covers the classes).

---

## Wave F-2 — the demo must show the differentiator (adoption)

### F-2.1 Seed supervision state in the demo — **DONE**

> Landed. `demo.py` seeds all five states through the real commands as Sol,
> holding Sol's own bearer token (resolved via `tokens.resolve_token`, the same
> function the HTTP layer uses — a hand-built actor dict would have skipped the
> credential checks the worker registry exists to enforce). The fleet-attention
> card shows three non-zero counts; `/admin/run-controls` and the approvals queue
> each show one live row; the CLI now prints the two things waiting on the
> reviewer. One ordering lesson worth keeping: the budget must be set BEFORE the
> agent's writes, because a budget only meters what happens after it exists —
> setting it last left the cockpit showing a ceiling with zero consumption, a
> control the reviewer never sees bite. Original finding follows.


`demo.py` seeds tracker/wiki basics but zero supervision state, so every
Intervene/Trust surface renders its empty state on the five-minute tour — the
product reads as a small tracker+wiki and the thesis is invisible. Seed, in
the same fail-closed style: one **pending approval** (an `issue.close` ask
from Sol, waiting), one **open run control** (a steer against Sol's live run,
unanswered), one **active claim/lease** with a check-in, one **budget** set
low enough that the cockpit shows meaningful usage, and one **worker
heartbeat** in the registry. Keep `demo.py:222`'s philosophy ("a demo that
oversells is worse than one that is thin") by seeding only states that are
*true* — a pending approval seeded as pending is true. Acceptance: the
dashboard fleet-attention card shows non-zero counts naming their surfaces;
`/admin/run-controls` and the approvals queue each show one live row; the
README's five-minute tour is updated to walk through answering both.

### F-2.2 The desk-loop recipe — **DONE**

> Landed as `docs/RUNTIME_RECIPE.md` + `examples/desk_loop.md`, linked from the
> README tour and QUICKSTART step 5. `tests/test_runtime_recipe.py` pins it
> against the build: cited MCP tools must exist in `TOOL_SCOPES`, the config
> blocks must parse and match `claude_mcp_config`, curled endpoints must be
> registered routes, and relative links must resolve. That test earned its keep
> immediately — the first draft cited `/agents/onboard` (the real route is
> `POST /users/onboard_agent`), an `athena-mcp --print-config` flag that does not
> exist, and `docs/AGENT_ONBOARDING.md`, which does not exist either. Original
> finding follows.


Athena ships no agent runtime, and the only worked executor is the
self-authored reference. Write the one document + script pair that closes the
loop for a real user: `docs/RUNTIME_RECIPE.md` (or extend QUICKSTART step 5)
plus `examples/desk_loop.md` — a copy-paste Claude Code / MCP-client prompt
and config that: connects with the printed scoped token, calls `my_desk()`,
claims one delegated issue, works it, records a learning, completes the claim,
and answers a pending run control if one arrives. The field guide
(`src/athena/field_guide/`) already teaches the vocabulary — this artifact is
the *operator-side* mirror: exactly what to paste, where, and what they will
see on the dashboard while it runs. Acceptance: a fresh reader with Claude
Code installed reaches "an agent completed a delegated issue and the trail
shows it" without reading any other doc; the recipe is exercised by a test to
the extent it can be (the MCP config it prints parses; the tool names it cites
exist — pin against `mcp/server.py`'s registry so drift fails the build).

### F-2.3 Docker image + compose file — **DONE** (build/test landed; publishing still open)

> Landed exactly as scoped: the image and compose file are in the tree, CI builds
> and boots them, and nothing is pushed anywhere. The publishing question is
> written up in [`DECISIONS_PENDING.md`](DECISIONS_PENDING.md) with a
> recommendation rather than left as a line item.
>
> The finding worth carrying: **a container cannot usefully publish a port.**
> `local` mode permits loopback only and `tailnet` only Tailscale's ranges, so a
> `ports:` mapping points at an address the process is not on and the failure looks
> like a hang. compose.yaml therefore ships no port mapping, and docs/DOCKER.md
> names the shapes that do work. CI checks the properties that would make the image
> a liability rather than just that it starts: non-root, a fresh volume still
> refused without an explicit `--bootstrap`, its own HEALTHCHECK reporting healthy,
> the database owned by the non-root user, and a restart on an existing database.
> Original finding follows.

A `Dockerfile` (python:3.12-slim, non-root user, volume for the SQLite file
and attachments, `athena-serve` entrypoint, loopback-by-default with the
tailnet-bind envs documented) and a compose file that mounts a named volume.
This does not widen the security claim — the container publishes no port
unless the user maps one, and the docs say which shapes remain unsupported.
Whether to *publish* the image (GHCR) is Kevin's call; building and testing it
in CI (boot the container, hit `/healthz`, run the two-lifecycle smoke) is not
speculative and should land regardless.

### F-2.4 Prepare the release the checklist already defines — **DONE** (nothing tagged)

> Landed: `RELEASING.md`, `.github/workflows/publish.yml`, the version-bump
> discipline in CONTRIBUTING.md, and an executable version-agreement test. No tag,
> no publish — and the workflow cannot run until the `pypi`/`testpypi` environments
> and PyPI trusted publishers exist, which is the right state for a project that
> has never released.
>
> Two things went beyond the item as written. The guard job **verifies** the
> checklist's "hosted CI green at the exact head" rather than trusting it — a tag
> can be applied to a commit whose run went red, and nothing previously stopped
> that. And build+publish are one job on purpose: an artifact handoff would need a
> second third-party action pinned and would open a window in which the published
> artifact is not provably the verified one. Original finding follows.

RELEASE_READINESS.md's promotion checklist is two boxes from done: hosted CI
green at the exact head (re-check at tag time) and the human tag. Prepare
everything mechanical around it: a PyPI publish workflow (trusted publishing,
dry-run against TestPyPI, gated on the human tag event), the version-bump
discipline note, and a RELEASING.md that names the exact order. Do not tag.

### F-2.5 Liveness for the cockpit — htmx polling, not SSE — **DONE**

> Landed on exactly the three surfaces named below, with one design decision worth
> keeping: **the poll is the page**. Each panel's `hx-get` re-enters the route that
> rendered it, distinguished by an explicit `?panel=<name>` marker rather than the
> `HX-Request` header — the header would mean "any future htmx interaction on the
> dashboard returns the attention card", a trap waiting for the next feature. The
> consequence is that no partial has its own endpoint, so no partial can be given a
> weaker gate than the page it mirrors, and a filtered view survives its own
> refresh instead of silently widening to the whole fleet.
>
> The acceptance criterion "a paused/anonymous session polls nothing" needed no new
> code and got tests instead: the polling markup lives *inside* the admin-gated
> panel, so a viewer renders a page with no poll in it, and the session middleware
> already treats a paused user as signed out. Asking for a panel directly is
> answered the same way the page answers — an empty body, not a card.
>
> Two things surfaced while building it. `fleet_work.parse_query_pairs` is strict
> (an unknown key is a 400, deliberately, so a request can never be silently
> widened), so `/admin/agents/runs` strips the panel marker before parsing rather
> than loosening that rule. And the swap must be `outerHTML` on the panel's root:
> anything else polls exactly once and then stops, which looks live for one
> interval and is stale forever after — pinned by a test, since it fails silently.
> `ATHENA_LIVE_REFRESH_SECONDS` defaults to 10, accepts 0 to disable, and is held
> to 5–3600 otherwise. Original finding follows.

VISION's Observe promise ("what is each agent doing right now") meets a
refresh-only UI. SSE fights the one-process model and htmx already ships;
the Athena-shaped answer is `hx-trigger="every Ns"` partial refresh on
exactly three surfaces: the fleet-attention card, Mission Control's active-work
table, and `/admin/run-controls`' open-request list. Bound the interval
(config, default 10-15s) and render a "last updated HH:MM:SS" line so staleness
is visible instead of implied away. This item used to say "reuse the F-0.3
cache"; there is no F-0.3 cache — the rollup measured at ~0.5 ms for a real
fleet, so a 10s poll costs ~0.05 ms/s per admin and needs nothing. Re-measure
if the poll ever gets faster than the interval bound below. Acceptance: template contracts still pass
(the partials get routes); the poll respects the same visibility gates; a
paused/anonymous session polls nothing.

### F-2.6 The logged-out landing page must state the thesis

`home.html` says "a self-hosted workspace for your work and your knowledge" —
no agents anywhere. One screen: the one-liner ("Mission control for a
one-person AI fleet"), the operator loop in five words each, and the two
facts that differentiate (agents are audited first-class actors; everything
is attributable and reversible on your own machine). No feature list — link
the README for that.

---

## Wave F-3 — durability and hygiene

### F-3.1 Property-based tests over the hand-rolled parsers — **DONE**

> Landed, and it found a real one on the first run. Every bracket grammar captured
> `\d+` unbounded; Python raises `ValueError` from `int(str)` past 4300 digits; so
> `[[issue:<4301 digits>]]` in an issue body crashed `extract_refs` inside
> `sync_links` — **on every issue and page write**. Same shape in `[[KEY-N]]`,
> `[[user:N]]` (two copies, see below) and the forge linkifier, where the text
> arrives from a webhook rather than a person. Five sites now share one
> `links.ID_DIGITS` bound of 19 digits (a SQLite rowid's width), so an over-long run
> simply stops matching and stays literal text. Verified the honest way: the
> property fails against the old grammar and passes against the new.
>
> Two notes for whoever writes the next property test. The generator has to reach
> the grammar — plain `st.text()` almost never emits a bracket, so `BODY` mixes
> `[`, `]`, `:` and the literal words `issue`/`page`/`user` into its alphabet, and
> without that these tests would have proved only that non-matching input does not
> match. And the settings live once in `conftest.py` (`derandomize=True`,
> `database=None`): a property test with fresh random inputs each run can fail on a
> commit that changed nothing, which trains everyone to re-run red builds.
>
> One thing found and deferred at the time: `parse('"a:b"')` raised
> `unknown search field 'a'` — the tokenizer stripped quotes before the colon check,
> so quoting did not protect a phrase containing one. Deferred as a semantics
> question; on a closer look it was a plain contradiction of QUERY.md, which
> documents a quoted phrase as a substring search, so it was a bug and **is now
> fixed**. The tokenizer records where the separator was instead of rediscovering it
> after the quotes are gone. `assignee:"Ada Lovelace"` is unaffected — that colon is
> outside the quotes.
>
> On the dependency: the freeze was regenerated in a clean venv as documented, but
> with `-c constraints/ci-py312.txt` so pip resolved only what was new. The
> unconstrained form in the README swept in ~20 unrelated upgrades (fastapi
> 0.139→0.141, starlette 1.3.1→1.6.0, ruff 0.15→0.16) — a framework upgrade riding
> inside a testing PR, where a later bisect would never look. The README now
> documents both forms and when each is right. **That full refresh landed
> separately and is now done**, as its own commit: 21 packages, gate green,
> `starlette._utils.get_route_path` (a private import) verified to survive the
> three-minor jump. Net diff: two pins (`hypothesis`,
> `sortedcontainers`; `attrs` was already there), counts updated in
> `tests/test_supply_chain.py` and the two prose files that state them. Original
> finding follows.

The suite is curated-case excellent but has no generative testing, and the
codebase carries several hand-rolled parsers over untrusted input: the
`[[ref]]` link grammar (`core/links.py`), the work-query grammar
(`aegis/issue_query.py`), the import-bundle readers (`core/portability.py`,
`core/source_import.py`), and the forge linkifier. Add `hypothesis` to the
dev extras (this touches `constraints/ci-py312.txt` — regenerate the freeze
the documented way, don't hand-edit) and write property tests for at minimum:
parse-never-crashes (arbitrary text), parse/serialize round-trips where a
serializer exists, and grammar-error-always-names-the-atom (QUERY.md's
promise). Keep them deterministic in CI (`derandomize=true`, explicit seeds).

### F-3.2 One connection per request — **DONE**

> Landed, and the premise measured much larger than this item claims. The item
> blames "re-running three PRAGMAs"; the PRAGMAs are free (~0.005 ms each). What
> costs is the **first statement that touches the database file: ~2.2 ms to attach,
> per connection.** `sqlite3.connect` itself is lazy (0.03 ms) and `SELECT 1` never
> reaches the file, which is why this hides from casual profiling. Holding another
> connection open does not help — it is genuinely per-connection — while reusing an
> already-open one costs 0.004 ms, 500× less. So the only fix is to open fewer, and
> the value is far above what "fewer PRAGMAs" suggests.
>
> Counted, not assumed: a browser page and a plain REST call opened **2**, an
> idempotent write opened **5** (session, identity, reserve, publish, route). All
> are 1 now, and `/static` is still **0** — the holder opens lazily, so F-0.4's skip
> survives. Measured end to end: dashboard **14.97 → 9.81 ms**, `GET /issues`
> **15.22 → 10.20 ms**, idempotent `POST /issues` **35.32 → 19.19 ms**.
>
> Two things a reader should know before touching this. **The route dependency
> cannot own the connection's lifetime** — the idempotency publish and the
> exception handlers run *after* `get_conn` has exited, which is why an outer
> middleware creates and closes the holder instead. And **the invariant that makes
> sharing safe is transaction cleanliness**: `db.transaction` picks a real
> transaction or a savepoint by reading `conn.in_transaction`, so a layer returning
> the connection mid-transaction would turn the next writer's commit into a
> savepoint that is released without committing — a lost write, no error, no log.
> Every handoff verifies it, and a test proves the check fires. The 161
> Barrier-coordinated race tests pass unchanged; they use their own connections per
> thread, which is a different axis from this one. Original finding follows.

Each authenticated request opens 2-4 SQLite connections (session middleware,
route dependency, idempotency, handlers), each re-running three PRAGMAs, and
middleware and route see different snapshots. Thread one connection through
request state (FastAPI dependency overrides make this mechanical), keeping
the command layer's transaction ownership untouched. Acceptance: a
query-count/connection-count test pins one connection per request; the
Barrier-coordinated race tests stay green.

### F-3.3 **[OPERATOR DECISION]** Recovery portability vs. documented Linux-only

`core/recovery.py`'s `renameat2(RENAME_NOREPLACE)` via ctypes raises
`ENOTSUP` off-Linux, so backup/restore — the feature a self-hoster most needs
to be boring — fails on macOS/BSD. Either add the documented fallback
(`os.link` + unlink dance achieves no-replace semantics portably) or declare
Linux-only support in OPERATIONS.md and make `athena-doctor` say so on other
platforms. The decision is scope, not code; the fallback is a day of work if
wanted.

### F-3.4 Prune or promote — the standing review — **DONE** (first ledger drafted)

> Landed as the item describes: a process, with the first ledger drafted from
> evidence and every verdict left unset for Kevin. `docs/PRUNE_LEDGER.md` plus
> `scripts/prune_evidence.py`, which regenerates the evidence column from a real
> database read-only.
>
> Three findings worth keeping, all of them about how a ledger like this LIES.
> Guessed verb names reported zero events for agent supervision against a database
> that plainly exercises it — a typo arguing to cut the fleet loop; verbs are now
> pinned against the code by an AST-based test, because
> `verb="lease_renewed" if renewed else "claimed"` defeats a regex. Forge inbound
> lands as imported history BY DESIGN and the default evidence query excludes
> imported rows, so it read as never-used regardless of truth — the one candidate
> the tooling can measure was the one it measured wrong. And migration-seeded
> singleton tables can never read zero, so they measure nothing and are excluded.
>
> The honest limitation, stated in the ledger rather than buried: a pure read
> surface leaves no trace, so `n/a` is reported instead of `0` — and **two of the
> three candidates this item names are in that category**. The evidence is worst
> exactly where the question is sharpest, which is a reason to judge those two on
> operator habit rather than to wait for data that will never arrive.
> Original finding follows.

VISION.md says "when a proposed feature doesn't serve the picture, cut it or
reshape it"; nothing visible has ever been cut, and the maintenance surface
(355 routes, ~100 MCP tools, twin modules, three transports) is the project's
biggest long-term risk. Institutionalize the pressure: a quarterly
`docs/PRUNE_LEDGER.md` review naming, for each subsystem, its last real use
in the dogfood deployment and a keep/park/cut verdict. First candidates to
*evaluate* (not pre-judged): the knowledge-graph ego view, forge inbound, and
the answerability ledger's web surface. Parking means: routes return 410 with
a pointer, code moves under an `attic/` marker, tests stay. This item is a
process, not a PR — Opus drafts the first ledger from usage evidence, Kevin
decides.

---

## What this campaign does NOT do

No multi-tenant anything. No CRDTs or real-time co-editing. No JS build chain
(F-2.5 is htmx attributes, not a framework). No new subsystems — every item
above deepens or hardens something that already exists. No tag, no publish
without the human release owner's explicit act. And no lowering of any gate to
make any of it easier: the gates are why the codebase is trustworthy enough to
hand to a fleet in the first place.

## Suggested order

~~F-0.1 → F-0.2~~ (done) → ~~F-2.1 + F-2.2~~ (done) →
~~F-1.1/F-1.3/F-1.5~~ (done) → ~~F-0.3/F-0.4~~ (done) →
~~F-2.5~~ (done) → ~~F-3.1/F-3.2~~ (done) → ~~F-2.3/F-2.4~~ (done) → the
three [OPERATOR DECISION] items whenever Kevin decides (F-3.4's first ledger is
drafted and waiting on verdicts) — each decision now has a brief in
[`DECISIONS_PENDING.md`](DECISIONS_PENDING.md), so deciding is a read rather than a
research project. Waves are
parallelizable across agents except where noted; one item = one PR = one green
gate.

The note left here for F-0.3 said to re-measure with `scripts/seed_benchmark.py`
before building the cache, because a cache was the right answer only if the
measurement still said so. It didn't, and the item shipped without one. Keep
that habit for the rest of the wave: every remaining performance item in this
guide carries a number that was measured against some fixture at some commit,
and three of them have already moved under the fixes that landed since.
