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

from datetime import UTC, datetime, timedelta
import sqlite3

from athena.aegis import issues, leases, ready_queue, statuses, work_progress
from athena.core import clock, work_query
from athena.core.work_query import Query

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# When work last happened on the issue: the latest native work event (the progress
# view's own verb list), or creation when nothing was ever recorded. Check-ins and
# lease renewals are not in that list, so a run that keeps reporting while nothing
# moves stays idle. Never NULL, so comparisons and ordering need no NULL rule.
_IDLE_ANCHOR = f"COALESCE({work_progress.last_work_sql('i.id')}, i.created_at)"

# A live lease, by the clock: the lease module's own "active" verdict.
_LIVE_LEASE = leases.live_lease_sql("i.id")

# Ordering, by sort key. Literals only — the key comes from work_query's closed
# vocabulary, so an unknown key is impossible by the time it reaches here.
#
# The priority ranking is issues.PRIORITY_RANK_SQL, not a second copy of the same
# CASE: the web issue list ranks priority with the identical expression, and the two
# surfaces disagreeing about what "sort by priority" means is a bug we already had.
_PRIORITY_RANK = issues.PRIORITY_RANK_SQL

#
# Each sort is its ORDER BY columns as (expression, descending, value type), the id
# last so the order is total. The same columns are the keyset a list cursor resumes
# after (core/list_cursor.py): the next page starts strictly after the last row's
# values, so an issue created between two pages never repeats a row the way an
# offset did.
_SORT_KEYS: dict[str, tuple[tuple[str, bool, type], ...]] = {
    "created-asc": (("i.created_at", False, str), ("i.id", False, int)),
    "created-desc": (("i.created_at", True, str), ("i.id", True, int)),
    "id-asc": (("i.id", False, int),),
    "id-desc": (("i.id", True, int),),
    # "desc" for priority means most urgent first, which is the HIGHEST rank —
    # the reverse of the numeric direction, and the reading an operator expects.
    "priority-asc": ((_PRIORITY_RANK, False, int), ("i.id", True, int)),
    "priority-desc": ((_PRIORITY_RANK, True, int), ("i.id", True, int)),
    "status-asc": (("i.status", False, str), ("i.id", True, int)),
    "status-desc": (("i.status", True, str), ("i.id", True, int)),
    # Most idle first: the oldest last-work time leads.
    "idle-desc": ((_IDLE_ANCHOR, False, str), ("i.id", True, int)),
}
_ORDER_BY: dict[str, str] = {
    sort: ", ".join(f"{expr} {'DESC' if desc else 'ASC'}" for expr, desc, _ in keys)
    for sort, keys in _SORT_KEYS.items()
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


def _duration_sql(field: str, value: str, now: datetime) -> tuple[str, list]:
    """``idle:>14d`` → the idle anchor is older than 14 days ago; ``<`` → newer.
    ``age:`` is the same against ``i.created_at``. The cutoff is bound, never
    formatted in, and comes from the caller's one request clock."""
    parsed = work_query.parse_days(value)
    if parsed is None:  # the parser already refused this; kept as a hard stop
        raise QueryCompileError(
            f"'{field}:{value}' is not valid", atom=f"{field}:{value}"
        )
    sign, days = parsed
    cutoff = (now - timedelta(days=days)).strftime(_TS_FORMAT)
    column = _IDLE_ANCHOR if field == "idle" else "i.created_at"
    # More than N days idle means the anchor is EARLIER than the cutoff.
    comparison = "<" if sign == ">" else ">"
    return f"{column} {comparison} ?", [cutoff]


def _term_sql(
    conn: sqlite3.Connection, term, actor: dict | None, now: datetime
) -> tuple[str, list]:
    """One term as (clause, params). Negation wraps the positive form, so the two
    readings can never disagree about what the term means."""
    field, value = term.field, term.value

    if field in work_query.DURATION_FIELDS:
        duration_clause, duration_params = _duration_sql(field, value, now)
        return (
            f"NOT ({duration_clause})" if term.negated else duration_clause
        ), duration_params

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
        elif value in ("claimed", "unclaimed"):
            # One definition, negated for the other: they cannot disagree.
            clause = _LIVE_LEASE if value == "claimed" else f"NOT {_LIVE_LEASE}"
            params = [leases.clock_stamp(now)]
        elif value == "ready":
            # The ready queue's own rule, so the atom, the desk lane and
            # claim_next_ready cannot disagree about what is ready.
            try:
                clause, params = ready_queue.ready_clause(
                    conn, actor, now=now, category_expr=_CATEGORY
                )
            except ready_queue.ReadyQueueError as exc:
                raise QueryCompileError(str(exc), atom="is:ready") from exc
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
    now: datetime | None = None,
) -> tuple[str, list, str]:
    """Compile a parsed query to ``(where_sql, params, order_by)``.

    ``where_sql`` excludes the leading WHERE and is always non-empty (``1 = 1`` for
    an empty query), so callers concatenate without branching.

    Archived issues are excluded unless the query asks for them with ``is:archived``
    — the same default every list in Athena applies, kept here so a query and the
    list it replaces agree about what exists.

    ``now`` is the one clock every time-relative atom (``idle:``, ``age:``,
    ``is:claimed``) is measured against; it defaults to the current UTC time.
    """
    current = clock.now() if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    clauses: list[str] = []
    params: list = []

    for term in query.terms:
        clause, term_params = _term_sql(conn, term, actor, current)
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


def cursor_tag(query: Query) -> str:
    """The list a cursor of this query belongs to: one per sort, so a cursor
    from one order is never read as a place in another."""
    return f"issues:{query.sort}"


def cursor_types(query: Query) -> tuple[type, ...]:
    """The value types of a cursor key for this query's sort."""
    return tuple(kind for _, _, kind in _SORT_KEYS[query.sort])


def query_page(
    conn: sqlite3.Connection,
    query: Query,
    *,
    actor: dict | None,
    visible_project_ids: set[int] | None,
    limit: int,
    after: list | None = None,
) -> tuple[list[dict], list | None]:
    """One page of a compiled query: at most ``limit`` issues strictly after the
    sort key ``after`` (None: from the start), and the key the next page resumes
    after, or None when no issue follows.

    The keyset predicate compares the sort's own ORDER BY expressions with the
    last row's values, column by column (``(a > x) OR (a = x AND id < y)``), so a
    page starts exactly where the previous one ended in that order."""
    where, params, order_by = compile_query(
        conn, query, actor=actor, visible_project_ids=visible_project_ids
    )
    keys = _SORT_KEYS[query.sort]
    if after is not None:
        if len(after) != len(keys):
            raise QueryCompileError("cursor does not belong to this sort")
        alternatives = []
        for depth, (expr, desc, _) in enumerate(keys):
            ties = [f"{tied} = ?" for tied, _, _ in keys[:depth]]
            alternatives.append(
                "(" + " AND ".join([*ties, f"{expr} {'<' if desc else '>'} ?"]) + ")"
            )
            params = [*params, *after[:depth], after[depth]]
        where = f"({where}) AND ({' OR '.join(alternatives)})"
    sql = f"{issues.select_sql()}{_JOIN} WHERE {where} ORDER BY {order_by} LIMIT ?"
    rows = conn.execute(sql, [*params, limit + 1]).fetchall()
    page = [issues.to_issue(row) for row in rows[:limit]]
    if len(rows) <= limit:
        return page, None
    columns = ", ".join(expr for expr, _, _ in keys)
    last = conn.execute(
        f"SELECT {columns} FROM issues i WHERE i.id = ?", (page[-1]["id"],)
    ).fetchone()
    return page, list(last)


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
