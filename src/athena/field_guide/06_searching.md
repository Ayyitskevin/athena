When you do not know which module holds the answer, do not guess — ask once.

```
search_workspace("zebra")                 # MCP
GET /search/workspace?q=zebra
```

It answers across **issues, pages, and comments** in one call, with your own
visibility applied to each group.

## The work query grammar works here

```
search_workspace("is:open label:infra zebra")
```

Atoms (`is:`, `label:`, `project:`, `assignee:`, …) filter **issues**
structurally. The bare words in the same query go to full-text search across all
three kinds. So that query narrows the work *and* still finds the page that says
zebra.

Two consequences worth holding onto:

- A **pure-grammar** query has no free text, so the page and comment groups come
  back empty. The response echoes `query.text` so you can see that nothing was
  text-searched — that is not "no matches".
- The grammar is **issue-only**. `label:infra` will not find a labelled page.
  Pages and comments are reached by text, not by atoms.

## Two honesty rules in the answer

**Grouped by kind, never globally ranked.** Two search engines with two orders
cannot be interleaved into one relevance score without inventing it, so the
payload says `grouped_by_kind: true`. Compare within a group; do not read
"first result overall" into it, because there is no such thing here.

**Every group tells you when it was cut.** `clipped` is measured, not guessed.
If it is true, there is more — raise `limit_per_kind` (up to 25) or narrow the
query.

## An unknown atom is an error

`labl:infra` gets a `422` naming the atom, not an empty result set. This is
deliberate: an empty list would teach you the label does not exist, when what
actually happened is that you typed the field name wrong.

If the thing you were looking for is a space your fleet shares, subscribe to it
rather than searching it again tomorrow: [[Watching shared memory]].

Deeper: `docs/QUERY.md`.
