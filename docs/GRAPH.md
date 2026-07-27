# The knowledge graph

Athena has stored a link graph since migration 0012. Every `[[page:7]]`,
`[[ATH-12]]`, and `[[Page Title]]` an author writes becomes a row in `links`,
and backlinks have always answered "what points at this?".

This document covers what Stage O added on top: finding the edges that *should*
exist, seeing the neighbourhood an item sits in, and the two page-creation
habits — templates and a daily note — that make the graph grow without ceremony.

- [Unlinked mentions](#unlinked-mentions)
- [The graph view](#the-graph-view)
- [Templates](#templates)
- [The daily note](#the-daily-note)
- [Limits, stated](#limits-stated)

Every surface here is reachable from **Connections**, linked under "Referenced
by" on any page or issue.

## Unlinked mentions

A wiki's graph is only as good as the discipline of whoever wrote the prose.
Someone types "see the Fleet operating guide" and moves on; the page never learns
it was referenced.

**Unlinked mentions** are documents whose text names a thing without linking to
it. A page is named by its **title**; an issue is named by its **key**
(`ATH-12`), never its title — issue titles are sentences ("Fix the login
redirect") that recur in prose constantly, and matching them would bury you in
false positives. A backlog issue has no key, so it has no mention text at all,
and the surface says so rather than showing an empty list that reads as "nothing
mentions this".

Finding a mention **proposes** an edge. It never creates one. "Link it" rewrites
the *source* document's body through the ordinary page or issue command, so the
edge arrives attributed, versioned, and on the activity trail like any other
edit. Nothing is ever auto-linked: an index that silently rewrote bodies would
make the graph untrustworthy in exactly the way this feature exists to fix.

### What counts as a mention

Two rules, both deliberate:

**Code is not prose.** An occurrence inside a fenced block or an inline code span
is not a mention. It is a literal — a command, a filename, a sample — and
linkifying it would corrupt the literal while producing a link that has no
business inside a code block. An unterminated fence swallows the rest of the
document, exactly as a Markdown renderer treats it, so the two agree on what is
code. A **blockquote**, by contrast, *is* prose: a quote is still someone talking
about the thing.

**An attempted link is not a mention.** Text already inside a `[[...]]` token is
skipped even when it produced no row in `links` — an ambiguous title resolves to
nothing. The author already tried; offering to "link it" would splice `[[` inside
`[[`. Text inside a Markdown link target (`](...)`) is skipped for the same
reason.

A mention also never fires mid-word: `Fleet` does not match `Fleetwood`, and
`ATH-12` does not match `ATH-123`. A needle whose own edge is punctuation (`Q&A`,
`v2.0`) still matches, because the word boundary is applied only on the side that
needs it.

### How a suggestion is found

Full-text search **narrows**; Python **confirms**. `search` matches prefix tokens
ANDed together, so "Fleet operating guide" also hits a document containing those
three words scattered across three paragraphs. Every candidate is re-checked
against the real body for a genuine, non-code, not-already-linked occurrence.
Returning an unconfirmed hit would be inventing a mention.

Visibility is the **viewer's**. Candidates come from a search with your actor
applied, so a private project's issue mentioning a public page never surfaces to
someone who cannot see that issue. This matters more here than in an ordinary
list: a mention suggestion would otherwise leak the existence *and the wording*
of private work through the back door of a public page's sidebar.

### Taking a suggestion

```
POST /pages/{id}/link-mention   {"target_kind": "page", "target_id": 41}
POST /issues/{id}/link-mention  {"target_kind": "page", "target_id": 41}
```

The id in the path is the **source** — the document being edited. That is why the
endpoint lives on the source's own domain: a mention of an issue inside a page is
a *page* edit.

The token written in is the one a human would have typed: `[[Fleet operating
guide]]` for a page with a unique title, `[[ATH-12]]` for a keyed issue, and the
numeric `[[page:41]]` when a title is ambiguous — because an ambiguous
`[[Title]]` resolves to nothing and would record no link at all.

If the mention is gone by the time you click — the body changed underneath you —
the request is **refused with 409**, not applied elsewhere. Editing anyway would
rewrite text you never saw.

## The graph view

`GET /pages/{id}/graph` · `GET /issues/{id}/graph` · **Connections** in the
browser.

An **ego graph**: one focus, a bounded radius, a hard node ceiling. Not a global
force-directed view of the whole workspace — that is the feature everyone demos
and nobody uses, it is slow, it is a hairball, and its cost grows with the
database.

- **Undirected adjacency.** For "what neighbourhood am I in", an inbound
  reference and an outbound one are the same edge.
- **Deterministic layout.** Concentric rings, pure arithmetic over a stable
  `(kind, id)` ordering. No randomness, so no seed to fix and no "mostly stable"
  caveat — the same graph renders identically every time.
- **Breadth-first discovery.** When the ceiling bites, it keeps the nodes
  *closest* to the focus, and reports `showing N of M`. An unlabelled partial
  graph reads as the whole neighbourhood, which is the quiet lie the count exists
  to prevent.
- **Visibility during traversal, not after.** A node you cannot see is not a node
  and does not conduct a path. Filtering a finished graph would leak structure:
  the hidden node's edges would still shape the picture, and a gap at a known
  position would be an existence oracle.
- **No ghosts.** `links` resolves lazily, so an edge can point at something that
  never existed or has since been deleted. Those are not drawn.

The browser draws it as **server-rendered SVG with no JavaScript**. Each node is
a real `<a>`, so the graph is keyboard-navigable and works in a text browser.

Defaults: depth 2 (max 3), 40 nodes (max 120).

## Templates

**A template is a page carrying the `template` label.** There is no template
table and no `is_template` column, and that is a deliberate choice against
schema:

- A template *is* a page — written, versioned, commented on, linked to, archived
  exactly like one. A separate table would have to re-grant every one of those.
- Marking one is already an audited, reversible, attributed write, because the
  label commands emit their own activity events. A new column would have needed a
  new command, a new verb, and a new undo compensator to reach the same standard.
- "Which pages are templates" is a *query*, and Athena already answers it.

The honest cost: a workspace already using a label called "template" for
something else will see those pages offered as templates. The label is the
marker, so that is a naming collision, not a data problem — detach the label and
the page stops being one.

Templates are listed per space (`GET /spaces/{id}/templates`), because a template
is offered where it is used. Creating from one copies the **body** and never the
labels — inheriting them would carry `template` across and make every page
created from a template a template itself, a self-replicating menu.

Substitution is `{{title}}` and `{{date}}`, and nothing else. A template language
is a programming language, and a page body is not a place to run one. An unknown
`{{...}}` is left exactly as written rather than erroring or blanking: it is
content, and the author may have meant it literally.

## The daily note

One button on a space: **Open today's note**. It finds or creates the page titled
with today's date, seeded from that space's `Daily Note Template` if it has one.

- **Idempotent by construction.** The lookup and the insert share one
  `BEGIN IMMEDIATE` transaction, so two concurrent first-visits serialize and the
  second finds the first's page. A daily note is exactly the surface a
  double-click or a prefetching browser hits twice.
- **Revisiting writes nothing.** No budget charge, no event. Visiting an existing
  note is a read; stamping `page_created` on every visit would turn a morning
  habit into audit noise and make the trail lie about when the page came to be.
  The REST endpoint returns **201** when it created the page and **200** when it
  found one, so a caller can tell the difference.
- **A space with no template still gets notes** — they simply start empty. The
  feature must not require setup you have not done yet.
- A page merely *named* `Daily Note Template` is not one. The label is the only
  rule; two rules for the same thing is how drift starts.

Paired with [live embeds](EMBEDS.md), the daily template is where the morning
page earns its keep — an embed directive in the template means every day's note
opens showing live work rather than a stale copy:

````markdown
# {{date}}

## Needs attention

```athena
kind: issues
q: is:open assignee:@me sort:priority-desc
limit: 10
```
````

## Agents get data, not markup

Over MCP:

| Tool | What it does |
|---|---|
| `link_graph(kind, id, depth, max_nodes)` | The neighbourhood as positioned nodes and edges |
| `unlinked_mentions(kind, id)` | Documents naming this without linking to it |
| `link_mention(source_kind, source_id, target_kind, target_id)` | Take one suggested edge |

All three go through REST, so there is no second traversal to drift — the exact
failure the shared-command rule exists to prevent, and one that would stay
invisible until an agent and a browser disagreed about what links to what. There
is a test asserting the MCP and REST payloads are identical.

The useful pattern for an agent: after writing a page, ask
`unlinked_mentions` for it and connect the documents that already talk about it,
instead of hoping a human links it later. Reads are open; `link_mention` is an
ordinary page or issue edit and needs the matching write scope.

## Limits, stated

- **Dates are UTC.** Athena stores no per-user timezone, and everything else
  durable here is UTC (`datetime('now')`, the automation schedules, the activity
  trail). An operator west of Greenwich rolls over to a new daily note before
  their own midnight. Written down rather than papered over with a guess.
- **Mentions and the graph are their own route**, not panels on the page itself.
  A mention scan is a full-text query plus a body read per candidate, and a graph
  is a breadth-first walk with a visibility check per node. Putting either on
  every page view would tax reading — the thing people do most — to serve the
  thing they do occasionally.
- **One mention per click.** "Link it" rewrites the *first* real occurrence. A
  document naming the same thing five times needs five clicks, deliberately:
  bulk-rewriting a body from a list the operator skimmed is how you get an edit
  nobody reviewed.
- **No global graph**, and no cross-workspace view. Bounded ego graphs only.
- **No template variables beyond title and date**, and no per-space default
  template other than the daily one.
- **Comments are not mention sources.** They are indexed for search, but "link
  it" would have no body to edit through a page or issue command, so offering one
  would be an offer Athena cannot honor.
