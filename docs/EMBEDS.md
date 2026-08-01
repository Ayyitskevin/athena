# Live embeds — pages that show real work

A Mentor page can carry a fenced block that renders, at view time, as real data:

````markdown
Sprint status, for anyone reading this runbook:

```athena
kind: issues
q: is:open project:ATH sort:priority-desc
limit: 10
title: Open work
```
````

This is the point where Athena stops being a wiki *next to* a tracker. A runbook
that shows its issue's live state, a project charter that shows what is actually
open, a daily note that shows what needs attention — one page, real data, no
copy-paste that goes stale the moment someone closes something.

## Kinds

| `kind:` | Shows | Keys |
|---|---|---|
| `issues` | a table of matching issues | `q:`, `limit:`, `title:` |
| `count` | how many match, as one number | `q:`, `title:` |
| `issue` | one issue | `issue:` (id or `ATH-12`), `title:` |
| `rollup` | one issue's sub-issue progress, counted live | `issue:` (id or `ATH-12`), `title:` |

`rollup` counts a parent's direct children by status category on every read —
the same computation the issue page draws, so a page and the issue it describes
cannot disagree. Archived children are excluded and the count is stated;
children the reader cannot see are excluded silently, so two readers of one page
can correctly see different totals. See [PLANNING.md](PLANNING.md).

`q:` is the [work query language](QUERY.md) — the same grammar the search box and
MCP use, so a query you can type is a query you can embed.

## The rules that make an embed trustworthy

**Nothing is ever stored.** The page holds the *directive*; the data is resolved
fresh for whoever is looking. A snapshot written into page content would be a
staleness lie the moment the work moved, and a visibility leak the moment someone
else opened the page.

**Visibility is the reader's, never the author's.** An embed written by an admin
renders, for a member, only the issues that member could already see. Two people
opening the same page can legitimately see different rows — that is correct, and
it is why embeds are resolved per request rather than cached per page.

**A single hidden issue is reported exactly like a missing one.** `kind: issue`
naming an issue you cannot see says "no issue matches", the same as a bad id —
otherwise an embed would be an existence oracle for private work.

**Bounded, and honest about it.** At most `limit:` rows (default 10, capped at
50) and 10 directives per page. An over-limit directive is *clamped*, not
refused, and a truncated list says "Showing 10 of 42" — a window presented as the
whole answer is how an operator concludes there are ten open issues when there
are forty-two. Directives past the per-page ceiling render a visible refusal
rather than vanishing.

**A directive that cannot render says so, in place, with the reason.** A bad
query, an unknown kind, a typo'd key — each renders a visible error box. An
embed that silently produced nothing would be indistinguishable from one that
matched nothing, which is the invented answer this design exists to avoid. One
broken directive never breaks the others, and never breaks the page.

**Only ```athena fences are directives.** A ```python block containing
`kind: issues` is code, and stays code.

## How it composes with the sanitizer

Page bodies are untrusted: rendered as Markdown with raw HTML disabled, then run
through a strict sanitizer. Embeds slot into that pipeline the same way `[[ref]]`
tokens do — extracted from the source *before* Markdown, replaced by an opaque
token, and substituted back after sanitizing.

That ordering is not a preference. The sanitizer strips the
`class="language-athena"` that would identify the fence in rendered HTML, so
there is no way to find it afterwards — verified, not assumed. The extraction
approach also has the property that matters: the HTML an embed produces is built
by Athena from escaped values and never passes through the sanitizer as
author-supplied markup at all. An issue titled `<img src=x onerror=...>` arrives
in an embed as text, exactly as it would anywhere else.

The placeholder carries a **per-render random nonce**, so an author cannot write
a literal token into their page and have Athena substitute someone else's embed
into it. There is a test that fails if the nonce becomes predictable.

## Surfaces

```
POST /embeds/resolve   {"text": "...

"}   # resolve directives in any body, as you
GET  /embeds/help                         # kinds, keys, and limits, as data
```

Over MCP: `read_page_embeds(page_id)`, `resolve_embeds(text)`, `embed_help()`.
Agents get **structured rows, not HTML** — markup is a presentation detail, and
an agent asking "what work does this runbook point at" wants data.

`resolve` takes *text* rather than a page id because the import contract makes
Aegis and Mentor peers: a Mentor route may not import the Aegis query engine.
Reading the page is the caller's own already-authorized call, and the MCP client
composes the two so an agent still makes one tool call. That has an independent
benefit — you can resolve a body you are *about* to save and see what it will
show, including which blocks will error, before writing it.

## Deliberately not in v1

- **`kind: board` and `kind: metrics`.** Both are real surfaces already; embedding
  them needs a bounded, per-viewer projection of each, which is its own slice.
- **Embeds in issue bodies.** The renderer is shared, so this is close — but issue
  bodies are edited far more often and by more actors, and the render-budget story
  deserves its own thinking before opening that door.
- **Sorting or filtering an embed from the reading side.** An embed shows what its
  author asked for. Interactive tables are a JavaScript feature, and the no-build
  rule stands.
- **Embeds in exports.** An HTML export should render embeds as their directive
  text plus a visible "not live here" box — a live embed in a static file is stale
  data wearing a live face. That belongs with the export stage.
