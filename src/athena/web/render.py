"""Inline rendering for page/issue bodies — Markdown plus [[issue:N]]/[[page:N]],
[[KEY-N]], and bare [[Page Title]] cross-link tokens, turned into safe HTML.

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

from collections.abc import Collection
import re
import secrets
import sqlite3
from html import unescape
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from markupsafe import Markup, escape
import nh3

from athena.aegis import embed_data
from athena.core import embeds, links, notifications, users

# One configured Markdown renderer for every body. `html=False` is the security
# linchpin (raw HTML is escaped, not emitted); `breaks=True` preserves authored
# line breaks. The default link validator already rejects javascript:/data: URLs.
_MD = MarkdownIt("commonmark", {"html": False, "breaks": True})

# Where each kind is addressable in the web UI. Keys match the resolver's kinds.
_HREF = {"issue": "/aegis/issues/{}", "page": "/mentor/pages/{}"}

# A mention token [[user:N]] renders as @Name (a plain span — there is no user
# page to link to). Imports notifications.MENTION_RE rather than restating it, so the
# thing that NOTIFIES and the thing that RENDERS cannot disagree on what a mention
# is — they were two identical literals under a comment claiming they were shared.
_MENTION_RE = notifications.MENTION_RE


def _sub_mentions(conn: sqlite3.Connection, html: str) -> str:
    """Replace [[user:N]] tokens in already-safe HTML with @Name spans. An unknown
    id renders as its literal (escaped) token, so a typo is visible, not hidden."""

    def _one(match) -> str:
        user = users.get_user(conn, int(match.group(1)))
        if user is None:
            return str(escape(match.group(0)))
        return f'<span class="mention">@{escape(user["name"])}</span>'

    return _MENTION_RE.sub(_one, html)


# A bare buzz:// permalink — the relay's entity-link grammar: message, issue, pr,
# repo, project — becomes a clickable chip labelled by its entity kind, with the
# full URI in the title. Runs over already-safe HTML like the mention pass, and is
# deliberately NEVER written into the links index: a Buzz entity has no local
# table to resolve against, so backlinks/sync_links stay purely local and
# core/links.py never learns an unresolvable kind. This pass is also the ONE way
# a Buzz permalink stays clickable at all — nh3's scheme allowlist strips
# buzz:// out of a Markdown link destination.
#
# The query class is RFC 3986's `query` production in full (unreserved /
# pct-encoded / sub-delims / ":" / "@" / "/" / "?"), matched against the ESCAPED
# text, so `&` arrives as `&amp;` and is its own alternative rather than a class
# member. A narrower class is not merely incomplete — it TRUNCATES: with `+`
# missing, `?id=cd+ef` linkified only through `cd`, producing an href that
# pointed somewhere other than the URI the author wrote and the title displayed.
# A link that goes to the wrong place is worse than no link, so the class is the
# whole production rather than the characters today's canonical builders happen
# to emit — `relay=` already carries a `wss://…` value with `:` and `/` in it.
#
# `'` is IN the class and `"` is OUT, and the pair is deliberate: those are the
# two characters nh3 leaves RAW in a text node (it escapes only `&`, `<`, `>`),
# so they are the two that actually reach this regex as themselves. `'` is a
# legal sub-delim, so excluding it truncated `?id=a'b` to `?id=a`; `"` is not
# legal in a query and must arrive percent-encoded, so it correctly ENDS the
# match — the same place `_URL_RE` stops. Whatever is matched is re-escaped on
# the way into the attribute, so neither can break out of the href.
#
# The two alternatives have disjoint first characters (`&` is not in the class),
# so the scan is linear — this grammar has a polynomial-ReDoS history.
_BUZZ_QUERY_CHARS = r"A-Za-z0-9\-._~%!$'()*+,;:@/?="
_BUZZ_LINK_RE = re.compile(
    rf"(?<!\w)buzz://(message|issue|pr|repo|project)\?((?:[{_BUZZ_QUERY_CHARS}]|&amp;)+)"
)

# Sentence punctuation that ends a sentence rather than a URI. These are legal
# query characters, so they are matched and then trimmed from the tail — the
# same shape `_URL_RE` / `_URL_TRAILING_PUNCTUATION` use below, so "see
# buzz://message?id=ab." does not put the full stop inside the href.
#
# The VALUE deliberately equals `_URL_TRAILING_PUNCTUATION`: same question, same
# answer, and a test pins the two together so a future edit to one surfaces as a
# failure instead of a silent fork. An earlier draft added `!` and `?` here; both
# are legal query characters, so trimming them re-created the very defect this
# slice exists to close (an href that is not the URI the author wrote).
_BUZZ_TRAILING_PUNCTUATION = ".,;:)"

# Sanitized HTML, split into tag segments and text segments.
#
# A tag is `<`, then any run of (a character that is not a quote or `>`) or a
# WHOLE quoted attribute value, then `>`. Consuming quoted values as units is
# the load-bearing part: a sanitizer does NOT escape `>` inside an attribute
# (it is valid there), so `alt="see > x"` carries a raw `>` and the obvious
# `<[^>]*>` ends the tag early — handing the rest of a live attribute to the
# text branch, which is exactly the injection this split exists to prevent.
#
# The three alternatives start with disjoint character sets, so the scan cannot
# backtrack ambiguously. A lone `<` that begins no well-formed tag matches the
# bare `<` branch and is emitted verbatim: sanitized HTML has no such character
# (stray `<` is escaped to `&lt;`), but the branch means a malformed input is
# passed through untouched rather than silently dropped by the scan.
_HTML_SEGMENT_RE = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>|<|[^<]+""")
_TAG_NAME_RE = re.compile(r"^<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)")


def _sub_buzz_links(html: str) -> str:
    """Linkify bare buzz:// URIs in the TEXT NODES of already-sanitized HTML.

    Walking segments rather than running the regex over the whole string is a
    correctness requirement, not tidiness. This pass emits markup AFTER nh3 has
    sanitized, so anything it writes anywhere but a text node is markup
    injection past the sanitizer — and a guard on the preceding character
    cannot express "not inside a tag". A body like
    ``![see buzz://message?id=ab](https://x/p.png)`` puts an author-controlled
    URI inside the ``alt`` attribute Markdown builds, where substituting an
    anchor injected raw quotes and a ``>`` that closed the ``<img>`` early; the
    link-title variant pushed nh3's own ``rel="noopener noreferrer"`` out of the
    tag and onto the page as visible text.

    Tracking anchor depth in the same walk keeps a permalink that already sits
    inside a link from nesting a second anchor, which browsers resolve by
    closing the outer one — truncating the author's link.
    """

    def _one(match) -> str:
        matched = match.group(0)
        trimmed = matched.rstrip(_BUZZ_TRAILING_PUNCTUATION)
        # `(` and `)` are legal sub-delims, so the trim above can unbalance a URI
        # that legitimately ends in `)` — `?id=a(b)` became the href `?id=a(b`.
        # Give back each `)` that closes a paren still open in the trimmed match,
        # which keeps "(see buzz://message?id=ab)" trimming its sentence paren.
        # Bounded by the trimmed tail, so it is linear like the trim it corrects.
        while (
            trimmed.count("(") > trimmed.count(")")
            and matched[len(trimmed) : len(trimmed) + 1] == ")"
        ):
            trimmed = matched[: len(trimmed) + 1]
        _, _, query = trimmed.partition("?")
        if not query:
            # The whole query was sentence punctuation (``buzz://message?...``),
            # so there is no permalink here — and a link to a query-less URI
            # would point somewhere the author never wrote. Leave it as text.
            return matched
        # The trimmed tail is emitted verbatim beside the link, so trimming can
        # only move characters OUT of the href, never drop them from the page.
        # It comes from already-escaped text, so it is emitted as-is.
        tail = matched[len(trimmed) :]
        uri = unescape(trimmed)
        return (
            f'<a href="{escape(uri)}" class="xref buzz-link" '
            f'title="{escape(uri)}">buzz:{match.group(1)}</a>{tail}'
        )

    out: list[str] = []
    anchor_depth = 0
    for segment in _HTML_SEGMENT_RE.findall(html):
        if segment.startswith("<"):
            tag = _TAG_NAME_RE.match(segment)
            if tag is not None and tag.group(2).lower() == "a":
                anchor_depth = (
                    max(0, anchor_depth - 1) if tag.group(1) else anchor_depth + 1
                )
            out.append(segment)
            continue
        out.append(segment if anchor_depth else _BUZZ_LINK_RE.sub(_one, segment))
    return "".join(out)


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


#: A bare http(s) URL inside an activity detail. ONE unambiguous quantifier on
#: purpose: the earlier form ended with a second character class that was a
#: SUBSET of the repeated one (`[^\s<>"']+[^\s<>"'.,;:)]`), so every character
#: could be matched by either part and a long run backtracked quadratically —
#: a polynomial-ReDoS shape on text an outside system supplied (forge details
#: are stranger-controlled bytes; see FORGE.md). Sentence punctuation is now
#: trimmed AFTER matching, which is the same rendering with linear scanning.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

#: Punctuation that ends a sentence rather than a URL, stripped from the tail of
#: a match so "see https://x/y." does not put the full stop inside the href.
_URL_TRAILING_PUNCTUATION = ".,;:)"


def render_forge_detail(text: str | None, hosts: Collection[str]) -> Markup:
    """Render an activity detail, linking URLs that belong to a REGISTERED source.

    A forge event's detail carries a commit or pull-request URL, and it is far
    more useful as a link. But the detail is text an outside system supplied, so
    linking every URL in it would let anyone holding a source secret plant an
    arbitrary outbound link on an issue's trail.

    The host allowlist is what makes this safe: a URL renders as an anchor only
    when its host is one the operator registered a source for. Everything else —
    including a URL on a *plausible* host nobody registered — stays inert text.
    Escaping happens first and the anchor is built from the escaped value, so this
    can only ever turn safe text into a link, never introduce markup.
    """
    if not text:
        return Markup("")
    allowed = {host.strip().lower() for host in hosts if host}
    escaped = str(escape(text))
    if not allowed:
        return Markup(escaped.replace("\n", "<br>"))

    def _link(match: re.Match[str]) -> str:
        matched = match.group(0)
        url = matched.rstrip(_URL_TRAILING_PUNCTUATION)
        # The trimmed tail is returned verbatim beside the link, so trimming can
        # only ever move characters OUT of the href, never drop them from the page.
        tail = matched[len(url) :]
        if not url.partition("://")[2]:  # nothing left but the scheme
            return matched
        try:
            host = urlparse(url).hostname
        except ValueError:  # a malformed authority is not a link
            return matched
        if not host or host.lower() not in allowed:
            return matched
        # noopener/noreferrer: the trail must not hand a third-party page a
        # window handle or the URL of the issue someone was reading.
        return (
            f'<a href="{url}" class="xref" rel="noopener noreferrer nofollow" '
            f'target="_blank">{url}</a>{tail}'
        )

    return Markup(_URL_RE.sub(_link, escaped).replace("\n", "<br>"))


def render_comment(conn: sqlite3.Connection, text: str | None) -> Markup:
    """Render a comment: escaped plain text (not Markdown — comments are short and
    untrusted) with [[user:N]] mentions turned into @Name and newlines preserved.
    Same escape-first safety as render_plaintext, plus the mention pass."""
    if not text:
        return Markup("")
    escaped = str(escape(text))
    linked = _sub_mentions(conn, escaped)
    return Markup(linked.replace("\n", "<br>"))


def _extract_embeds(
    text: str,
    embed_results: list[dict] | None,
    *,
    show_refused_directives: bool = False,
) -> tuple[str, dict[str, str]]:
    """Replace each ```athena block with an opaque token, returning the rewritten
    source and the token→HTML map.

    ``embed_results`` comes from ``aegis.embed_data.resolve_body`` — the caller
    resolves, because resolution needs a database and a viewer and this module is
    the presentation layer. When it is None (an internal caller that did not
    resolve), directives render as a plain notice rather than vanishing: a reader
    must never be shown a page with a silent hole where an embed was.
    """
    found = embeds.find_directives(text)
    if not found:
        return text, {}
    nonce = secrets.token_hex(8)
    placeholders: dict[str, str] = {}
    for index, (whole, _body) in enumerate(found):
        token = f"athenaembed{nonce}x{index}"
        if embed_results is not None and index < len(embed_results):
            html = _embed_html(embed_results[index])
        elif show_refused_directives:
            # An export is a SNAPSHOT. A live embed must be visibly dead in it —
            # otherwise it is stale data wearing a live face — but simply
            # deleting it would lose what the author actually wrote. So the
            # refusal carries the directive it came from: a reader sees both
            # that nothing was resolved and exactly what would have been.
            html = (
                '<div class="embed embed-error"><div class="embed-message">'
                "Not rendered here — this embed is live in Athena and cannot be "
                "resolved in an exported copy.</div>"
                f'<pre class="embed-directive">{escape(whole.strip())}</pre></div>'
            )
        else:
            html = (
                '<div class="embed embed-error"><div class="embed-message">'
                "Embed not rendered here.</div></div>"
            )
        placeholders[token] = html
        text = text.replace(whole, token, 1)
    return text, placeholders


def _embed_html(resolved: dict) -> str:
    """Render one resolved embed to HTML.

    Every value here is escaped by this function; nothing author-supplied is
    emitted as markup. That is why the embed HTML is substituted AFTER the
    sanitizer rather than passing through it — there is no untrusted markup in it
    to sanitize, and running it through nh3 would strip the structure it needs.
    """
    title = resolved.get("title") or ""
    head = f'<div class="embed-title">{escape(title)}</div>' if title else ""

    error = resolved.get("error")
    if error:
        # Visible, in place, with the reason. An embed that silently rendered
        # nothing would be indistinguishable from one that matched nothing.
        return (
            f'<div class="embed embed-error">{head}'
            f'<div class="embed-message">Embed did not render: {escape(error)}</div>'
            f"</div>"
        )

    kind = resolved.get("kind")
    if kind == "count":
        return (
            f'<div class="embed embed-count">{head}'
            f'<span class="embed-number">{escape(str(resolved["matched"]))}</span>'
            f'<span class="embed-query">{escape(resolved.get("query") or "")}</span>'
            f"</div>"
        )

    if kind == "issue":
        return (
            f'<div class="embed embed-issue">{head}{_issue_row(resolved["item"])}</div>'
        )

    if kind == "rollup":
        rollup = resolved["rollup"]
        if not rollup["total"]:
            # "No sub-issues" would be false when every one of them is archived:
            # there IS work here, and all of it was set aside.
            body = (
                f'<div class="embed-message">Every sub-issue is archived '
                f"({escape(str(rollup['archived_excluded']))}), so there is no "
                f"live progress to report.</div>"
                if rollup["archived_excluded"]
                else '<div class="embed-message">No sub-issues to roll up.</div>'
            )
        else:
            # Widths arrive already computed, from the same rollup the issue page
            # draws — neither surface does the arithmetic, so neither can drift.
            # The width is a stepped class, not an inline style: style-src carries
            # no 'unsafe-inline' (see main.content_security_policy), and an embed is
            # rendered here rather than in a template, so it has no nonce to use.
            # Whole percent only — the exact count rides in the title, and a
            # one-percent step is visually identical on a bar.
            segments = "".join(
                f'<span class="rollup-seg rollup-{segment["bucket"]} '
                f'rollup-w-{round(segment["percent"])}"'
                f' title="{escape(str(segment["count"]))} '
                f'{escape(segment["bucket"])}"></span>'
                for segment in rollup["segments"]
            )
            archived = ""
            if rollup["archived_excluded"]:
                noun = (
                    "child is" if rollup["archived_excluded"] == 1 else "children are"
                )
                archived = (
                    f" · {escape(str(rollup['archived_excluded']))} archived "
                    f"{noun} not counted"
                )
            body = (
                f'<div class="rollup"><div class="rollup-bar">{segments}</div>'
                f'<div class="embed-message">{escape(str(rollup["percent_done"]))}% '
                f"done — {escape(str(rollup['done']))} of "
                f"{escape(str(rollup['total']))} sub-issues{archived}</div></div>"
            )
        return (
            f'<div class="embed embed-rollup">{head}'
            f"{_issue_row(resolved['item'])}{body}</div>"
        )

    rows = "".join(_issue_row(item) for item in resolved.get("items", []))
    if not rows:
        rows = '<div class="embed-message">No issues match.</div>'
    footer = ""
    if resolved.get("truncated"):
        # Say what was left out. A bounded window presented as the whole answer
        # is how an operator concludes there are ten open issues when there are
        # forty-two.
        footer = (
            f'<div class="embed-more">Showing {escape(str(resolved["shown"]))} '
            f"of {escape(str(resolved['matched']))}</div>"
        )
    return f'<div class="embed embed-issues">{head}{rows}{footer}</div>'


def _issue_row(item: dict) -> str:
    key = item.get("key") or f"#{item['id']}"
    assignee = item.get("assignee_name")
    who = (
        f'<span class="embed-assignee">{escape(assignee)}</span>'
        if assignee
        else '<span class="embed-assignee muted">unassigned</span>'
    )
    return (
        f'<div class="embed-row">'
        f'<a href="{_HREF["issue"].format(item["id"])}" class="embed-key">'
        f"{escape(key)}</a> "
        f'<span class="embed-issue-title">{escape(item["title"])}</span> '
        f'<span class="embed-status">{escape(item["status"])}</span> '
        f'<span class="embed-priority">{escape(item["priority"])}</span> {who}'
        f"</div>"
    )


def render_body(
    conn: sqlite3.Connection,
    text: str | None,
    *,
    actor: dict | None | object = links._UNGATED,
    embed_results: list[dict] | None = None,
    show_refused_directives: bool = False,
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

    # Lift ```athena directives out BEFORE Markdown, leaving an opaque token in
    # their place. This is not a style choice: the sanitizer strips the
    # `class="language-athena"` that would identify the block afterwards, so
    # there is no way to find it in the rendered HTML. The token is alphanumeric,
    # so it survives Markdown and nh3 untouched — exactly how the [[ref]] tokens
    # already do — and it carries a per-render random component, so an author
    # cannot write a literal token into their page and have Athena replace it
    # with someone else's embed.
    text, placeholders = _extract_embeds(
        text, embed_results, show_refused_directives=show_refused_directives
    )

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

    def _title_link(match) -> str:
        # [[Page Title]] — a bare wiki-link. Resolve the title to a concrete page (the
        # source-space preference only applies at index time, not to a free-standing
        # render), link it live, or render the literal token broken. A hit the viewer
        # can't see is treated as a miss (broken), so a private space's page never leaks
        # its title/existence through a title link — the same gate the typed refs use.
        # This pass runs over already-escaped HTML, so a title like "Q&A" arrives here as
        # "Q&amp;A". Undo the entity escaping so the lookup key matches the raw title the
        # index recorded (the token grammar already excludes <>, so & is the only entity
        # that reaches this point). Used ONLY as a DB lookup key, never re-emitted raw.
        inner = unescape(match.group(1))
        # A reserved token that its own pass left as a literal (a broken [[page:404]] /
        # [[ATH-99]] / unknown [[user:9]], now sitting as text inside a broken span or
        # mention) must NOT be re-read as a title. Leave it exactly as that pass rendered
        # it.
        if links._is_reserved_ref(inner):
            return match.group(0)
        ref = links.resolve_title_ref(conn, inner)
        if ref["exists"] and links._visible_ref(conn, actor, "page", ref["id"]):
            href = _HREF["page"].format(ref["id"])
            label = escape(ref["title"])
            return f'<a href="{href}" class="xref">{label}</a>'
        # The literal token is emitted WITHOUT re-escaping: unlike the typed/key refs,
        # a title can carry a `&` (rendered as "&amp;"), and match.group(0) is already
        # the escaped-safe substring from `linked` — re-escaping it would double-escape
        # the entity ("Q&amp;A" → "Q&amp;amp;A"). The <> exclusion in TITLE_REF_RE means
        # this substring can never contain raw markup, so emitting it as-is is safe.
        return f'<span class="xref broken">{match.group(0)}</span>'

    linked = links.REF_RE.sub(_link, safe)
    linked = links.KEY_REF_RE.sub(_key_link, linked)
    linked = _sub_mentions(conn, linked)
    # Title wiki-links resolve LAST, over what the typed/key/mention passes left behind:
    # by now every reserved token is either linked markup (no [[…]] left) or a rendered
    # literal the guard above skips, so this pass only ever sees genuine [[Title]] text.
    linked = links.TITLE_REF_RE.sub(_title_link, linked)
    linked = _sub_buzz_links(linked)
    # Embeds go in LAST, after every substitution pass and after the sanitizer.
    # Their HTML is built entirely by this module from escaped values, so there is
    # no untrusted markup in it to sanitize — and passing it through nh3 would
    # strip the structure it needs. A directive nested inside a quoted block is
    # therefore rendered as an embed exactly where the author put it, and one
    # inside a NON-athena code fence was never extracted at all.
    for token, html in placeholders.items():
        # Markdown wraps a bare token in its own paragraph; replace the whole
        # paragraph so a block-level embed is not nested inside a <p>.
        linked = linked.replace(f"<p>{token}</p>", html).replace(token, html)
    return Markup(linked)


#: The ceiling on text a preview will render, matching the embed resolver's own
#: bound. A preview renders arbitrary unsaved text on every keystroke, so it
#: needs the same limit the saved path has.
MAX_PREVIEW_CHARS = 200_000


# --- Per-surface rendering --------------------------------------------------
#
# Each surface renders a body ONE way, and that way is named here so display and
# preview cannot drift. R-1's whole promise is that what an author sees while
# writing is what readers get; the way to keep that true is not discipline but
# structure — a single function per surface, called by both.


def render_page_body(
    conn: sqlite3.Connection, text: str | None, *, actor: dict | None
) -> Markup:
    """A Mentor page body, exactly as the page view renders it.

    Embeds resolve here, per request and against THIS viewer — never cached,
    because a page-keyed cache would serve one reader's visibility to another.
    """
    return render_body(
        conn,
        text,
        actor=actor,
        embed_results=embed_data.resolve_body(conn, text, actor=actor),
    )


def render_issue_body(
    conn: sqlite3.Connection, text: str | None, *, actor: dict | None
) -> Markup:
    """An issue body, exactly as the issue view renders it.

    Embeds are deliberately NOT resolved on issues (EMBEDS.md defers them), so a
    directive in an issue body renders its "not rendered here" box. The preview
    shows that same box rather than a live embed: a preview that rendered
    something the issue page will not is the drift this pairing exists to
    prevent. When issue embeds ship, both surfaces gain them from this one line.
    """
    return render_body(conn, text, actor=actor)
