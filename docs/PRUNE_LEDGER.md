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

## There is no usage evidence, and that is the first finding

F-3.4 asks for "its last real use in the dogfood deployment". **There is no dogfood
deployment.** Athena has never been run in anger — by anyone, including its author.
RELEASE_READINESS.md has said so all along, in a line that reads differently once
you are trying to prune: *"No production deployment has occurred. All evidence here
comes from synthetic temporary databases and loopback processes."*

So the column this ledger was designed around cannot be filled — not because the
data is out of reach, but because it was never created. That is worth stating
before any table, because it reframes the exercise. The question is not "which of
these 362 routes has gone quiet?" Nothing has gone quiet; nothing has ever spoken.
The question is **"which of these was built on a demonstrated need, and which on a
guess?"** — and a surface this size assembled without a single day of real use is a
larger risk than any subsystem on it.

The honest first verdict for most rows is therefore neither keep, park, nor cut. It
is **unknown, and unknowable until someone uses this thing.**

### What evidence does exist

Two artifacts encode what Athena's author believes the product IS, and both are
runnable today:

- **`athena-demo --seed-only` (the five-minute pitch, `src/athena/demo.py`)** — the story told to someone seeing
  Athena for the first time.
- **`scripts/field_exercise.py` (25 steps over real HTTP)** — the operator loop,
  end to end, as a working system.

Neither is usage evidence. Nobody chose to reach for a feature under time pressure;
these are scripted. But they are **intent evidence**, and intent evidence is exactly
the right instrument for the question above: a subsystem that neither canonical
story touches is one that even the author's own account of the product does not
need in order to be itself.

That is a signal available now, and it discriminates.

## How to run the review

```bash
python scripts/prune_evidence.py <database> --markdown
```

Read-only (`immutable=1`), so it is safe against a running deployment. Run it
against **both** canonical stories and compare — that comparison, not either number
alone, is what the table below records:

```bash
athena-demo --db /tmp/demo.db --attach-dir /tmp/demo-att --seed-only
python scripts/prune_evidence.py /tmp/demo.db          # the pitch
python scripts/prune_evidence.py <field-exercise db>   # the operator loop
```

The script is dependency-free (stdlib only, no Athena import), so it runs anywhere a
database does, without installing anything.

### Three ways this table misleads

- **`n/a — leaves no trace` is not zero.** A pure read surface — the graph view, the
  answerability page, search, export, recovery — writes nothing, so this script
  cannot see it *by construction*. Marked `n/a` rather than `0` precisely because a
  reviewer skimming a column of zeroes will not stop to make the distinction. Two of
  the three subsystems F-3.4 names as candidates are in this category: the evidence
  is worst exactly where the question is sharpest.
- **Zero here means "no canonical story touches it"**, not "unused". Given the
  paragraph above, no stronger reading is available.
- **Some counts are floors.** Row counts for indexes maintained on every write track
  workspace size, not use. Migration-seeded singleton tables are excluded for the
  same reason — a count that can never reach zero measures nothing.

### What a verdict means

| verdict | meaning | what it costs |
|---|---|---|
| **keep** | earns its surface | nothing changes |
| **park** | probably not needed, but not provably | routes return **410 Gone** with a pointer, code moves under an `attic/` marker, **tests stay** |
| **cut** | gone, with its data | a migration, a CHANGELOG entry, and a promise broken if anyone was using it |

Parking is why there are three options: reversible, tests stay green so the code
cannot rot silently, and a 410 with a pointer tells a user what happened instead of
a 404 that reads like a bug. **Prefer park to cut** — a wrong keep costs
maintenance, a wrong cut costs trust.

---

## The ledger — 2026-Q3 (draft, verdicts unset)

Measured 2026-08-15 against both canonical stories. "demo" is the five-minute pitch;
"field" is the 25-step operator loop.

| subsystem | surface | demo | field exercise | verdict |
|---|---|---|---|---|
| **issues (Aegis core)** | `/issues`, `/aegis`, `/projects` | 4 rows, 9 events | 3 rows, 5 events | _(unset)_ |
| **pages (Mentor core)** | `/pages`, `/mentor`, `/spaces` | 3 rows, 3 events | 7 rows, 8 events | _(unset)_ |
| **agent supervision** | `/workers`, `/approvals`, `/run-controls`, `/fleet` | 8 rows, 6 events | 5 rows, 4 events | _(unset)_ |
| **activity trail** | `/activity`, `/events` | 46 rows | 62 rows | _(unset)_ |
| **forge inbound** | `/forge`, `/event-sources` | — | **1 row, 2 events** | _(unset)_ |
| **Icarus dispatch** | `/dispatches`, `/callbacks` | — | **1 row, 4 events** | _(unset)_ |
| **delegation inbox** | `/delegations`, `/inbox` | — | **1 event** | _(unset)_ |
| **notifications / watches** | `/notifications`, `/watches` | 13 rows | 19 rows | _(unset)_ |
| **automation rules** | `/automation` | **neither** | **neither** | _(unset)_ |
| **webhooks (outbound)** | `/webhooks` | **neither** | **neither** | _(unset)_ |
| **attachments** | `/attachments` | **neither** | **neither** | _(unset)_ |
| **saved filters** | `/filters` | **neither** | **neither** | _(unset)_ |
| **desk / cursors** | `/desk` | **neither** | **neither** | _(unset)_ |
| **OIDC login** | `/auth` | **neither** | **neither** | _(unset)_ |
| **knowledge graph / ego view** | `/aegis/graph` | n/a — no trace | n/a — no trace | _(unset)_ |
| **answerability ledger** | `/admin/answerability` | n/a — no trace | n/a — no trace | _(unset)_ |
| **workspace search** | `/search`, `/find` | n/a — no trace | n/a — no trace | _(unset)_ |
| **playbooks / workflows** | `/workflows` | n/a — no trace | n/a — no trace | _(unset)_ |
| **portability (export/import)** | — | n/a — no trace | n/a — no trace | _(unset)_ |
| **recovery (backup/restore)** | — | n/a — no trace | n/a — no trace | _(unset)_ |

### What the comparison actually says

**Six measurable subsystems are touched by neither story:** automation rules,
outbound webhooks, attachments, saved filters, desk cursors, and OIDC login. That is
the shortlist this quarter produces — and note that F-3.4's three pre-named
candidates are **not** on it. Two of them cannot be measured at all, and the third
(forge inbound) is exercised by the field exercise, which is evidence *for* it.

Two of the six have obvious innocent explanations and should not be read as
findings: **OIDC login** is dormant unless four settings are configured, and
**attachments** need a file upload that a scripted story reasonably skips. The
other four are the real question — a rule engine, an outbound integration, stored
queries, and a cursor store, none of which either account of the product reaches
for.

**The one thing that would change this ledger more than any verdict** is using
Athena for a month. Every row above would gain a real answer, and the four
interesting ones would resolve themselves.

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

*What the evidence now says:* the field exercise registers a source and lands a
commit (step 24 — *"the forge reported in; it landed as imported and moved nothing"*),
so the operator loop's own account of Athena includes it. That is evidence **for**
forge inbound, and it is the one candidate of the three where this quarter's run
changes the picture rather than restating the question.

The counterweight is that forge inbound is the only subsystem accepting
**unauthenticated-by-default network input**, so its security surface is
disproportionate to its size. That is a reason to want it earning its keep, not a
neutral fact — but "the canonical story uses it" is a real answer, and it points
toward keep.

### Answerability ledger's web surface

Computed on read from existing data; owns no table, writes no verb. Completely
invisible here.

*What would settle it:* whether the page has ever changed a decision. It is a
judgement surface rather than a data surface, and the honest test is whether the
operator can recall acting on it.

---

## Next review

**2026-Q4.** Two different things could happen between now and then, and they call
for different reviews.

**If Athena is still unused**, re-running the two canonical stories will produce the
same six-row shortlist, and the ledger will have said the same thing twice. That is
itself a verdict — not about any subsystem, but about the project: a second
identical quarter means the shortlist is the best evidence that will ever exist, and
the four interesting rows should be decided on judgement rather than waited on
further.

**If Athena has been used for even a month**, throw this table away and regenerate
against the real database. Every row gains an answer the scripted stories cannot
give, and the `n/a` rows — the graph view, the answerability page, search — become
answerable for the first time, because the question stops being "does the code write
anything" and becomes "did you open it".

The second is worth more than any verdict set here. A prune ledger without usage is
a rehearsal; the thing that makes it real is using the product.
