"""Inline rendering for page/issue bodies — Markdown plus [[issue:N]]/[[page:N]]
cross-link tokens, turned into safe HTML.

This is the *presentation* half of the cross-link feature: core/links.py owns the
token grammar and resolves what a reference points at; this module knows the web
URLs (/aegis/issues/N, /mentor/pages/N) and the markup, which core has no business
owning. Both halves share core.links.REF_RE so the index and the rendered links
can never disagree on what counts as a reference.

XSS rule (render_body): the body is author-supplied (and may come from an agent),
so we never trust it. We render Markdown with raw HTML DISABLED (`html=False`), so
an author's `<script>` becomes escaped text rather than live markup, and dangerous
link schemes (javascript:, data:) are dropped by the Markdown link validator. We
then run the rendered HTML through nh3 (a sanitizer with a strict tag/attribute
allowlist) as a second, independent layer, and only afterwards substitute our own
cross-link markup. The `[[issue:5]]`-style tokens contain no Markdown- or
HTML-special characters, so they survive both passes intact and the regex still
matches. `breaks=True` keeps a single newline as a line break, so plain-text
bodies written before Markdown landed still read the way their authors intended.

render_plaintext / render_snippet stay escape-first (no Markdown): comments and
search snippets are short, untrusted, and want literal text, not formatting.
"""
from __future__ import annotations

import re
import sqlite3

from markdown_it import MarkdownIt
from markupsafe import Markup, escape
import nh3

from athena.core import links, users

# One configured Markdown renderer for every body. `html=False` is the security
# linchpin (raw HTML is escaped, not emitted); `breaks=True` preserves authored
# line breaks. The default link validator already rejects javascript:/data: URLs.
_MD = MarkdownIt("commonmark", {"html": False, "breaks": True})

# Where each kind is addressable in the web UI. Keys match the resolver's kinds.
_HREF = {"issue": "/aegis/issues/{}", "page": "/mentor/pages/{}"}

# A mention token [[user:N]] renders as @Name (a plain span — there is no user
# page to link to). Shares the grammar with notifications._MENTION_RE so the thing
# that NOTIFIES and the thing that RENDERS can never disagree on what a mention is.
_MENTION_RE = re.compile(r"\[\[user:(\d+)\]\]")


def _sub_mentions(conn: sqlite3.Connection, html: str) -> str:
    """Replace [[user:N]] tokens in already-safe HTML with @Name spans. An unknown
    id renders as its literal (escaped) token, so a typo is visible, not hidden."""

    def _one(match) -> str:
        user = users.get_user(conn, int(match.group(1)))
        if user is None:
            return str(escape(match.group(0)))
        return f'<span class="mention">@{escape(user["name"])}</span>'

    return _MENTION_RE.sub(_one, html)

# core.search builds snippets with the matched terms wrapped in [..] (its chosen
# delimiters). This pulls a balanced [..] pair out of the ALREADY-escaped snippet
# so we can swap it for <mark>. Non-greedy + balanced, so a lone literal '[' with
# no closing ']' is left as plain text rather than opening a dangling tag.
_SNIPPET_MARK = re.compile(r"\[([^\[\]]*)\]")


def render_snippet(snippet: str | None) -> Markup:
    """Render a core.search snippet to safe HTML with the matched terms in <mark>.

    Same XSS rule as render_body: the snippet is excerpted from author-supplied
    body text, so we escape EVERYTHING first, then substitute markup into the
    already-escaped string. search wraps matches in [..]; we turn those into
    <mark>…</mark>. The match text itself is already escaped, so a body that
    contained <script> shows as inert text inside the highlight."""
    if not snippet:
        return Markup("")
    escaped = str(escape(snippet))
    marked = _SNIPPET_MARK.sub(lambda m: f"<mark>{m.group(1)}</mark>", escaped)
    return Markup(marked)


def render_plaintext(text: str | None) -> Markup:
    """Render untrusted plain text as safe HTML, preserving line breaks."""
    if not text:
        return Markup("")
    return Markup(str(escape(text)).replace("\n", "<br>"))


def render_comment(conn: sqlite3.Connection, text: str | None) -> Markup:
    """Render a comment: escaped plain text (not Markdown — comments are short and
    untrusted) with [[user:N]] mentions turned into @Name and newlines preserved.
    Same escape-first safety as render_plaintext, plus the mention pass."""
    if not text:
        return Markup("")
    escaped = str(escape(text))
    linked = _sub_mentions(conn, escaped)
    return Markup(linked.replace("\n", "<br>"))


def render_body(
    conn: sqlite3.Connection,
    text: str | None,
    *,
    actor: dict | None | object = links._UNGATED,
) -> Markup:
    """Render a body (Markdown) to safe HTML with cross-references linked. A
    reference to a real target becomes an <a class="xref">title</a>; a broken one
    (target not created, or deleted) renders the literal token in
    <span class="xref broken"> so the author sees it's dangling rather than having
    it silently vanish.

    The body is rendered as Markdown with raw HTML disabled, then sanitized with
    nh3, then the cross-link tokens are substituted — the single safe path for body
    rendering (the body is never trusted raw). A token inside a code span/block is
    still linkified (the substitution runs over the rendered HTML); that edge is
    accepted in exchange for keeping one obvious render path.

    `actor` is the viewer the cross-link gate runs against: a token pointing at a
    target the viewer can't see (an issue in a private project / a page in a private
    space) is rendered broken — the same dangling token a deleted target gets — so
    neither the hidden title nor a working link leaks, and the broken form can't be
    told apart from a genuinely missing target (no existence leak). _UNGATED (the
    default, for internal/test callers) waves every reference through."""
    if not text:
        return Markup("")
    # Markdown (raw HTML escaped by html=False), then an independent sanitizer
    # pass. nh3 keeps a strict allowlist of formatting tags and drops anything
    # dangerous; the [[ref]] tokens are plain text, so they pass through to the
    # cross-link substitution below.
    safe = nh3.clean(_MD.render(text))

    def _link(match) -> str:
        kind, num = match.group(1), int(match.group(2))
        ref = links.resolve_ref(conn, kind, num)
        # A ref the viewer may not see is treated exactly like a dangling one:
        # broken, no title, no link — so a hidden target leaks neither its title nor
        # its existence (broken-because-hidden is indistinguishable from
        # broken-because-missing).
        if ref["exists"] and links._visible_ref(conn, actor, kind, num):
            href = _HREF[kind].format(num)
            label = escape(ref["title"])
            return f'<a href="{href}" class="xref">{label}</a>'
        # match.group(0) is the token from already-escaped text; escape again for
        # belt-and-suspenders (it has no special chars, so this is a no-op).
        return f'<span class="xref broken">{escape(match.group(0))}</span>'

    def _key_link(match) -> str:
        # [[ATH-12]] — resolve the project key + number to a concrete issue. A hit
        # links to that issue (by id, the stable address); a miss (unknown key or
        # retired number) renders the literal token as broken, same as a dangling
        # numeric ref. A hit the viewer can't see is treated as a miss (broken),
        # so a private project's issue title/existence never leaks through its key.
        ref = links.resolve_key_ref(conn, match.group(1), int(match.group(2)))
        if ref["exists"] and links._visible_ref(conn, actor, "issue", ref["id"]):
            href = _HREF["issue"].format(ref["id"])
            label = escape(ref["title"])
            return f'<a href="{href}" class="xref">{label}</a>'
        return f'<span class="xref broken">{escape(match.group(0))}</span>'

    linked = links.REF_RE.sub(_link, safe)
    linked = links.KEY_REF_RE.sub(_key_link, linked)
    linked = _sub_mentions(conn, linked)
    return Markup(linked)
