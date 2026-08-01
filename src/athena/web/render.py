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

from athena.core import embeds, links, users

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


#: A bare http(s) URL inside an activity detail. Kept narrow on purpose: the
#: trailing character class excludes the punctuation that usually ends a sentence,
#: so "see https://x/y." does not swallow the full stop into the href.
_URL_RE = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:)]")


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
        url = match.group(0)
        try:
            host = urlparse(url).hostname
        except ValueError:  # a malformed authority is not a link
            return url
        if not host or host.lower() not in allowed:
            return url
        # noopener/noreferrer: the trail must not hand a third-party page a
        # window handle or the URL of the issue someone was reading.
        return (
            f'<a href="{url}" class="xref" rel="noopener noreferrer nofollow" '
            f'target="_blank">{url}</a>'
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
    text: str, embed_results: list[dict] | None
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
            segments = "".join(
                f'<span class="rollup-seg rollup-{segment["bucket"]}" '
                f'style="width: {segment["percent"]}%"'
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
    text, placeholders = _extract_embeds(text, embed_results)

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
