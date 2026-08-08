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
agent can do on its own is ever lime. Do not spend the accent on decoration.

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
| Work region | full-bleed |
| Prose | `--measure: 76ch`, applied **locally** inside the full-width shell |

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
menu — they are not steps in the loop.

## Agent-native UI vocabulary

This is what separates Athena from a project tracker. When you build a surface
that shows an agent, show these — the data already exists:

- **Liveness.** Every agent avatar carries a check-in dot:
  `reporting_recently` → success, `stale` → warning, `expired`/`defied` → danger.
  Silence is the primary signal a supervisor reads.
- **Run, not age.** Prefer the run id (`iris-run-0417`) over "created 2 days ago".
  Runs are how the work actually decomposes.
- **Claim, not assignment.** Leases, heartbeats and handoffs are distinct from a
  human assignment. Say which one you mean.
- **The trail is a ledger.** Show the hash-chain sequence number (`#2846`) and
  render a compensating event as `undoes #2841`. History is append-only.
- **Say what a number counts.** Every aggregate states its window and its
  exclusions in the same block — a scope label that moves without its number
  states something false.

## Things that are load-bearing and easy to break

- `fleet_attention` computes **counts only** and links out. It must never compute
  state of its own, or it can disagree with the page it points at.
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
statuses — so `chips.status_tone` is a best-effort map of the default seed set
(`open`/`in_progress`/`done`) with a `neutral` fallback, not a true
category lookup. The correct fix is mapping by category
(`statuses.global_category`, already used for board/sort ordering), which
needs a database connection a template filter doesn't have. **Follow-up**:
thread `category` through the aegis issue-query layer so status chips are
correct for custom project statuses too, not just the seeded defaults.

## Deferred to a follow-up PR

This migration intentionally stopped short of two items the design package
itself flagged as bigger, separable changes:

- **Mentor `page_detail.html` restructure** — turning the seven stacked
  `.form-container` sections (labels, comments, attachments, referenced by,
  history, activity, manage) into a rail + `.doc-admin` disclosures. A real
  layout reorganization, not a class rename.
- **`/settings/theme` and `/settings/rail` POST routes** — cookie-backed
  persistence for the user's theme and rail-collapsed preference. Optional:
  without them the shell still works, it just always renders dark with the
  rail open.
