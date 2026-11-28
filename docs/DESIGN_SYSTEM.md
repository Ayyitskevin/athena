# Athena — design system ("Phosphor Ink")

The visual and IA system introduced by the `claude/phosphor-ink-design-system`
migration. This is operating guidance for anyone touching `styles.css` or a
template — read it before adding a class or a hex value. The full token table
(every value, both themes, contrast floors) lives in
[`docs/TOKENS.md`](TOKENS.md).

## The three rules that matter most

```css
/* every status, priority, state, badge and pill in the app */
.chip[data-tone="success|warning|danger|info|agent|neutral|accent"]

/* every boxed surface in the app */
.panel

/* the operator's colour, split in two */
--accent       /* FILL only, always with --accent-ink  */
--accent-text  /* FOREGROUND: text, icons, focus, charts */
```

`--accent` is a bright phosphor lime. In light mode it is **1.65:1 against
white** — illegible. Writing `color: var(--accent)` is always a bug; use
`--accent-text`.

## What the accent means

Phosphor lime appears in exactly three semantic places: the fleet-attention
figure, the Intervene nav badge, and the `in review` status. All three mean the
same thing — **an agent has stopped and is waiting on the human.** Nothing an
agent can do on its own is ever lime. Do not spend the accent on decoration,
and do not spend it on a button: see "The default button".

## The default button

`.button` with no modifier is the page's primary action, and it is **ink**:
the fill is `--text` and the label is `--surface` (14:1 or better in both
themes, with no new token). It is never `--accent`. Until U15 it was, so a lime
"Create Project" said the same thing as the Intervene badge, and the one colour
that means "waiting on you" was on every form in the app. A primary action is
the operator's own act, and "human actors get no colour" (What violet means),
so it takes the theme's foreground instead of a hue. The variants keep their
meanings: `.secondary` (a quiet `--surface-2` fill), `.danger` (`--danger`
tint, destructive), `.ghost` (no fill). `tests/test_cockpit_polish.py` fails if
the default button or its hover names an accent token, and if the badge and the
accent chip stop using it.

## Database colours are never text colours

Labels carry an operator-chosen hex from SQLite. One value, two themes — so the
hex is a **background tint only**, set as `style="--label-color: {{ l.color }}"`.
Text is `color-mix(in oklab, var(--label-color) 32%, var(--text))`. Any future
user-chosen colour (project keys, custom statuses) must follow the same rule —
see `TOKENS.md`'s "recurring trap" section for the measured contrast math.

## Never transition a themed property

`transition: color …` / `background` / `border-color` on anything whose value
comes from a token is a **bug**, not a preference. A transitioned property does
not re-resolve when `data-theme` flips on `<html>` — it freezes at the old
theme's computed value. Because `styles.css` also honours
`prefers-color-scheme`, an OS day/night change re-themes the page with **no
reload**, and this app is left open overnight. Transition `transform`,
`opacity`, `box-shadow` and `filter` only. Instant colour change on hover is
correct and reads fine.

## What violet means

`--agent` (`#6D28D9` light / `#A78BFA` dark) marks anything a machine authored:
run ids, agent badges, lineage rails, agent avatars, agent-authored trail rows.
Human actors get no colour.

## Typography is semantic

- **Public Sans** — anything a human wrote.
- **Azeret Mono** — anything Athena recorded: issue keys, run ids, tokens,
  hashes, counts, timestamps, status chips, chain sequence numbers.

If you would copy-paste it, it is monospaced.

## Layout

The old `--max-width: 720px` applied a prose measure to kanban boards. Gone.

| Region | Width |
|---|---|
| App bar | full, translucent, sticky |
| Context rail | `--rail: 232px`, collapsible via cookie |
| Work region | full width, with the page gutter: `.work` pads itself `--sp-6` (`--sp-5 --sp-4` on a phone). Only the Mentor document layout (`.doc-layout`) is full-bleed and pads its own columns |
| Prose | `--measure: 76ch`, applied **locally** inside the full-width shell |

### A long name never widens the page

Anything a person or an agent names (an issue title, a project, a space, a
page, an agent, a user, a filter, a label) can be 120 characters of one word.
No page scrolls sideways because of one, at 390 px or at 1280 px (U15 measured
every page an admin reaches, with such names seeded, in Chromium). The rules
that hold it, each in `styles.css` and pinned by `tests/test_cockpit_polish.py`:

- **The work region breaks words anywhere.** `.work { overflow-wrap: anywhere }`
  is inherited by everything in it. `anywhere`, not `break-word`: only
  `anywhere` lowers an element's min-content width, which is what lets a flex
  or grid item (a page header, a card, a definition) give the width back.
- **A table scrolls itself.** Every `<table>` in a template sits directly in a
  `<div class="table-scroll">`, a scroller and not a box (the box, where there
  is one, is the `.panel` around it; it is `position: relative` so a visually
  hidden label inside is clipped with the table). Inside a table words stay
  whole, and only the name cell breaks anywhere: a cell holding a link or a
  `<strong>`, a row header, `.issue-title`, or `.cell-wrap` (plain text a person
  or agent wrote: a name that is not a link, a description), each with an
  `8rem` floor so it never becomes a column of letters. A table too wide for a
  phone scrolls inside its scroller; the page does not.
- **Controls shrink.** `select`, `input` and `textarea` have `max-width: 100%;
  min-width: 0` (a select is as wide as its longest option, and an option can
  be a long name), and either side of a `.page-header` may shrink below its
  widest control.
- **The navigation wraps.** `.nav` wraps its groups onto a second row, and
  under 820 px an open menu is a sheet under the whole bar rather than a
  dropdown hanging off its group.
- **Recorded values stay whole** where a break would misread them: an issue
  key in a table cell (`.issue-id`) does not wrap at its hyphen.

## Keyboard: the skip link

The first thing Tab reaches on every page is `<a class="skip-link"
href="#main">Skip to content</a>`, the first child of `<body>` in `base.html`.
It is clipped to nothing until it has focus (never `display: none`, which would
also take it out of the tab order) and shows over the app bar's corner when it
does. Its target is `<main class="work" id="main" tabindex="-1">`, so Enter
moves focus (not only the scroll position) to the work region, and the next
Tab starts there; the region draws no focus ring of its own, being no control.
A page that renders outside `base.html` must carry the same link.

## An empty page says what to do next

An empty list is a `.empty-state` (§8), and it says two things: what is
missing, and the one next step, as a link to where it is taken ("No projects
yet. Create the first project", linking to the form's `id` on the same page,
or to the page that does it). The step is gated by the same check as the
action: a viewer, who may not create, is told who does ("They appear here when
a member creates one") and given no link. When filters emptied the list, the
next step is clearing them, which anyone may take. `tests/test_cockpit_polish.py`
renders the empty issues list, board, projects, spaces, a space's pages,
labels, tokens, filters, inbox and activity as a member and as a viewer, and
the agents, webhooks, automation and sprints pages as an admin.

## Information architecture

Navigation follows the README's own operator loop. Don't invent a new one, and
don't add routes without also placing them in one of these five groups.

| Group | Means | Routes |
|---|---|---|
| Direct | what the fleet is pointed at | `/aegis/dashboard` `/aegis/boards` `/aegis` `/aegis/projects` `/mentor` `/aegis/labels` `/aegis/filters` |
| Delegate | who may act, with what scope | `/admin/agents` `/settings/tokens` `/admin/automation` |
| Observe | what happened, in order | `/aegis/activity` `/admin/agents/runs` `/aegis/fleet-metrics` `/admin/webhooks` |
| Intervene | asking for a human now | `/admin/run-controls` `/inbox` + the fleet-attention links |
| Trust | evidence you can hand over | `/admin/security` `/admin/users` |

Account settings (`/settings/password`, `/settings/identities`) live in the user
menu — they are not steps in the loop. `/`, where the logo links, is the
dashboard for a signed-in session and the landing page signed out.

## Agent-native UI vocabulary

This is what separates Athena from a project tracker. When you build a surface
that shows an agent, show these — the data already exists:

- **Liveness.** Every agent avatar carries a check-in dot:
  `reporting_recently` → success, `stale` → warning, `expired`/`defied` → danger.
  Silence is the primary signal a supervisor reads.
- **Run, not age.** Prefer the run id (`iris-run-0417`) over "created 2 days ago".
  Runs are how the work actually decomposes.
- **Claim, not assignment.** Leases, heartbeats and handoffs are distinct from a
  human assignment. Say which one you mean. Wherever a view names an issue's
  assignee it names the holder of its live lease beside it (`attach_claims` in
  `web/browsing.py` reads them for a whole list at once): a board card says
  "claimed by Sol", the issue list has a Claim column, and a board lane follows
  whoever is working the card, the holder when there is one. A lapsed lease
  is not a claim.
- **The trail is a ledger.** Show the hash-chain sequence number (`#2846`) and
  render a compensating event as `undoes #2841`. History is append-only.
- **Say what a number counts.** Every aggregate states its window and its
  exclusions in the same block — a scope label that moves without its number
  states something false. Say it in an operator's words: "every pending
  request", "the newest 200 pending requests; older ones may exist", "1 of 1
  examined". Not "input completeness unknown" or "considered candidates".
- **Names, not row ids or identifiers.** A page names the account ("Created by
  Ann"), never its id, and words a machine's detail ("claimed until 15:38 UTC")
  with the stored text in the element's title.

## Things that are load-bearing and easy to break

- `fleet_attention` computes **counts only** and links out. It must never compute
  state of its own, or it can disagree with the page it points at.
  - **Composed lanes are allowed beside the counts.** The card may also show
    bounded, read-only lanes from a projection another surface already serves.
    Today the progress lanes from `aegis/work_progress.py` (also behind active
    work and the desk): the oldest request waiting on you, expired leases, and
    stale in-progress work. The card composes them; it does not compute them.
  - **Each lane carries its own count and meaning,** caps its rows (20) with
    "showing N of M", links every row to its owning record, and stays out of the
    signal total.
  - **When a lane moves to its own page.** Any fourth lane, or any uncapped list,
    moves the lanes to a dedicated Observe page instead of growing the card.
- **No developer note ships in the interface** (U15). The navigation once
  printed each link's route beside its name, pages closed with "Data from the
  Aegis issues API.", the new-issue form promised that "in production it will
  hit the real REST API", and a REST call stood in for a button. Copy is
  written for the operator: no route path, raw HTTP call, requirement id,
  implementation name (REST, htmx) or work note in visible text.
  `tests/test_cockpit_polish.py` scans every template's visible text (Jinja
  comments are not shipped and stay). Real content is not a note: an issue
  key, a run id, a command the operator runs (`athena-doctor`).
- **No template carries a script, a handler or a style attribute.** The
  content security policy is `script-src 'self'` and a `style-src` with no
  `'unsafe-inline'`, so an inline `<script>`, an `onchange="…"` or a
  `style="…"` is silently blocked in the browser and the page looks fine in
  every test that does not run one. A page asks for behaviour with a data
  attribute that `static/confirm.js` handles from the document: `data-confirm`
  (ask before a form submits), `data-autosubmit` (submit the form when the
  control changes; keep the submit button for a browser without scripts) and
  `data-copy-command` (copy the text, then say "Copied"). A style that depends
  on data is a `<style>` element carrying the response's nonce. htmx is
  configured in `base.html` to add no style of its own and evaluate nothing.
  `tests/test_no_inline_script_or_handler.py` reads every template and renders
  every page. One more attribute is handled by `static/drafts.js`:
  `data-draft` on a comment box keeps what is typed in the tab's
  `sessionStorage` until the comment is accepted (see "A refusal is a page").
- **A refusal is a page, never a bare line** (U11, COCKPIT-13). A browser
  route never answers a refusal with a bare `HTMLResponse('<div
  class="error">…</div>', 4xx)`: `tests/test_form_errors_in_page.py` scans
  `web/` for one. Two answers instead:
  - *The page that posted it.* A validation refusal of a form on a page with a
    context builder (the issue page and its edit form, the Mentor page and its
    editor, the space page and its edit form, the filters page, the admin
    pages) re-renders that page at the same status, with the error at the top
    (`.error`, or `.alert[data-tone="danger"]` on the issue page) and what was
    typed back in the fields (a `typed` dict in the context).
  - *The shared refusal page.* Everything else (sign-in required, read-only,
    admin only, not found, a route with no page of its own) is a
    `BrowserRefusal` (`web/form_errors.py`): built like the `HTMLResponse` it
    replaces, it sends htmx the same fragment it always got and everyone else
    `templates/refused.html` — the fragment inside a `.panel` in the shell, a
    heading by status, and a link back (a same-origin Referer, else the
    section's index; never anything read from the object, so hidden and
    missing still answer alike). A comment refused for want of a session also
    shows the text that was not posted.

  The shared refusal page carries `data-refused`, and so does the error slot
  of every page with a comment box (the issue page, the Mentor page). The
  slots of the editors, the space and filters pages and the fleet roster
  carry it too. `static/drafts.js` reads it to know a post was not accepted,
  so a comment draft survives the refusal, the sign-in and the return to the
  issue or page, and is forgotten only when the comment lands.
- An unanswered run control reads as **expired, never as obeyed**. Athena cannot
  signal a process.
- Budgets meter **actions, not tokens or dollars**. Athena never observes model spend.
- Imported (`imported_at`) history is what a registered source *said* happened.
  Never present it as native.
- Board move refusals: `stale` and `policy` fail closed with `role="alert"`.
  Ordinary rejected moves snap back silently — that is deliberate.

## Chip tone mapping

`priority`, agent `reporting_state`/`claim_state`, agent `health_state`,
`sprint.state`, and token-warning `severity` are all closed `Literal` types, so
they map to a tone by exact value — see the Jinja filters in
`src/athena/web/chips.py` (`priority_tone`, `checkin_tone`, `health_tone`,
`sprint_tone`, `token_tone`), registered in `main.py`.

Issue **status** is the one open-ended domain — a project can rename or add
statuses — so status chips color by CATEGORY wherever the row carries
`status_category` (added to the shared issue `_SELECT`, resolved by the same
`category_sql` expression the work-query language uses): use
`{{ issue.status_category | category_tone }}`. A project whose done-state is
named `shipped` colors exactly like one calling it `done`. `status_tone`
(name-based, seed-set only) remains ONLY for rows that genuinely carry no
category — search index hits, cross-project by-status rollups (one name can
map to different categories in different projects), and projections that
build their own rows. When you add a surface, prefer threading the category
through its query over reaching for the name-based fallback.

## §8 Ported Surfaces — why a "legacy-looking" section exists

`styles.css` §8 holds ~116 structural classes ported from the pre-Phosphor
stylesheet (activity trail, issue-detail layout, search, login, inbox, board
filter bar, Mission Control tables, lineage/time-travel, fleet metrics, home).
The design drop-in restyled the four concept screens and deleted everything
else's structure; §8 is what keeps the rest of the app rendered. Every hex was
replaced with its token, so both themes work. **Do not delete §8 as cleanup.**
The intended path is the opposite: redesign a surface properly, move its rules
into the appropriate section, and shrink §8 one surface at a time. §8 also
carries the `.htmx-request` busy mirrors — htmx toggles that class, never
`aria-busy`, so §6's `[aria-busy]` indicators alone are dead code.

## Delivered follow-ups (2026-08-08, PRs #342–#344)

Everything the original migration deferred has since shipped:

- **Two CI contracts** (`scripts/check_template_styles.py`,
  `scripts/check_template_routes.py`) — every rendered class must have a rule
  or a justified allowlist entry; every template endpoint must reach a
  registered route. Both run in CI beside the other contract scripts.
- **Category-based status chips** (see Chip tone mapping above).
- **`/settings/theme` + `/settings/rail`** — cookie-backed preferences with
  their buttons in the shell; system/dark/light cycle, rail collapse.
- **Fleet attention in the Intervene nav badge** — read per request for
  admins, lazily, only when a page renders the nav (`web/nav.py`); members get
  nothing, not zeros. The badge is the "needs you" total alone; the card's live
  refresh swaps it out of band so the two never differ (U6). The badge also
  refreshes itself on every page, as a panel of the dashboard route, and an
  issue page's supervision block refreshes as a panel of the issue route
  (U12); both use `partials/live.html` like every other live panel.
- **Issue detail** adopted `.issue-layout` + `.supervision` + `.meta-rail`;
  **Mentor page detail** adopted `.doc-layout` + `.doc-admin` disclosures.
  Their §8 shell rules are gone — the shrink-one-surface-at-a-time path in
  the §8 section above is how the rest of §8 retires too.
- **Footer version stamp**, **empty-instance login hint**, and working
  **htmx busy indicators** (inherited `hx-indicator` per surface).
