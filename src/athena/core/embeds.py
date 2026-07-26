"""Live embed directives — the grammar for a page that shows real work.

A Mentor page can carry a fenced block that renders, at view time, as real data:

    ```athena
    kind: issues
    q: is:open project:ATH sort:priority-desc
    limit: 10
    ```

This module owns the *grammar* only: finding directives in a body and parsing
them into a validated :class:`Directive`. It is pure — no database, no domain
imports, no HTML — for the same reason `work_query` is: `core` may not import
`aegis`, the parse is worth testing without fixtures, and one parse result then
drives both the HTML render and the structured MCP read.

**Nothing is ever stored.** A page stores the directive text; the data is
resolved fresh for whoever is looking. A snapshot written into page content
would be a staleness lie the moment the work moved, and a visibility leak the
moment someone else opened the page.

**Why extraction happens before Markdown.** The obvious implementation — render
Markdown, then find the ```athena block in the HTML — cannot work: the sanitizer
strips the ``class="language-athena"`` that would identify it, verified rather
than assumed. So directives are lifted out of the source, replaced by an opaque
token, and substituted back after the HTML is sanitized. That is exactly how
`[[issue:5]]` tokens already survive the pipeline, and it has the property that
matters: the HTML an embed produces is built by Athena from escaped values, and
never passes through the sanitizer as author-supplied markup at all.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

#: What an embed can show. Closed on purpose: an unknown kind renders a visible
#: refusal naming it, never an empty space that reads as "no results".
KINDS = ("issues", "count", "issue")

#: Keys a directive may set. Unknown keys are refused rather than ignored — a
#: silently dropped `limt: 5` would render a differently-sized embed than the
#: author wrote and give them nothing to debug.
KEYS = ("kind", "q", "limit", "title", "issue")

#: Per-directive row ceiling, and the default when none is given. A page must not
#: become a way to make the server materialize the whole issue table on every
#: view; over-budget clamps and the surface says it clamped.
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

#: Per-page directive ceiling. Each directive is a query; a page with hundreds
#: would be a denial-of-service amplifier that anyone with page-write can author.
MAX_DIRECTIVES_PER_PAGE = 10

MAX_TITLE_CHARS = 120

#: The fence. Only ``athena`` (case-insensitive) is a directive; every other
#: fenced block is ordinary code and is left completely alone.
DIRECTIVE_RE = re.compile(
    r"^[ \t]*```[ \t]*athena[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


class DirectiveError(ValueError):
    """A directive could not be parsed. The message is shown to the reader in the
    embed's place — an author needs to see *why* their block did not render, and
    a reader needs to know something was meant to be here."""


@dataclass(frozen=True)
class Directive:
    """One parsed embed request."""

    kind: str
    query: str = ""
    limit: int = DEFAULT_LIMIT
    title: str = ""
    #: For ``kind: issue`` — the issue reference (numeric id or project key).
    issue_ref: str = ""


def _parse_body(body: str) -> dict[str, str]:
    """``key: value`` lines. Deliberately not YAML: a real YAML parser would
    accept nested structures, anchors, and type coercion this grammar has no use
    for, and would turn a page body into a much larger attack surface."""
    fields: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise DirectiveError(f"line is not 'key: value': {line[:60]!r}")
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key not in KEYS:
            raise DirectiveError(
                f"unknown embed key '{key}' — expected one of: {', '.join(KEYS)}"
            )
        if key in fields:
            raise DirectiveError(f"'{key}' given more than once")
        fields[key] = value.strip()
    return fields


def parse_directive(body: str) -> Directive:
    """Parse one directive body, or raise :class:`DirectiveError`."""
    fields = _parse_body(body)
    kind = fields.get("kind", "").lower()
    if not kind:
        raise DirectiveError(f"embed needs a 'kind' — one of: {', '.join(KINDS)}")
    if kind not in KINDS:
        raise DirectiveError(
            f"unknown embed kind '{kind}' — expected one of: {', '.join(KINDS)}"
        )

    title = fields.get("title", "")[:MAX_TITLE_CHARS]

    if kind == "issue":
        issue_ref = fields.get("issue", "")
        if not issue_ref:
            raise DirectiveError("'kind: issue' needs an 'issue:' id or key")
        if fields.get("q"):
            raise DirectiveError("'kind: issue' takes an 'issue:', not a 'q:'")
        return Directive(kind=kind, issue_ref=issue_ref, title=title)

    raw_limit = fields.get("limit", "")
    limit = DEFAULT_LIMIT
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise DirectiveError(
                f"'limit' must be a number, got {raw_limit!r}"
            ) from exc
        if limit < 1:
            raise DirectiveError("'limit' must be at least 1")
        # Clamped rather than refused: the author's intent is clear and honoring
        # it approximately, while saying so, beats refusing the whole embed.
        limit = min(limit, MAX_LIMIT)
    if fields.get("issue"):
        raise DirectiveError(f"'issue:' only applies to 'kind: issue', not {kind!r}")
    return Directive(kind=kind, query=fields.get("q", ""), limit=limit, title=title)


def find_directives(text: str | None) -> list[tuple[str, str]]:
    """Every ```athena block in a body, as ``(whole_match, body)`` pairs.

    Returns the raw text; parsing is the caller's next step, so a malformed
    directive can be reported *in place* rather than failing the whole page.
    """
    if not text:
        return []
    return [(match.group(0), match.group(1)) for match in DIRECTIVE_RE.finditer(text)]


def describe() -> dict[str, object]:
    """The embed vocabulary, as data — for docs and MCP, emitted rather than
    restated so the three cannot drift."""
    return {
        "kinds": {
            "issues": "a table of issues matching 'q'",
            "count": "how many issues match 'q', as a single number",
            "issue": "one issue, named by 'issue:' (id or project key)",
        },
        "keys": list(KEYS),
        "limits": {
            "max_limit": MAX_LIMIT,
            "default_limit": DEFAULT_LIMIT,
            "max_directives_per_page": MAX_DIRECTIVES_PER_PAGE,
        },
        "example": "```athena\nkind: issues\nq: is:open project:ATH\nlimit: 10\n```",
    }
