# Playbooks — docs that start work

Athena's two modules have been able to see each other for a while. Live embeds
let a page **show** work: a fenced ```athena directive resolves real issues at
view time, with the viewer's own visibility. Run learnings let work **write
back** to a page: what a run learned is promoted into the issue's runbook,
quoted and attributed. The direction that was missing is the one that starts
things.

A **playbook** is an ordinary Mentor page carrying the `playbook` label whose
markdown checklist can be turned into real work: one parent issue, one child per
unchecked step — and **indentation is structure**, so an indented step nests
under the issue its enclosing step became. A checklist with sub-steps
instantiates as the same issue tree a hand would build, one `set_issue_parent`
per child.

```text
POST /pages/{page_id}/start-playbook   {"project_id": 12, "title": "March release"}
```

MCP: `start_playbook(page_id, project_id=None, title=None, idempotency_key=None)`.

After that, all three directions are live at once, and the last one costs no new
machinery: every created issue cites the page with an ordinary `[[page:N]]`
wikilink, so the existing indexer builds the backlinks, the page shows the work
it started, and a `kind: rollup` embed on that same page counts the children's
progress. No code in the playbook command knows what a link, a backlink, or an
embed is.

## What becomes work

| Line | Result |
|---|---|
| `- [ ] Freeze the release branch` | a child issue titled "Freeze the release branch" |
| `* [ ] …`, `+ [ ] …` | the same — the common bullet styles |
| `  - [ ] Announce the window` (indented) | a child of the issue the enclosing step became |
| `- [x] Tell the operator` | **counted and skipped**, reported as `checked_skipped` |
| `- [ ]` with no text | skipped (an issue with no title helps nobody) |
| `- [] malformed`, prose mentioning `[ ]` | ignored |

Nesting is **relative**: two spaces or a tab (read as four) both mean "deeper
than the line above", and siblings return to their level. A ticked box is the
author saying the step is already done. Creating an issue for it would be the
tool arguing with its author, so those are counted and reported rather than
silently dropped or silently created — but a ticked step's unchecked sub-steps
are still real work, and they attach to the nearest ancestor that became work
(top level when none did) rather than vanishing with their parent.

## A template is not a live mirror

Starting a playbook **snapshots** the page. Editing it afterwards changes
nothing that already exists — no reconciliation, no sync-back, no drift
detection — and starting it again creates a **second, independent**
instantiation. That is what templates are for: the deploy checklist you run
every release should produce a new set of issues every release.

Retry-safety comes from the ordinary `Idempotency-Key` contract the `/pages`
API root already honors. There is deliberately **no** playbook-specific replay
table: a second mechanism would be a second thing to keep in sync with the
first, and this one already works (same key → same parent, four issues rather
than eight).

## Bounds and refusals

| Bound | Value | Refusal |
|---|---|---|
| Unchecked steps | 1–50 | `422` with none, `429` (capacity) above 50 |
| Step / override title | 200 chars | truncated (steps) · `422` (blank override) |
| Page state | live, visible, labeled | `404` unseen or missing (same answer) · `422` archived or unlabeled |

Fifty issues from one call is already generous; a page that would create more is
a data-entry accident, not a plan. Everything lands in **one transaction**, so a
refusal partway through leaves no orphaned parent with half its steps.

## Where it lives, and why that is new

The command is `src/athena/workflows/playbook_commands.py` — the first
inhabitant of a layer added for it. Aegis and Mentor are peers by design and
neither may import the other; `web/` cannot own the work because the cardinal
rule keeps authorization out of transports. A command that must read a page and
create issues therefore had **no legal home** until `workflows/` existed. The
rules are otherwise unchanged: workflows may import both modules and core, and
nothing below may import workflows.

The writes themselves still belong to Aegis. `issue_commands.create_issue` and
`set_issue_parent` own them, with their audit events, their budget metering, and
their authorization — a playbook is not a second way to create an issue.

## What this does not claim

- **It does not track the work it started.** There is no "playbook run" entity,
  no completion state, no link back from the issues to a playbook lifecycle.
  The parent issue's own children ARE the progress, counted by the same rollup
  any parent gets.
- **It does not keep the page and the work in sync.** See the snapshot rule
  above. If the procedure changes, start it again.
- **It does not tick boxes for you.** Closing the child issue does not edit the
  page. The page is the procedure; the issues are one run of it, and Athena
  will not rewrite an author's document as a side effect of work.
- **It grants no new authority.** You can start a playbook exactly when you
  could both see the page and create the issues by hand.
