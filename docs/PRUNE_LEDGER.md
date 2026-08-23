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

## Usage evidence now exists

F-3.4 asks for "its last real use in the dogfood deployment". The premise this
ledger started from is no longer true: Athena now has a canonical dogfood
deployment. On 2026-08-22 its `/healthz` and `/readyz` endpoints both reported
`ok`, and the read-only evidence command below measured its live database:

| signal | live evidence |
|---|---|
| issues (Aegis core) | 64 rows, 93 events; last seen 2026-08-22 04:38:18 |
| pages (Mentor core) | 6 rows, 11 events; last seen 2026-08-22 01:01:38 |
| agent supervision | 13 rows, 49 events; last seen 2026-08-22 03:30:22 |
| activity trail | 368 rows |
| delegation inbox | 4 events; last seen 2026-08-16 03:42:10 |
| notifications / watches | 66 rows |
| automation rules | 0 rows, 0 events |
| webhooks (outbound) | 0 rows, 0 events |

That final pair is evidence of no recorded use in this snapshot, not a parking
verdict. Usage is only one input: current integration dependencies and shared
security/transport primitives are separate evidence, and removing either feature
can break a live neighboring subsystem without changing these counts first.

The deployed checkout was at `2268971` when measured, while repository `main` had
advanced. This snapshot therefore describes the deployed database, not a claim
that every capability on current `main` was already running there.

### Additional intent evidence

Two artifacts encode what Athena's author believes the product IS, and both are
runnable today:

- **`athena-demo --seed-only` (the five-minute pitch, `src/athena/demo.py`)** — the story told to someone seeing
  Athena for the first time.
- **`scripts/field_exercise.py` (25 steps over real HTTP)** — the operator loop,
  end to end, as a working system.

Neither is usage evidence. These are scripted, but they remain **intent evidence**:
a subsystem that neither canonical story touches is one the author's own account
of the product did not need in order to be itself at the time the story was written.

That is a signal available now, and it discriminates.

## How to run the review

```bash
python scripts/prune_evidence.py <database> --markdown
```

Read-only (`immutable=1`), so it is safe against a running deployment. Run it
against the canonical dogfood database first. The two scripted stories remain
useful as a historical baseline or when no deployment exists:

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
- **Zero means "no recorded use in this database or story"**, not "safe to
  remove". Runtime dependencies and shared primitives are not usage rows.
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
"field" is the 25-step operator loop. This table is the pre-deployment baseline;
the dated live snapshot above is the current usage evidence.

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

**Six measurable subsystems were touched by neither scripted story:** automation rules,
outbound webhooks, attachments, saved filters, desk cursors, and OIDC login. That is
the shortlist the baseline produced — and note that F-3.4's three pre-named
candidates are **not** on it. Two of them cannot be measured at all, and the third
(forge inbound) is exercised by the field exercise, which is evidence *for* it.

Two of the six have obvious innocent explanations and should not be read as
findings: **OIDC login** is dormant unless four settings are configured, and
**attachments** need a file upload that a scripted story reasonably skips. The
other four were the real question in that baseline — a rule engine, an outbound
integration, stored queries, and a cursor store, none of which either scripted
account of the product reached for.

The live snapshot supersedes the baseline for usage claims. It does not resolve the
verdicts by itself: zero recorded automation or webhook rows must be weighed against
their current integration role before either can be parked.

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

**2026-Q4.** Re-run `scripts/prune_evidence.py` against the canonical dogfood
database and compare it with the dated live snapshot above. Treat row-count and
last-seen movement as usage evidence, then inspect runtime consumers and integration
contracts before setting any verdict. The `n/a` rows — graph view, answerability,
search, export, and recovery — still require operator evidence because they leave no
database trace by construction.
