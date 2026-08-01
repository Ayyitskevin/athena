# Planning — the timeline and live rollups

Athena has had sprints since migration 0033 and typed dependencies since 0016,
but nothing ever drew them together: the sprint page is a table, and a
dependency was a list item on one issue. This is what Stage Q added — the two
views that answer *when is this happening* and *how far along is it* — and,
just as importantly, the claims they refuse to make.

- [The timeline](#the-timeline)
- [Live rollups](#live-rollups)
- [The rollup embed](#the-rollup-embed)
- [Why there are no target dates](#why-there-are-no-target-dates)
- [Limits, stated](#limits-stated)

## The timeline

**`/aegis/projects/{id}/timeline`** — one project's sprints as lanes, its issues
placed in the lane their sprint puts them in, and the declared dependencies
between those issues drawn as edges. A solid arrow means *blocks*, pointing at
the blocked issue; a dashed line means *relates* and has no direction.

Lanes run in date order: every sprint that has a date sorts by it (its start
date, or its end date when that is all it has), then undated sprints in creation
order, and the **backlog last** — where work sits when it is not scheduled at all. The backlog
lane is always drawn, because "nothing is unscheduled" is a fact worth seeing
and a missing lane would read as a rendering gap.

**Lane width is not a duration, and the view says so.** Sprint dates are
nullable and never validated: a planned sprint legitimately has none, and
nothing stops an end date before its start. Scaling lanes by those dates would
collapse an undated project and misdraw a mis-dated one, so lanes are equal
columns and each prints its own dates as a label. The *order* is a real reading
of time; the *width* is not a claim at all.

The picture is drawn from coordinates computed in `aegis/timeline.py`; the
template only places what it was given. There is no JavaScript, and every card
is a real link, so the roadmap is keyboard-navigable and readable in a text
browser — the same rule the [link graph](GRAPH.md) follows.

It is **read-only by construction**. Moving an issue between sprints goes
through the sprint form on the issue itself, which already owns that write and
its audit event. A roadmap that could also reorder work would be a second write
path for placement, and the two would drift.

The same structure is available as data:

```text
GET /projects/{project_id}/timeline?max_per_lane=12&max_items=120
```

and over MCP as `project_timeline(project_id)`, so an agent reads the plan the
operator is looking at rather than reconstructing it.

## Live rollups

A parent issue's page shows how its sub-issues are distributed across status
categories, as a bar and a sentence: *50% done — 1 done, 0 in progress, 1 to
do*. It is computed on **every read** from the real children. There is no
denormalized progress column and there must not be: a cached rollup is stale
the moment a child moves, and it is a visibility leak the moment a different
viewer opens the page.

**Done-ness is a category, never a status name.** The buckets come from each
child's own project status configuration, so a project whose finished state is
called `shipped` fills the bar exactly like one that calls it `done` — the
promise [QUERY.md](QUERY.md) and [WORKFLOW_GATES.md](WORKFLOW_GATES.md) already
make about closed-ness. A child in a *different* project resolves against that
project's vocabulary, because parenting has no same-project rule.

Two exclusions, both deliberate:

| Excluded | Counted aloud? | Why |
|---|---|---|
| Archived children | **Yes** — "1 archived child is not counted" | Abandoned work must not sit in a denominator forever, but a bar that quietly drops rows is a lie |
| Children the viewer cannot see | **No** | Reporting them would make the bar an existence oracle for private work |

The timeline applies the same rule to its off-picture dependency count: only
edges whose far end the viewer may see are counted, so the number cannot move
when hidden work gains a link to something visible.

A parent with no live children reports 0%, not 100%: having nothing to do is
not the same as being finished. For the same reason **100% is reserved for
everything being done and 0% for nothing being done** — 199 of 200 children
rounds to 99%, not to a triumphant 100. And a parent whose children are *all*
archived says exactly that, rather than reporting 0% as though the work were
untouched.

## The rollup embed

The same computation is available inside a page, so a plan document can carry
live progress instead of a number somebody pasted in last month:

````text
```athena
kind: rollup
issue: ATH-12
title: Migration progress
```
````

`issue:` takes an id or a project key, exactly like `kind: issue`. The embed
resolves per request against **the reader**, so two people can open the same
page and correctly see different totals when one of them cannot see a child.

It is the same `aegis/rollups.py` call the issue page makes. That is the point:
one owner for the number means the page and the dashboard-in-a-page cannot
disagree about how done something is. See [EMBEDS.md](EMBEDS.md) for the
directive syntax, budgets, and how a refused embed renders.

## Why there are no target dates

Stage Q was chartered with an optional third piece — a per-issue `target_date` —
to be built *only if the timeline proved insufficient without it*. It was not
built, and this is the record of why.

The timeline places issues by **sprint**, and sprints already carry real dates
that the lanes print. A per-issue target date would not move a single card,
because nothing positions by it — it would be a second, finer date sitting
beside the one the view actually reads. And a date field with no enforcement is
an invitation to build the thing the charter explicitly rules out: reminders,
escalation, SLA math. Athena schedules nothing and chases nobody.

If the need reappears, it should arrive with the question it answers, not as a
column. Note for whoever picks it up: the guide names "migration 0069" for it,
but that number (and several after it) is long since applied — check
`src/athena/core/migrations/` for the next free number rather than trusting
any doc to have kept count.

## Limits, stated

- Each lane draws its first **12** issues and the whole picture stops at **120**
  (`max_per_lane` / `max_items`, clamped at 40 and 400). The overall ceiling is
  spent left to right, so a late lane can be cut short or even drawn empty —
  each lane prints how many of its own issues are missing, and the line beneath
  the drawing says the ceiling exists. A clipped roadmap never reads as the
  whole plan.
- **Dependencies with one end off the picture are counted, not drawn.** An
  arrow into empty space reads as a rendering bug; no arrow at all reads as
  "nothing blocks this". The line beneath the drawing gives the number.
- Archived issues are not drawn, and neither are issues the viewer cannot see.
- Dependency cycles are storable (only the direct two-cycle is refused at write
  time), and the timeline tolerates them: placement comes from sprint
  membership alone, so nothing here sorts topologically and nothing can loop.
- The rollup counts **direct children only**, one level deep. A grandchild's
  progress does not roll up into its grandparent.
- A project with no sprints and no issues renders an empty state rather than an
  empty drawing.

## What these views do not claim

Neither view schedules, assigns, reorders, or predicts. The timeline reports
which sprint an issue is in and what it declares a dependency on; the rollup
reports how many children are in each status category right now. There is no
critical path, no forecast completion date, no velocity, and no workflow
engine — and a lane's width, to say it once more, is not a duration.
