
# Editing and leaving

Two things a knowledge tool has to get right before anything else matters:
writing in it should feel good, and nothing you put in should be trapped. This
is what Stage R added — a preview that cannot lie to you, drafts that survive a
closed laptop, images that display, and a way out that needs no Athena.

- [Preview](#preview)
- [Drafts](#drafts)
- [Two people, one page](#two-people-one-page)
- [Inline images](#inline-images)
- [HTML export](#html-export)
- [Limits, stated](#limits-stated)

## Preview

The page and issue editors show a live preview beside the text. It updates as
you type, and it is rendered by **the same function the view itself uses** —
`render_page_body` for a page, `render_issue_body` for an issue. That is the
whole design: two renderers would have drifted the first time one of them
learned something the other did not, and the drift would only have surfaced
after publishing.

Two consequences worth knowing:

- The preview is rendered **as you**. Cross-links and embeds resolve per reader,
  so a link to something you cannot see is dead in your preview exactly as it
  would be on the page. Someone else previewing the same text may correctly see
  something different.
- The preview inherits each surface's limits, including the awkward one. Embeds
  are not resolved on issues (see [EMBEDS.md](EMBEDS.md)), so an embed directive
  in an issue previews as its "not rendered here" box. Showing a live embed
  there would promise something saving the issue does not deliver.

The editor works without JavaScript. The preview simply stays empty, and the
form saves exactly as it always did.

## Drafts

Page editors autosave. If your browser crashes, your laptop closes, or you hit
Cancel by mistake, your text is still there when you come back.

**A draft is user-private state, not content.** That sentence decides everything
about how it behaves:

| A draft… | …because |
|---|---|
| lives in its own table, never in `pages` | every write to page content cuts a version, and autosave must not |
| records **no** activity event | autosave every few seconds would drown the trail it exists to inform |
| is visible only to its author — not admins, not space members | unfinished thinking is not a document |
| is not in portability exports | the same reason saved filters and watches are not |
| never becomes the page by itself | only an explicit Save, through the ordinary audited command, turns text into content |

The editor **offers** a draft, it never applies one. The form still shows the
saved page; *Restore draft* re-renders the form with your text, which is a read
— nothing is written until you press Save, so the button cannot lose anything.
*Discard draft* removes only your copy. Saving makes your text the page and then
drops the draft, so the next "you have unsaved work" means it.

Two people can draft the same page at once and neither blocks the other. That
means a draft can fall behind: if someone else saved while you were typing, the
editor says so and warns that restoring will drop their changes when you save.
Athena will not resolve that for you — it will not let it happen silently
either.

## Two people, one page

Two browsers can open the same page. Until recently the second save simply won,
and the first author's work vanished with no notice and no trace.

The edit form now carries the page's ETag as it was **rendered**, and the save
compares it inside the same write lock the edit runs in — so two editors holding
the same tag cannot both pass. The loser's save is refused with a `409`, and the
refusal is where the design actually lives:

- **Nothing is overwritten.** That is what the refusal means.
- **Nothing is merged.** Athena will not claim to have resolved something a
  person has to read to resolve. There is no three-way merge, no conflict
  markers, and no "we combined these for you".
- **Nothing you typed is thrown away.** Your text is written to your own draft.
  A bare `412` page would leave your work living only in a browser buffer, and
  "it is still in the form" stops being true the moment you navigate.

The form then re-renders showing **their** version — the page as it now stands —
with **your** version displayed beside it, and one click (`Restore draft`) to put
yours back in the fields. You reconcile; the tool reports.

Your draft is recorded against the baseline you were editing *from*, not the
page's new tag, so it stays marked stale and the warning survives you closing the
tab. And it is *your* draft: drafts are owner-scoped personal state, so a
conflict never publishes one editor's unsaved text into another's editor.

Two deliberate softenings, both because the hidden field is a concurrency aid and
not an authorization check:

- a form rendered **before this field existed** (a tab left open across the
  upgrade) sends no tag, and keeps the old last-write-wins behavior rather than
  being refused over something its author cannot see or fix;
- a **malformed** tag is treated as no precondition at all, rather than becoming a
  wall between an author and their own page.

REST and MCP are unchanged: they have always supported `If-Match`, and an agent
that omits it has always been able to overwrite. This closes the browser gap.

## Inline images

`![alt](/attachments/12)` renders inline on pages and issues, served through the
ordinary visibility-gated attachment route: an image on a page you cannot see is
as unreachable as the page. Image attachments show a thumbnail and the exact
markdown that embeds them, so the feature is visible rather than folklore.

What is served inline is decided by **the bytes, not the upload's claim**. An
uploader's content type can be absent, wrong, or a lie, so Athena sniffs the
magic bytes: a real PNG, JPEG, GIF, or WebP is recorded as itself and served
`Content-Disposition: inline`, and anything merely *claiming* one of those types
is recorded as an opaque download. SVG is deliberately excluded from inline
rendering — an SVG is a document that can carry script, and serving one inline
is how an upload becomes stored XSS. `X-Content-Type-Options: nosniff` applies
throughout.

## HTML export

**Export HTML** on any space downloads it as **one self-contained file**. No
server, no stylesheet to fetch, no image that only resolves against a running
Athena — images are inlined as data URIs. JSON portability already answers "can
a machine take my data"; this answers "can a person read it in five years with
nothing but a browser".

It renders through the same renderer the pages use, so it cannot drift from what
readers saw. And it is honest about being a snapshot:

- **Embeds are visibly dead.** An embed resolves against a viewer and a moment;
  frozen into a file it would be stale data wearing a live face. Each one
  exports as a refusal box **carrying the directive it came from**, so a reader
  sees both that nothing was resolved and exactly what the author wrote.
- **It contains what you could see** when you asked for it, and says so.
- **It names what it left out** — a skipped image is replaced by a note giving
  its filename and size, and the footer counts any pages beyond the ceiling.

## Limits, stated

- Preview and drafts are bounded at 200,000 characters — the same ceiling the
  embed resolver uses. A body beyond that saves but does not preview or draft.
- Drafts exist for **pages**, not issues. One row per (page, author); saving
  again overwrites it, so a draft is a position, not a history.
- Staleness is detected from the page's ETag, and is surfaced rather than
  enforced. A draft is never discarded for being stale — it is your work.
- An export stops at 500 pages, skips any single image over 2 MB, and stops
  inlining once the file reaches 32 MB. Every one of those is reported in the
  file itself.
- Export is per **space** (a page-subtree root is supported by the builder but
  is not yet exposed as a link), and it is a browser download — there is no
  `athena-export-html` CLI yet.
- An exported file is not re-importable. It is the human-readable exit; JSON
  portability remains the machine-readable one.

## What none of this claims

A preview is not a save. A draft is not a version, an approval, or a lock — it
never appears in history, and holding one grants no claim over the page. An
export is not a backup and not a live mirror: it is a snapshot of what one
person could see at one moment, and it says so on its face.
