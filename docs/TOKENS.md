# Token reference

Every value in `styles.css` §1. Nothing visual exists outside this list.

## Surfaces

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#0B0E14` | `#F4F6F8` | Page ground. Content sits directly on it. |
| `--surface` | `#11151D` | `#FFFFFF` | Panels, cards, bars |
| `--surface-2` | `#161B26` | `#F1F4F8` | Inputs, hover, footers, quiet fills |
| `--surface-3` | `#1C2230` | `#E7ECF3` | Track fills, code, key badges |
| `--border` | `#232B3A` | `#DCE2EB` | Hairlines |
| `--border-strong` | `#33405A` | `#B9C3D2` | Input borders, menu edges, dashed empties |

Depth is a **surface ramp**, not a shadow, in dark mode — an eight-hour glance at
a monitor should have no glow to fight.

## Text

| Token | Dark | Light | Min contrast | Use |
|---|---|---|---|---|
| `--text` | `#E6EAF2` | `#0F131B` | 14:1 | Titles, values, primary copy |
| `--text-2` | `#A6B2C7` | `#4B566D` | 6.2:1 | Body, labels, secondary copy |
| `--text-3` | `#8290A9` | `#616B80` | **4.51:1** | Metadata, timestamps, scopes, help |

`--text-3` is the most-used token in the app. Both values were chosen to clear
4.5:1 against the four flat surfaces — `--surface`, `--surface-2`, `--surface-3`
and `--bg`. Do not darken them "just a little"; all four have to keep passing.

**Where the guarantee stops — and what handles it.** Over a translucent `-soft`
tint layered on another surface (`.meta` in an `.is-attention` row,
`.field-help` in a `.check--danger`) the composite lifts enough to drop
`--text-3` to ~4.4:1. `styles.css` §6 carries an explicit rule stepping those
contexts up to `--text-2`, which measures above 6:1. **If you add a new tinted
container with muted text inside it, add its selector to that rule.** With it in
place the audited floor is 4.51:1 in light and 4.93:1 in dark across chips,
metadata, help text, counts, run ids, timestamps and footers.

## Accent — split in two, on purpose

| Token | Dark | Light | Use |
|---|---|---|---|
| `--accent` | `#C7F24A` | `#9BD316` | **Fill only.** Buttons, badges, active pills, brand mark, rail indicator. Always with `--accent-ink`. |
| `--accent-text` | `#C7F24A` | `#3F6212` | **Foreground.** Text, icons, chart strokes, focus ring, the hero numeral. |
| `--accent-ink` | `#0C1200` | `#101A00` | Text on an accent fill |
| `--accent-soft` | 13% lime | `#EEF9CE` | Chip and row tints |
| `--accent-line` | 38% lime | `#C6E77A` | Chip borders, focus-adjacent edges |

In light mode `--accent` is 1.65:1 on white. `color: var(--accent)` is always a bug.

## Semantics

| Token | Dark | Light | Means |
|---|---|---|---|
| `--success` | `#2DD4BF` | `#0F766E` | done, verified, reporting, healthy |
| `--warning` | `#FBBF24` | `#92400E` | stale, near a ceiling, imported, advisory |
| `--danger` | `#FB7185` | `#B42318` | blocked, refused, expired, defied, revoked |
| `--info` | `#7DB8FF` | `#1D4ED8` | in progress, informational, neutral-positive |
| `--agent` | `#A78BFA` | `#6D28D9` | **machine-authored** — runs, lineage, agent identity |
| `--neutral` | `#9AA6BC` | `#56607A` | open, low priority, unclassified |

Each has `-soft` (fill) and `-line` (border) siblings. `.chip[data-tone]` and
`.alert[data-tone]` wire all three automatically.

Green is `--success`, and it is a blue-green (teal) precisely so it never reads
as the yellow-green accent. They mean different things and must not be confused.

## Type scale

| Token | px | Use |
|---|---|---|
| `--fs-xs` | 11 | chips, eyebrows, axis labels, run ids |
| `--fs-sm` | 12 | help text, table meta, buttons |
| `--fs-md` | 13 | dense UI, list rows, table cells |
| `--fs-base` | 14 | default |
| `--fs-lg` | 16 | prose |
| `--fs-xl` | 19 | section titles, supervision values |
| `--fs-2xl` | 26 | page titles, signal numerals |
| `--fs-3xl` | 34 | stat numerals |
| `--fs-display` | 72 | the **one** hero numeral per screen |

Fonts: `--font-ui` (Public Sans) for anything a human wrote, `--font-mono`
(Azeret Mono) for anything Athena recorded.

## Space, radius, frame

`--sp-1` 4 · `--sp-2` 8 · `--sp-3` 12 · `--sp-4` 16 · `--sp-5` 20 · `--sp-6` 24 · `--sp-8` 32 · `--sp-10` 40

`--r-sm` 5 · `--r-md` 9 · `--r-lg` 14 · `--r-xl` 18 · `--r-pill` 999

`--bar` 60px · `--rail` 232px · `--measure` 76ch

## Motion

`--ease: cubic-bezier(.2,.85,.25,1)` · `--dur-fast` .16s · `--dur` .24s · `--dur-slow` .45s

All motion is inside a `prefers-reduced-motion` guard. Nothing animates on a
loop except the liveness pulse and the busy indicator, both of which encode state.

**Never transition `color`, `background` or `border-color`.** Those values come
from theme tokens, and a transitioned property does not re-resolve when
`data-theme` changes — it freezes at the old theme's value. With
`prefers-color-scheme` honoured, an OS day/night flip re-themes with no reload,
which is exactly the freeze condition. Transition `transform`, `opacity`,
`box-shadow` and `filter`.

## Audited floor

Measured on `preview.html` across **every text-bearing element** — 209 nodes per
theme, enumerated from the DOM rather than a curated selector list — each
composited against its full background stack, **after a live theme flip** (not a
reload): **4.68:1 light · 4.81:1 dark, zero failures.**

### The recurring trap: translucent fills on tinted rows

Three separate defects had the same cause. A translucent `-soft` fill sitting on
an already-tinted row (`tr.is-attention`, `.alert`, `.list-row.is-unread`)
composites two tints, lightens, and drops its own text under 4.5:1.

Two remedies, and the choice matters:

- **Opaque base** for things whose tint is decorative —
  `.table tr.is-attention .chip`, `.run-id` get `background-color: var(--surface)`.
- **Opaque mix** for things whose tint is *identity* — `.avatar--agent` uses
  `color-mix(in oklab, var(--agent) 14%, var(--surface))`, which keeps the violet
  that distinguishes an agent from a person while making the fill stable on any
  background. Flattening it to `--surface` would have passed the audit and
  destroyed the signal.

**If you add a new tinted container, or put an existing component inside one,
re-measure it composited.** This class of bug is invisible in isolation.
