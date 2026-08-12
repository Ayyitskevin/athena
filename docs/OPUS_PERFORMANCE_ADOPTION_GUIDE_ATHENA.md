# Opus Performance & Adoption Guide — Athena

Produced from the 2026-08-12 external review (Fable) of `main` at `6235ec9`.
Method: four independent deep passes — architecture/code quality, security,
test engineering, product surface — plus a measured benchmark run against a
seeded 10k-issue / 100k-event database and a full-suite cold-clone run
(3,433 passed, 0 failed). Where a finding below carries a number, that number
was **measured in the review, not estimated**; re-measure before and after each
fix with the same shape (seeded 10k issues / 100k chained events).

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

### F-0.1 The gated activity feed is O(n) per page

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

### F-0.2 The web issue list and board hydrate everything

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

### F-0.3 The admin nav rollup taxes every request

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

### F-0.4 Static assets are never browser-cached for signed-in users

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

### F-1.1 Per-account brute-force protection

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

### F-1.3 A global default-private visibility switch

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

### F-1.5 Drop `style-src 'unsafe-inline'`

The CSP exception (`main.py:97`) exists for the rollup width styles. Replace
inline `style=` widths with a small set of stepped width classes (the design
system already tolerates a bounded scale) or a nonce. Acceptance: CSP has no
`unsafe-inline` anywhere; the rollup bars still render (template contract
covers the classes).

---

## Wave F-2 — the demo must show the differentiator (adoption)

### F-2.1 Seed supervision state in the demo

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

### F-2.2 The desk-loop recipe — the missing 20% that proves the product

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

### F-2.3 Docker image + compose file **[OPERATOR DECISION on publishing]**

A `Dockerfile` (python:3.12-slim, non-root user, volume for the SQLite file
and attachments, `athena-serve` entrypoint, loopback-by-default with the
tailnet-bind envs documented) and a compose file that mounts a named volume.
This does not widen the security claim — the container publishes no port
unless the user maps one, and the docs say which shapes remain unsupported.
Whether to *publish* the image (GHCR) is Kevin's call; building and testing it
in CI (boot the container, hit `/healthz`, run the two-lifecycle smoke) is not
speculative and should land regardless.

### F-2.4 Prepare the release the checklist already defines

RELEASE_READINESS.md's promotion checklist is two boxes from done: hosted CI
green at the exact head (re-check at tag time) and the human tag. Prepare
everything mechanical around it: a PyPI publish workflow (trusted publishing,
dry-run against TestPyPI, gated on the human tag event), the version-bump
discipline note, and a RELEASING.md that names the exact order. Do not tag.

### F-2.5 Liveness for the cockpit — htmx polling, not SSE

VISION's Observe promise ("what is each agent doing right now") meets a
refresh-only UI. SSE fights the one-process model and htmx already ships;
the Athena-shaped answer is `hx-trigger="every Ns"` partial refresh on
exactly three surfaces: the fleet-attention card, Mission Control's active-work
table, and `/admin/run-controls`' open-request list. Bound the interval
(config, default 10-15s), reuse the F-0.3 cache so polling admins don't
re-tax the rollup, and render a "last updated HH:MM:SS" line so staleness is
visible instead of implied away. Acceptance: template contracts still pass
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

### F-3.1 Property-based tests over the hand-rolled parsers

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

### F-3.2 One connection per request

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

### F-3.4 Prune or promote — the standing review

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

F-0.1 → F-0.2 (the measured ceilings, highest leverage) → F-2.1 + F-2.2 (the
demo argument, independent of F-0) → F-1.1/F-1.3/F-1.5 (hardening with no
design dependency) → F-0.3/F-0.4 → F-2.5 (needs F-0.3's cache) → F-3.1/F-3.2
→ F-2.3/F-2.4 (release prep) → the three [OPERATOR DECISION] items whenever
Kevin decides. Waves are parallelizable across agents except where noted; one
item = one PR = one green gate.
