# The work query language

One way to ask for work — in a search box, over REST, from an agent, and saved
for later. The shape is GitHub's, not Jira's:

```
is:open label:infra project:ATH assignee:@me sort:priority-desc payment
```

Space-separated `field:value` atoms, joined by **AND**, with `-` to negate one.
There are no operators, no parentheses, and no precedence — which is the point.
JQL is a programming language you look up; this is a sentence you type, and an
agent composing one from a docstring cannot get precedence wrong because there
is none to get wrong.

## Fields

| Atom | Matches |
|---|---|
| `is:open` | status category is not `done` — per that project's own status set |
| `is:closed` | status category **is** `done` |
| `is:archived` | archived issues (see below) |
| `is:unassigned` | no assignee (same as `assignee:none`) |
| `is:claimed` | someone holds a live lease on it (the lease clock says active) |
| `is:unclaimed` | nobody holds a live lease: no lease, or one that has expired |
| `is:ready` | you could claim it now: to-do or doing, unclaimed, no blocker that is not done (one you cannot see still blocks), and you are its assignee, a delegated contributor, or an admin. Needs a signed-in caller. The same rule as the desk's `work.ready` lane ([DESK.md](DESK.md)) |
| `idle:>Nd` / `idle:<Nd` | more / less than N days since the last recorded **work** (see below) |
| `age:>Nd` / `age:<Nd` | created more / less than N days ago |
| `has:blockers` | at least one issue blocks this one |
| `has:parent` | has a parent issue |
| `has:children` | has at least one child |
| `has:labels` | has at least one label |
| `status:<name>` | exact status name |
| `priority:<name>` | `low` \| `medium` \| `high` \| `urgent` |
| `label:<name>` | has this label (case-insensitive) |
| `project:<id\|KEY\|none>` | in this project, or `none` for the backlog |
| `sprint:<id\|none>` | in this sprint, or `none` for no sprint |
| `assignee:<id\|@me\|none>` | assigned to this user, you, or nobody |
| `sort:<key>` | see below |
| bare words, `"quoted phrases"` | substring of title or body |

Quotes protect a colon. `"Error: timeout"` searches for that phrase rather than
being read as a `field:value` atom — which matters because a colon is ordinary
punctuation in the log lines and error strings an operator is most likely to be
hunting for. A field with a quoted **value** is unaffected, since that colon is
outside the quotes: `assignee:"Ada Lovelace"` is still an assignee filter.

`is:open` and `is:closed` are **category-based**, not name-based. A project whose
done state is called `shipped` behaves correctly, because the query resolves
through the same status-category expression the fleet views use — there is one
definition of "closed" in Athena, and this is not a second one.

## Idle, age and claims

`idle:` answers "what has gone untouched?" It is measured from the last
recorded **work** on the issue, using the same closed verb list as the progress
view: an edit, a status change, a comment or comment edit, an attachment, a
link, a parent set, or a completed claim. When no work was ever recorded it
falls back to the issue's creation time, so a never-touched issue is as idle as
it is old.

- **Not counted as work:** check-ins, heartbeats, lease claims and renewals, and
  triage such as labels, assignment, sprint or priority changes. A run that keeps
  reporting while nothing moves stays idle.
- **Imported history** (rows a registered source back-dated) does not count.

`age:` is plain time since creation. Both take `>Nd` or `<Nd`: one comparison
sign, whole days, at most 36500. Anything else refuses and names the atom.
`idle:14d`, `idle:>14`, `idle:>2w` and `age:>=3d` are all errors, not guesses.

`is:claimed` and `is:unclaimed` follow the lease clock. An expired lease is
unclaimed, and reading never clears it. `is:unclaimed is:open sort:idle-desc` is
the claimable work, longest-waiting first.

Every time-relative atom in one query is measured against one clock, taken when
the query is compiled.

**Cost.** `idle:` and `sort:idle-desc` add one correlated subquery per issue the
rest of the query keeps. It reads that issue's own trail through
`idx_activity_target (target_kind, target_id, id)`, never the whole activity
table; a test pins the query plan.
- Rows: work is bounded by the page `limit`.
- Count: the count endpoint pays it for every issue that passes the other atoms,
  so put cheap atoms (`is:open`, `project:`) beside `idle:` on a large board.

`is:claimed` is one primary-key lookup in `issue_leases`. `age:` is a column
comparison.

## Sorting

`sort:` takes `created`, `id`, `priority`, or `status`, each with `-asc` or
`-desc`, plus `idle-desc`. The default is `id-desc` — newest first, which is
what an operator scanning a list wants. `sort:priority-desc` puts *urgent*
first: priority is ranked, not sorted alphabetically, which would read urgent,
medium, low, high. `sort:idle-desc` puts the longest-idle issue first, by the
same measure as `idle:`.

**There is no `sort:updated`.** The issues table has no `updated_at` column. A
sort key that silently fell back to another ordering would be a lie about the
result, so the gap is recorded here instead of faked.

## Rules worth knowing

- **Repeated fields AND together.** `label:a label:b` means both, like GitHub.
- **Negation wraps the positive form.** `-label:noise` is exactly "not
  `label:noise`", so the two readings cannot disagree.
- **Archived issues are excluded** unless the query says `is:archived` — the
  same default every list applies, so a query and the list it replaces agree
  about what exists.
- **`@me` is the caller.** In a saved filter it means whoever *runs* it, which
  is the reason to save `assignee:@me` rather than a fixed user id. An anonymous
  caller using `@me` gets a refusal, not an empty list: "you have no work" is a
  different claim from "I don't know who you are".
- **You see only what you could always see.** Visibility is composed into the
  SQL, not filtered afterwards, so a bounded page is a full page of visible
  results rather than a partial one with the hidden rows silently removed.

## Unknown atoms are errors, not empty results

```
GET /issues?q=asignee:@me
422 {"detail": {"error": "unknown search field 'asignee'",
                "code": "invalid_query", "atom": "asignee"}}
```

This is the load-bearing rule. A query box that answers "no results" to a typo
has invented an answer — the operator concludes there is no matching work when
really there was a missing `s`. The refusal names the offending atom so it can
be fixed rather than guessed at.

Unknown **values** for open-vocabulary fields are different and legitimately
match nothing: `label:deleted-label` and `status:whatever` have always behaved
that way, and tightening them would break every saved filter naming a label
since removed. Only the closed vocabularies (`is:`, `has:`, `sort:`) and the
closed duration shape (`idle:`, `age:`) refuse a bad value, and their errors list
what would have worked.

## Surfaces

```
GET  /issues?q=<query>&limit=&offset=   # the same endpoint as the structured filters
GET  /issues/query/count?q=<query>      # total behind a bounded page
GET  /issues/query/help                 # the vocabulary, as data
```

`q` and the structured filters (`status=`, `label=`, …) are **mutually
exclusive**, and combining them is a 422 rather than a merge. Silently AND-ing
would make `?q=is:open&status=done` return nothing and look like missing data;
silently preferring one would ignore what the caller asked for.

Over MCP: `search_work(q)`, `count_work(q)`, `query_help()` — all through REST,
so there is no second implementation to drift.

In the browser, `/find` takes the same atoms in its search box, through the same
parser and compiler, so the page and the API agree on what matches.
- **Atoms in the box:** the page lists matching issues in the query's order.
  Pages and comments are not searched by atoms.
- **Empty box, a filter menu set:** a filter-only browse.
- **A typo:** the page answers 422 with the compiler's own error, never "no
  matches".
- **Atoms and filter menus together:** refused, like `?q=` with structured filters.
- **Plain words** (with or without the menus): the ranked full-text search it
  always was.

### One ask across the workspace

```
GET /search/workspace?q=<query>&limit_per_kind=1..25   # issues + pages + comments
```

Over MCP: `search_workspace(query, limit_per_kind)`. For an agent that does not
yet know which module holds the answer, this is the first call — the Office
search box, not a second search engine.

It **composes the two searches that already exist and invents no third**:

| Part of `q` | Where it goes |
|---|---|
| grammar atoms (`is:open`, `label:infra`, …) | the issue query compiler — issues only |
| bare words and quoted phrases | full-text search, across issues, pages, and comments |

Which one applies is decided by **this parser**, not a second notion of
"grammar-shaped": if `parse()` produced terms, issues come from the compiler;
if it produced only text, issues come from full-text search like everything
else. `is:open zebra` filters issues structurally *and* finds the page that says
zebra. A pure-grammar query has no free text, so the page and comment groups are
empty — and the response echoes `query.text` so that silence reads as "nothing
was text-searched" rather than "nothing matched".

Two honesty rules in the response shape:

- **Grouped by kind, never globally ranked.** Two engines with two orders cannot
  be interleaved into one relevance score without inventing it, so the payload
  says `grouped_by_kind: true` and asks you to compare within a group.
- **Every group discloses its bound.** `clipped` is measured — one row past the
  limit is fetched — not inferred from a full page.

An unknown atom is still an error naming the atom, and every group is gated by
the caller's own visibility, so this can never show more than the module
surfaces would.

Saved filters accept a `query` key in their criteria, mutually exclusive with the
structured dimensions for the same reason. A query is validated **when saved**, so
a filter that could never run cannot be stored — otherwise it would fail closed to
an empty list every time, which is the invented answer this whole design avoids.

`/issues/query/help` is emitted from the parser's own vocabulary rather than
restated, so this document, the endpoint, and the MCP docstring cannot drift
from what the parser actually accepts.

## Deliberately not in v1

- **OR and parentheses.** Every atom ANDs. Boolean grouping is where a query box
  becomes a language, and the value it adds for a solo operator is small next to
  the parsing and UI complexity it brings. Revisit with a real need.
- **`is:blocked`** (semantic — blocked by something *not yet closed*), as opposed
  to `has:blockers` (structural — any blocking edge). The semantic version needs
  per-project status categories resolved per blocker row; `dependencies.open_blockers`
  already does this correctly in Python, and reproducing it in the compiler would be
  a second implementation of blocked-ness. `has:blockers` is exact about what it
  means, and the docs say which one this is.
- **A cross-kind grammar.** `/search/workspace` (above) now answers one ask
  across issues, pages, and comments — but by *composition*, not by extending
  this language: the atoms still filter issues only, and pages and comments are
  still reached by full text. `label:infra` will not find a labelled page,
  because a page's labels are not this grammar's vocabulary. Making them so is a
  larger change than a search surface, and it is not this one.
- **Date atoms** (`created:>2026-01-01`). Ranges need a comparison syntax, which
  is the first step toward the operator grammar this deliberately avoids.
