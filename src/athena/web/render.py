"""Inline rendering for page/issue bodies — turns [[issue:N]]/[[page:N]] tokens
into safe HTML links.

This is the *presentation* half of the cross-link feature: core/links.py owns the
token grammar and resolves what a reference points at; this module knows the web
URLs (/aegis/issues/N, /mentor/pages/N) and the markup, which core has no business
owning. Both halves share core.links.REF_RE so the index and the rendered links
can never disagree on what counts as a reference.

XSS rule: the body is author-supplied text, so we escape EVERYTHING first, then
substitute link markup into the already-escaped string. A resolved title is
escaped again before it goes into the anchor. Tokens like [[issue:5]] contain no
HTML-special characters, so they survive escaping intact and the regex still
matches.
"""
from __future__ import annotations

import re
import sqlite3

from markupsafe import Markup, escape

from athena.core import links

# Where each kind is addressable in the web UI. Keys match the resolver's kinds.
_HREF = {"issue": "/aegis/issues/{}", "page": "/mentor/pages/{}"}

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


def render_body(conn: sqlite3.Connection, text: str | None) -> Markup:
    """Render a body to safe HTML with cross-references linked. A reference to a
    real target becomes an <a class="xref">title</a>; a broken one (target not
    created, or deleted) renders the literal token in <span class="xref broken">
    so the author sees it's dangling rather than having it silently vanish.
    Newlines become <br> so plain-text paragraphs survive — this is the single
    safe path for body rendering (the body is escaped first, never trusted raw)."""
    if not text:
        return Markup("")
    escaped = str(escape(text))

    def _link(match) -> str:
        kind, num = match.group(1), int(match.group(2))
        ref = links.resolve_ref(conn, kind, num)
        if ref["exists"]:
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
        # numeric ref.
        ref = links.resolve_key_ref(conn, match.group(1), int(match.group(2)))
        if ref["exists"]:
            href = _HREF["issue"].format(ref["id"])
            label = escape(ref["title"])
            return f'<a href="{href}" class="xref">{label}</a>'
        return f'<span class="xref broken">{escape(match.group(0))}</span>'

    linked = links.REF_RE.sub(_link, escaped)
    linked = links.KEY_REF_RE.sub(_key_link, linked)
    return Markup(linked.replace("\n", "<br>"))
