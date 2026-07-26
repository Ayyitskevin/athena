"""The work query grammar — one way to ask for work, everywhere.

Athena had filters (structured query parameters, saved-filter criteria, a board's
implicit scope) but no *language*. An operator who wanted "open infra issues in
ATH assigned to me, most urgent first" had to click three controls and could not
save, share, or hand that sentence to an agent. This module is the sentence.

The shape is deliberately GitHub's, not Jira's::

    is:open label:infra project:ATH assignee:@me sort:priority-desc payment

`JQL` is a programming language with functions, operator precedence, and a
grammar you look up. That is the wrong trade for a tool whose second audience is
an LLM agent composing a query from a docstring: every atom here is
``field:value``, atoms are joined by AND, and a leading ``-`` negates one. There
is no precedence to get wrong because there are no operators.

**This module is pure.** No database, no domain imports, no I/O — ``core`` may
not import ``aegis``/``mentor`` under the import contract, and the parser has no
business knowing what a status *is*. It validates the grammar and the closed
vocabularies it owns (``is:``, ``has:``, ``sort:``); it hands every free value
(a status name, a label, a project key) through untouched for the domain layer
to resolve. So this module is unit-testable with no fixtures, and the same parse
result drives REST, the browser, MCP, and saved filters.

**An unknown atom is an error, never an empty result.** ``asignee:@me`` returns
422 naming the atom rather than silently matching nothing: a query that quietly
answers "no results" to a typo is inventing an answer, which is the one thing
Athena's steering rules forbid outright. Unknown *values* for open-vocabulary
fields are different and legitimately match nothing — an unknown label or status
name has always behaved that way, and tightening it would break every existing
filter that names a label since deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

# --- the closed vocabulary --------------------------------------------------

#: Fields whose value is free text the domain layer resolves (a status name, a
#: label, a project key or id). An unrecognized value matches nothing.
OPEN_FIELDS = ("status", "priority", "label", "project", "sprint", "assignee")

#: ``is:`` states. Closed: a typo here is a refusal, not an empty page.
IS_VALUES = ("open", "closed", "archived", "unassigned")

#: ``has:`` structural predicates. Closed, same reason.
HAS_VALUES = ("blockers", "parent", "children", "labels")

#: ``sort:`` keys. Closed. Note the absence of ``updated``: the issues table has
#: no ``updated_at`` column, and inventing one here would produce a sort key that
#: silently fell back to something else. Recording the gap beats faking the sort.
SORT_VALUES = (
    "created-asc",
    "created-desc",
    "id-asc",
    "id-desc",
    "priority-asc",
    "priority-desc",
    "status-asc",
    "status-desc",
)

#: The default order when a query names none: newest first, which is what an
#: operator scanning a list almost always wants. ``list_issues`` orders by id ASC
#: for stable paging; a *query* is a different contract and says so.
DEFAULT_SORT = "id-desc"

FIELDS = (*OPEN_FIELDS, "is", "has", "sort")

#: Closed value vocabularies, by field.
_CLOSED: dict[str, tuple[str, ...]] = {
    "is": IS_VALUES,
    "has": HAS_VALUES,
    "sort": SORT_VALUES,
}

#: The actor placeholder. Resolved by the compiler against the request actor —
#: never here, because this module never learns who is asking.
ME = "@me"

#: A bound on input size. A query is typed by a human or composed by an agent;
#: neither needs more than this, and an unbounded string is a parser DoS.
MAX_QUERY_CHARS = 500
MAX_TERMS = 40


class QueryError(ValueError):
    """A query could not be parsed. ``atom`` names the offending piece so a
    transport can echo it back — "unknown search field 'asignee'" is actionable
    where "invalid query" is not."""

    def __init__(self, message: str, *, atom: str | None = None) -> None:
        super().__init__(message)
        self.atom = atom


@dataclass(frozen=True)
class Term:
    """One ``field:value`` atom, possibly negated."""

    field: str
    value: str
    negated: bool = False


@dataclass(frozen=True)
class Query:
    """A parsed query: structured terms, free text, and an order.

    ``text`` is the concatenation of every bare (non-``field:``) word and quoted
    phrase, which the domain layer applies as its ordinary substring search — the
    same behavior the existing ``search=`` parameter has, so a query box is not a
    second search implementation.
    """

    terms: tuple[Term, ...] = ()
    text: str = ""
    sort: str = DEFAULT_SORT
    #: Every term whose field is negated is here too; kept for surfaces that want
    #: to render the query back as chips.
    raw: str = dataclass_field(default="")

    def of(self, field: str, *, negated: bool = False) -> tuple[str, ...]:
        """Every value given for one field, in order."""
        return tuple(
            t.value for t in self.terms if t.field == field and t.negated is negated
        )


def _tokenize(raw: str) -> list[str]:
    """Split on whitespace, honoring double-quoted phrases.

    Hand-rolled rather than regex or ``shlex``: ``shlex`` raises on an unbalanced
    quote and has POSIX escaping rules nobody typing in a search box expects. An
    unterminated quote here simply runs to end of input, which is what every
    search box on the internet does.
    """
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    for char in raw:
        if char == '"':
            quoted = not quoted
            continue
        if char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def parse(raw: str | None) -> Query:
    """Parse a query string. Raises :class:`QueryError` for a malformed one.

    An empty or whitespace-only query is legal and means "everything" — the same
    thing an empty saved-filter criteria means.
    """
    if raw is None:
        return Query(raw="")
    if len(raw) > MAX_QUERY_CHARS:
        raise QueryError(f"query must be at most {MAX_QUERY_CHARS} characters")

    terms: list[Term] = []
    words: list[str] = []
    sort = DEFAULT_SORT
    sort_seen = False

    for token in _tokenize(raw):
        negated = token.startswith("-") and len(token) > 1
        body = token[1:] if negated else token
        if ":" not in body:
            # A bare word is text. A lone "-" or a stray negation of nothing is
            # text too — refusing it would fail queries containing a hyphen.
            words.append(token)
            continue
        field, _, value = body.partition(":")
        field = field.lower()
        if field not in FIELDS:
            raise QueryError(f"unknown search field '{field}'", atom=field)
        if not value:
            raise QueryError(f"'{field}:' needs a value", atom=field)
        allowed = _CLOSED.get(field)
        if allowed is not None:
            value = value.lower()
            if value not in allowed:
                raise QueryError(
                    f"'{field}:{value}' is not valid — "
                    f"try {', '.join(f'{field}:{v}' for v in allowed)}",
                    atom=f"{field}:{value}",
                )
        if field == "sort":
            if negated:
                raise QueryError("sort cannot be negated", atom="sort")
            if sort_seen and value != sort:
                raise QueryError(
                    "a query may name only one sort order", atom=f"sort:{value}"
                )
            sort, sort_seen = value, True
            continue
        terms.append(Term(field=field, value=value, negated=negated))
        if len(terms) > MAX_TERMS:
            raise QueryError(f"query may have at most {MAX_TERMS} terms")

    return Query(
        terms=tuple(terms),
        text=" ".join(words).strip(),
        sort=sort,
        raw=raw.strip(),
    )


def describe() -> dict[str, object]:
    """The grammar, as data — for `/query/help`, MCP docstrings, and the docs.

    Emitting the vocabulary rather than restating it in prose is what keeps the
    three surfaces from drifting: there is one list, and it is this one.
    """
    return {
        "fields": {
            "is": list(IS_VALUES),
            "has": list(HAS_VALUES),
            "sort": list(SORT_VALUES),
            "status": "any status name configured for the project",
            "priority": "low | medium | high | urgent",
            "label": "any label name",
            "project": "project id, project key (e.g. ATH), or 'none' for backlog",
            "sprint": "sprint id, or 'none' for issues in no sprint",
            "assignee": "user id, '@me', or 'none'",
        },
        "negation": "prefix any field with '-' to exclude, e.g. -label:noise",
        "text": 'bare words and "quoted phrases" match title and body',
        "default_sort": DEFAULT_SORT,
        "limits": {"max_chars": MAX_QUERY_CHARS, "max_terms": MAX_TERMS},
    }
