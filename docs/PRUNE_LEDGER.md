# Prune ledger

VISION.md's opening says: *when a proposed feature doesn't serve the picture, cut it
or reshape it.* Nothing visible has ever been cut. Meanwhile the surface is **362
routes, 124 MCP tools, 65 tables and ~46,600 lines** across the feature modules,
and the external review named that maintenance surface the project's biggest
long-term risk — bigger than any individual bug it found.

The problem is not that anything here is obviously dead. It is that nothing creates
*pressure* to ask. Every feature arrived with a reason, and a reason is enough to
add something; it is not enough to keep it forever. So this is a standing review
rather than a one-time audit: once a quarter, each subsystem answers the same
question, and answering "no idea" three times in a row is itself the finding.

**This first ledger is a draft for the release owner.** Every verdict below is
deliberately unset. Filling them in is not a job an agent should do — a cut is a
promise to users, and parking a subsystem is a judgement about what Athena is for.

---

## How to run the review

```bash
python scripts/prune_evidence.py /path/to/dogfood/athena.db --markdown
```

That regenerates the table below from a real database, read-only (the file is
opened `immutable=1`, so it is safe to point at a live deployment). Paste the
output over the table, then fill the verdict column.

**The evidence column cannot be produced from the repository.** Source code proves a
feature exists; only a database somebody has been working in shows whether anyone
reached for it. The table currently reflects **the demo seed**, not the dogfood
deployment — a placeholder that shows the shape and proves the tooling works. It
should be regenerated against real data before any verdict is set, and the demo
numbers should not be read as evidence about anything.

### What the columns mean, and how they mislead

Three ways to misread this table, in the order they are likely to bite:

- **`n/a — leaves no trace` is not zero.** A pure read surface — the graph view, the
  answerability page, search, export, recovery — writes nothing, so this script
  cannot see it *by construction*. Those rows are marked `n/a` rather than `0`
  precisely because a reviewer skimming a column of zeroes will not stop to make the
  distinction. Two of the three subsystems F-3.4 names as candidates are in this
  category, which is the awkward part: the ones most in question are the ones the
  evidence is worst at.
- **Zero is not proof of disuse.** A measurable subsystem reading zero means nobody
  used it *in this database, in this window*. A feature used once a year looks
  identical to a dead one over a single quarter. That is why the ledger is standing:
  the argument is carried by the **trend**, not by one run. Three consecutive quiet
  quarters is a case; one is a data point.
- **Some counts are floors, not usage.** Row counts for indexes maintained on every
  write (the link table, the search index) track workspace size, not whether anyone
  opened the thing that reads them. The map excludes migration-seeded singleton
  tables for the same reason — a table that can never read zero measures nothing.

### What a verdict means

| verdict | meaning | what it costs |
|---|---|---|
| **keep** | earns its surface | nothing changes |
| **park** | probably not needed, but not provably | routes return **410 Gone** with a pointer, code moves under an `attic/` marker, **tests stay** |
| **cut** | gone, with its data | a migration, a CHANGELOG entry, and a promise broken if anyone was using it |

Parking is the point of having three options. It is reversible, it keeps the tests
green so the code cannot rot silently, and a 410 with a pointer tells a user what
happened instead of a 404 that reads like a bug. **Prefer park to cut** unless the
data is also a liability — a wrong keep costs maintenance, a wrong cut costs trust.

---

## The ledger — 2026-Q3 (draft, verdicts unset)

<!-- Regenerate with: python scripts/prune_evidence.py <db> --markdown -->
<!-- Numbers below are from the DEMO SEED, not the dogfood deployment. -->

| subsystem | surface | evidence of use | last seen | verdict |
|---|---|---|---|---|
| **issues (Aegis core)** | `/issues`, `/aegis`, `/projects`, `/sprints`, `/labels` | 4 rows, 9 events | 2026-08-15 | _(unset)_ |
| **pages (Mentor core)** | `/pages`, `/mentor`, `/spaces` | 3 rows, 3 events | 2026-08-15 | _(unset)_ |
| **agent supervision** | `/workers`, `/approvals`, `/run-controls`, `/agent-runs`, `/fleet` | 8 rows, 6 events | 2026-08-15 | _(unset)_ |
| **activity trail** | `/activity`, `/events` | 46 rows | — (no dated verbs) | _(unset)_ |
| **automation rules** | `/automation` | 0 rows, 0 events | **never** | _(unset)_ |
| **webhooks (outbound)** | `/webhooks` | 0 rows, 0 events | **never** | _(unset)_ |
| **forge inbound** | `/forge`, `/event-sources` | 0 rows, 0 events | **never** | _(unset)_ |
| **Icarus dispatch** | `/dispatches`, `/callbacks` | 0 rows, 0 events | **never** | _(unset)_ |
| **knowledge graph / ego view** | `/aegis/graph` | n/a — leaves no trace | — | _(unset)_ |
| **answerability ledger** | `/admin/answerability` | n/a — leaves no trace | — | _(unset)_ |
| **workspace search** | `/search`, `/find` | n/a — leaves no trace | — | _(unset)_ |
| **playbooks / workflows** | `/workflows` | n/a — leaves no trace | — | _(unset)_ |
| **desk / cursors** | `/desk` | 0 rows | — (no dated verbs) | _(unset)_ |
| **delegation inbox** | `/delegations`, `/inbox` | 0 events | **never** | _(unset)_ |
| **portability (export/import)** | — | n/a — leaves no trace | — | _(unset)_ |
| **recovery (backup/restore)** | — | n/a — leaves no trace | — | _(unset)_ |
| **attachments** | `/attachments` | 0 rows, 0 events | **never** | _(unset)_ |
| **notifications / watches** | `/notifications`, `/watches` | 13 rows | — (no dated verbs) | _(unset)_ |
| **saved filters** | `/filters` | 0 rows | — (no dated verbs) | _(unset)_ |
| **OIDC login** | `/auth` | 0 rows | — (no dated verbs) | _(unset)_ |

---

## The three candidates F-3.4 named

The guide names three to *evaluate*, explicitly not pre-judged. Here is what can be
said about each without deciding, and — more usefully — what evidence would settle
it, since two of the three are invisible to the script.

### Knowledge graph / ego view

Reads the `links` table that `sync_links` maintains on every write. That table is
**not optional**: backlinks depend on it, and backlinks are part of the one-roof
pitch. So the question is narrower than it looks — not "is the link index worth
keeping" (yes, backlinks need it) but "does the *graph view* earn its own surface on
top of backlinks that already work?"

*What would settle it:* whether the operator has ever opened `/aegis/graph` when a
backlink list would not have answered the same question. That is a question about
habit, not data — nothing records it.

### Forge inbound

The one candidate the script can actually measure, and the one where measuring it
naively gets the wrong answer. Forge events land as **imported history** by design
(the module: *every landed row is Athena's record of what it was told*), and the
default evidence query excludes imported rows — so the first version of this tooling
reported forge as never used regardless of the truth. That is now a flagged
exception with a test pinning it.

*What would settle it:* whether any event source is registered and has delivered.
Both are visible in the table above once it is run against real data. Note also that
forge inbound is the only subsystem accepting **unauthenticated-by-default network
input**, so its security surface is disproportionate to its size — a point for
parking it if unused, not merely a neutral fact.

### Answerability ledger's web surface

Computed on read from existing data; owns no table, writes no verb. Completely
invisible here.

*What would settle it:* whether the page has ever changed a decision. It is a
judgement surface rather than a data surface, and the honest test is whether the
operator can recall acting on it.

---

## Next review

**2026-Q4.** Regenerate against the dogfood database, compare with this quarter, and
set verdicts where the trend supports one. If a subsystem reads quiet twice in a
row, that is the point to decide rather than to note it again — a ledger that only
ever records "still quiet" has become the thing it was built to prevent.
