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

`is:open` and `is:closed` are **category-based**, not name-based. A project whose
done state is called `shipped` behaves correctly, because the query resolves
through the same status-category expression the fleet views use — there is one
definition of "closed" in Athena, and this is not a second one.

## Sorting

`sort:` takes `created`, `id`, `priority`, or `status`, each with `-asc` or
`-desc`. The default is `id-desc` — newest first, which is what an operator
scanning a list wants. `sort:priority-desc` puts *urgent* first: priority is
ranked, not sorted alphabetically, which would read urgent, medium, low, high.

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
since removed. Only the closed vocabularies (`is:`, `has:`, `sort:`) refuse a
bad value, and their errors list what would have worked.

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
- **Cross-kind queries** over pages and comments. `/search` already spans kinds
  via full-text; unifying that with this grammar is its own stage.
- **Date atoms** (`created:>2026-01-01`). Ranges need a comparison syntax, which
  is the first step toward the operator grammar this deliberately avoids.
