"""Compiling a parsed work query into SQL against the issue tables.

`core/work_query.py` owns the grammar and knows nothing about issues. This module
owns the other half: what ``is:open`` *means* for an issue whose project defines
its own statuses, how ``project:ATH`` resolves a key, and which column
``sort:priority-desc`` orders by. The split follows the import contract — the
parser is pure and testable without a database; the meaning lives with the domain.

Three rules shape everything here.

**Visibility is composed in SQL, never filtered in Python.** A compiled query
carries the same project-visibility clause ``list_issues`` uses, so a bounded page
of results is a page of *visible* results. Fetching rows and dropping them
afterwards would make ``limit`` mean "up to N, minus the ones you couldn't see",
which silently under-fills a page and leaks the existence of what was removed
through the count.

**Every value is a parameter.** Column and table names are literals chosen from
closed tables in this module; user input is only ever bound.

**"Closed" is a category, not a name.** ``is:open``/``is:closed`` resolve through
``statuses.category_sql`` — the same generated expression the fleet views use — so
a project whose done state is called ``shipped`` behaves correctly, and there is
no second definition of closed-ness to drift.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issues, statuses
from athena.core import work_query
from athena.core.work_query import Query

# Ordering, by sort key. Literals only — the key comes from work_query's closed
# vocabulary, so an unknown key is impossible by the time it reaches here.
#
# Priority is an ordered concept stored as text, so it is ranked explicitly rather
# than sorted alphabetically (which would read urgent, medium, low, high). The
# CASE is generated from issues.PRIORITIES so adding one cannot silently sort it
# to the bottom.
_PRIORITY_RANK = (
    "CASE i.priority "
    + " ".join(
        f"WHEN '{name}' THEN {index}" for index, name in enumerate(issues.PRIORITIES)
    )
    + f" ELSE {len(issues.PRIORITIES)} END"
)

_ORDER_BY: dict[str, str] = {
    "created-asc": "i.created_at ASC, i.id ASC",
    "created-desc": "i.created_at DESC, i.id DESC",
    "id-asc": "i.id ASC",
    "id-desc": "i.id DESC",
    # "desc" for priority means most urgent first, which is the HIGHEST rank —
    # the reverse of the numeric direction, and the reading an operator expects.
    "priority-asc": f"{_PRIORITY_RANK} ASC, i.id DESC",
    "priority-desc": f"{_PRIORITY_RANK} DESC, i.id DESC",
    "status-asc": "i.status ASC, i.id DESC",
    "status-desc": "i.status DESC, i.id DESC",
}

# The status-category expression, over the LEFT JOIN this module adds below.
_CATEGORY = statuses.category_sql(
    status_column="i.status", category_column="qps.category"
)

# Joined once, unconditionally: the category expression needs it, and a LEFT JOIN
# on an indexed (project_id, name) pair costs nothing when unused.
_JOIN = (
    " LEFT JOIN project_statuses qps "
    "ON qps.project_id = i.project_id AND qps.name = i.status"
)

_EXISTS_BLOCKERS = (
    "EXISTS (SELECT 1 FROM issue_links ql WHERE ql.to_id = i.id AND ql.kind = 'blocks')"
)
_EXISTS_CHILDREN = "EXISTS (SELECT 1 FROM issues qc WHERE qc.parent_id = i.id)"
_EXISTS_LABELS = "EXISTS (SELECT 1 FROM issue_labels qil WHERE qil.issue_id = i.id)"

_HAS_SQL: dict[str, str] = {
    "blockers": _EXISTS_BLOCKERS,
    "children": _EXISTS_CHILDREN,
    "labels": _EXISTS_LABELS,
    "parent": "i.parent_id IS NOT NULL",
}


class QueryCompileError(ValueError):
    """A parsed query cannot be compiled — a value the grammar accepted that this
    domain rejects. Carries the same shape as :class:`work_query.QueryError` so a
    transport maps both to one 422."""

    def __init__(self, message: str, *, atom: str | None = None) -> None:
        super().__init__(message)
        self.atom = atom


def _label_clause(negated: bool) -> str:
    inner = (
        "SELECT 1 FROM issue_labels qil JOIN labels qlb ON qlb.id = qil.label_id "
        "WHERE qil.issue_id = i.id AND qlb.name = ? COLLATE NOCASE"
    )
    return f"{'NOT ' if negated else ''}EXISTS ({inner})"


def _resolve_project(conn: sqlite3.Connection, value: str) -> tuple[str, list]:
    """``project:none`` is the backlog; otherwise an id or a key like ATH.

    An unresolvable key matches nothing rather than raising: the same contract the
    existing ``project=`` parameter has for an unknown id, and a project the caller
    cannot see must not be distinguishable from one that does not exist.
    """
    if value.lower() == "none":
        return "i.project_id IS NULL", []
    parsed = issues.parse_filter_id(value)
    if issues.is_filter_id(parsed):
        return "i.project_id = ?", [parsed]
    row = conn.execute(
        "SELECT id FROM projects WHERE key = ? COLLATE NOCASE", (value,)
    ).fetchone()
    if row is None:
        return "1 = 0", []
    return "i.project_id = ?", [int(row["id"])]


def _resolve_assignee(value: str, actor: dict | None) -> tuple[str, list]:
    if value.lower() == "none":
        return "i.assignee_id IS NULL", []
    if value == work_query.ME:
        if actor is None:
            # An anonymous caller has no "me". Refusing beats matching nothing:
            # silently returning zero rows would read as "you have no work".
            raise QueryCompileError(
                "'assignee:@me' needs an authenticated caller", atom="assignee:@me"
            )
        return "i.assignee_id = ?", [int(actor["id"])]
    parsed = issues.parse_filter_id(value)
    if not issues.is_filter_id(parsed):
        return "1 = 0", []
    return "i.assignee_id = ?", [parsed]


def _resolve_sprint(value: str) -> tuple[str, list]:
    if value.lower() == "none":
        return "i.sprint_id IS NULL", []
    parsed = issues.parse_filter_id(value)
    if not issues.is_filter_id(parsed):
        return "1 = 0", []
    return "i.sprint_id = ?", [parsed]


def _term_sql(conn: sqlite3.Connection, term, actor: dict | None) -> tuple[str, list]:
    """One term as (clause, params). Negation wraps the positive form, so the two
    readings can never disagree about what the term means."""
    field, value = term.field, term.value

    if field == "label":
        return _label_clause(term.negated), [value]

    if field == "has":
        clause = _HAS_SQL[value]
        return (f"NOT ({clause})" if term.negated else clause), []

    if field == "is":
        params: list = []
        if value == "open":
            clause = f"{_CATEGORY} != 'done'"
        elif value == "closed":
            clause = f"{_CATEGORY} = 'done'"
        elif value == "archived":
            clause = "i.archived_at IS NOT NULL"
        else:  # unassigned — the vocabulary is closed, so this is exhaustive
            clause = "i.assignee_id IS NULL"
        return (f"NOT ({clause})" if term.negated else clause), params

    if field == "project":
        clause, params = _resolve_project(conn, value)
    elif field == "assignee":
        clause, params = _resolve_assignee(value, actor)
    elif field == "sprint":
        clause, params = _resolve_sprint(value)
    elif field == "status":
        clause, params = "i.status = ?", [value]
    else:  # priority — the field vocabulary is closed upstream
        clause, params = "i.priority = ?", [value]
    return (f"NOT ({clause})" if term.negated else clause), params


def compile_query(
    conn: sqlite3.Connection,
    query: Query,
    *,
    actor: dict | None,
    visible_project_ids: set[int] | None,
) -> tuple[str, list, str]:
    """Compile a parsed query to ``(where_sql, params, order_by)``.

    ``where_sql`` excludes the leading WHERE and is always non-empty (``1 = 1`` for
    an empty query), so callers concatenate without branching.

    Archived issues are excluded unless the query asks for them with ``is:archived``
    — the same default every list in Athena applies, kept here so a query and the
    list it replaces agree about what exists.
    """
    clauses: list[str] = []
    params: list = []

    for term in query.terms:
        clause, term_params = _term_sql(conn, term, actor)
        clauses.append(clause)
        params.extend(term_params)

    if query.text:
        clauses.append("(i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\')")
        like = issues.like_contains(query.text)
        params.extend([like, like])

    wants_archived = any(
        t.field == "is" and t.value == "archived" and not t.negated for t in query.terms
    )
    if not wants_archived:
        clauses.append("i.archived_at IS NULL")

    if visible_project_ids is not None:
        # Identical to list_issues: the backlog has no project to gate on, so an
        # empty visible set narrows to the backlog rather than to nothing.
        if visible_project_ids:
            placeholders = ",".join("?" for _ in visible_project_ids)
            clauses.append(
                f"(i.project_id IS NULL OR i.project_id IN ({placeholders}))"
            )
            params.extend(visible_project_ids)
        else:
            clauses.append("i.project_id IS NULL")

    where = " AND ".join(clauses) if clauses else "1 = 1"
    return where, params, _ORDER_BY[query.sort]


def run_query(
    conn: sqlite3.Connection,
    query: Query,
    *,
    actor: dict | None,
    visible_project_ids: set[int] | None,
    limit: int,
    offset: int = 0,
) -> list[dict]:
    """Execute a compiled query and return issue dicts.

    Reuses ``issues.select_sql()`` and ``issues.to_issue()`` so a queried issue is
    byte-for-byte the same shape a listed one is — one row mapper, one set of
    joins, no second notion of what an issue looks like.
    """
    if offset < 0 or offset > issues.MAX_OFFSET:
        raise QueryCompileError(f"offset must be between 0 and {issues.MAX_OFFSET}")
    where, params, order_by = compile_query(
        conn, query, actor=actor, visible_project_ids=visible_project_ids
    )
    sql = (
        f"{issues.select_sql()}{_JOIN} WHERE {where} "
        f"ORDER BY {order_by} LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    return [issues.to_issue(row) for row in rows]


def count_query(
    conn: sqlite3.Connection,
    query: Query,
    *,
    actor: dict | None,
    visible_project_ids: set[int] | None,
) -> int:
    """How many issues match, ignoring paging — so a surface can say "12 of 340"
    instead of implying the page is the whole answer."""
    where, params, _ = compile_query(
        conn, query, actor=actor, visible_project_ids=visible_project_ids
    )
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM issues i{_JOIN} WHERE {where}", params
    ).fetchone()
    return int(row["n"])
