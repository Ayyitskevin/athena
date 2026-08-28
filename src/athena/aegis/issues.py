"""Data access for Aegis issues.

All issue SQL lives here. HTTP handlers call these functions instead of writing
queries, so if the storage ever changes, only this file does.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TypeGuard

from athena.aegis import projects, statuses
from athena.core import issue_identity, links, search, users

# The DEFAULT lifecycle for issues without a project. Since migration 0024,
# statuses are per-project (aegis/statuses.py owns those sets and their board
# columns); this tuple remains the fallback validated against for project-less
# issues and the seed for a new project's defaults. 'open' is the create default
# (matches the schema).
STATUSES = ("open", "in_progress", "done")

# How urgent an issue is, lowest → highest. 'medium' is the create default and
# the value existing rows backfilled to (migration 0006). Like STATUSES, this is
# the one canonical set the REST API and the web forms both validate against.
PRIORITIES = ("low", "medium", "high", "urgent")

# SQLite binds LIMIT/OFFSET parameters as signed 64-bit values. The REST list
# exposes this ceiling explicitly; keep the data layer safe for direct callers too.
MAX_OFFSET = 2**63 - 1


def _like_contains(value: str) -> str:
    """A LIKE pattern matching rows that CONTAIN `value` literally: %, _ and \\ are
    escaped so a search term like "run_id" doesn't have its underscore act as a
    single-char wildcard (silently over-matching "runXid"). Pair with ESCAPE '\\' in the
    query. Mirrors core.activity._like_pattern; kept local to stay in the aegis lane."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# Every read returns the assignee's display name + actor kind and the project's
# name + key alongside the row (NULL when unassigned / no project), so callers
# never resolve the ids themselves. LEFT JOINs, not JOINs: an unassigned or
# project-less issue must still come back.
#: Every issue row carries its status CATEGORY alongside the status name, via
#: the same generated expression the fleet views and work queries resolve
#: "closed" through. Chips color by category, never by name, so a project
#: whose done state is called "shipped" renders like one calling it "done".
_CATEGORY_SELECT = statuses.category_sql(
    status_column="i.status", category_column="ips.category"
)

_SELECT = (
    "SELECT i.*, u.name AS assignee_name, u.is_agent AS assignee_is_agent, "
    "p.name AS project_name, p.key AS project_key, "
    f"{_CATEGORY_SELECT} AS status_category "
    "FROM issues i "
    "LEFT JOIN users u ON u.id = i.assignee_id "
    "LEFT JOIN projects p ON p.id = i.project_id "
    "LEFT JOIN project_statuses ips "
    "ON ips.project_id = i.project_id AND ips.name = i.status"
)


def select_sql() -> str:
    """The issue SELECT (no WHERE), for a caller building its own predicate.

    Exposed so ``issue_query`` composes the *same* projection and joins rather
    than declaring a second one: a queried issue and a listed issue must be the
    same shape, or two surfaces of the same product disagree about what an issue
    has. Callers append their own joins, WHERE, and ORDER BY.
    """
    return _SELECT


def to_issue(row: sqlite3.Row) -> dict:
    """Map a :func:`select_sql` row to an issue dict — the one row mapper."""
    return _to_issue(row)


def like_contains(value: str) -> str:
    """The escaped LIKE pattern for a case-insensitive substring match, so a query
    grammar's free text searches exactly like the ``search=`` parameter does."""
    return _like_contains(value)


def priorities_for_ids(conn: sqlite3.Connection, issue_ids: set[int]) -> dict[int, str]:
    """Return the canonical priority for each existing issue in ``issue_ids``.

    Cross-module projections call this instead of borrowing the ``issues`` table
    from core. Missing ids are absent, so the caller can preserve its own default
    or fail-closed behavior without inventing an issue row.
    """
    if not issue_ids:
        return {}
    placeholders = ", ".join("?" for _ in issue_ids)
    rows = conn.execute(
        f"SELECT id, priority FROM issues WHERE id IN ({placeholders})",
        tuple(sorted(issue_ids)),
    ).fetchall()
    return {row["id"]: row["priority"] for row in rows}


def _to_issue(row: sqlite3.Row) -> dict:
    """Turn a _SELECT row into an issue dict, adding the computed display key.

    The key is the project's prefix + the issue's per-project number (ATH-12). It
    exists only when the issue is in a project (project_seq is NULL otherwise), so
    a backlog issue has key=None and callers fall back to "#<id>". Computed here,
    in the one place every read funnels through, so the API and the web view can
    never disagree on an issue's key."""
    issue = dict(row)
    if issue.get("assignee_is_agent") is not None:
        issue["assignee_is_agent"] = bool(issue["assignee_is_agent"])
    if issue.get("project_key") and issue.get("project_seq") is not None:
        issue["key"] = f"{issue['project_key']}-{issue['project_seq']}"
    else:
        issue["key"] = None
    return issue


def create_issue(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    created_by: int,
    status: str = "open",
    priority: str = "medium",
    project_id: int | None = None,
    commit: bool = True,
) -> dict:
    """Insert an issue and return it. Raises sqlite3.IntegrityError if
    created_by isn't a real user, or if project_id is a non-NULL id with no
    matching project (the foreign keys refuse the orphan). project_id is
    optional — None means the issue starts with no project (and so has no key
    until it's moved into one)."""
    # The lifecycle trail outlives hard-deleted issue rows, so allocate through
    # the shared monotonic identity owner used by native and portability writes.
    try:
        issue_id = issue_identity.allocate_issue_id(conn)
        project_seq = (
            projects.next_seq(conn, project_id) if project_id is not None else None
        )
        cur = conn.execute(
            "INSERT INTO issues "
            "(id, title, body, status, priority, created_by, project_id, project_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_id,
                title,
                body,
                status,
                priority,
                created_by,
                project_id,
                project_seq,
            ),
        )
        row_id = cur.lastrowid
        assert row_id is not None
        issue_id = row_id
        # Derived rows belong to the same unit as their source. The command layer
        # may keep all three uncommitted until it appends the required audit event.
        links.sync_links(
            conn,
            source_kind="issue",
            source_id=issue_id,
            body=body,
            commit=False,
        )
        search.index_document(conn, kind="issue", source_id=issue_id, commit=False)
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    issue = get_issue(conn, issue_id)
    assert issue is not None
    return issue


def update_issue(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    commit: bool = True,
) -> dict | None:
    """Partial update: only the fields passed as non-None change. Returns the
    updated issue, or None if no issue has that id (so the caller can 404).
    Field validation (status in STATUSES, priority in PRIORITIES, non-empty
    title) is the boundary's job.

    The column names below are hardcoded literals, never caller input, so the
    f-string assembles a safe SET clause; the values stay parameterized."""
    fields = {
        col: val
        for col, val in (
            ("title", title),
            ("body", body),
            ("status", status),
            ("priority", priority),
        )
        if val is not None
    }
    if not fields:
        # Nothing to change — still distinguish "no such issue" (None) from a
        # real but unchanged issue, so the boundary's 404 stays correct.
        return get_issue(conn, issue_id)
    assignments = ", ".join(f"{col} = ?" for col in fields)
    try:
        cur = conn.execute(
            f"UPDATE issues SET {assignments} WHERE id = ?",
            (*fields.values(), issue_id),
        )
        if cur.rowcount == 0:
            if commit:
                conn.commit()
            return None
        # Re-index references only when the body actually changed (the only field
        # that carries [[...]] tokens); a status/priority/title edit leaves them be.
        if "body" in fields:
            links.sync_links(
                conn,
                source_kind="issue",
                source_id=issue_id,
                body=fields["body"],
                commit=False,
            )
        # Re-index for search whenever an indexed field (title or body) changed; a
        # status/priority-only edit can't affect the text index.
        if "title" in fields or "body" in fields:
            search.index_document(conn, kind="issue", source_id=issue_id, commit=False)
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    return get_issue(conn, issue_id)


def update_status(conn: sqlite3.Connection, issue_id: int, status: str) -> dict | None:
    """Move an issue to a new status. Thin data-layer wrapper over update_issue.

    User-facing and automation writes go through issue_commands so this mutation
    and its audit event share one transaction.
    """
    return update_issue(conn, issue_id, status=status)


def set_assignee(
    conn: sqlite3.Connection,
    issue_id: int,
    assignee_id: int | None,
    *,
    commit: bool = True,
) -> dict | None:
    """Assign the issue to a user, or clear it (assignee_id=None -> Unassigned).
    Returns the updated issue, or None if no issue has that id. Checking that
    assignee_id is a real user is the command's job; the DB's foreign key is
    the backstop (raises sqlite3.IntegrityError on an unknown non-NULL id).

    Commands pass ``commit=False`` so the row and its assignment audit event,
    watch, and notifications share one transaction.
    """
    try:
        cur = conn.execute(
            "UPDATE issues SET assignee_id = ? WHERE id = ?", (assignee_id, issue_id)
        )
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def set_sprint(
    conn: sqlite3.Connection,
    issue_id: int,
    sprint_id: int | None,
    *,
    commit: bool = True,
) -> dict | None:
    """Put the issue in a sprint, or move it back to the backlog (sprint_id=None).
    Returns the updated issue, or None if no issue has that id. Like set_assignee:
    a single nullable column, so a dedicated op keeps None ('backlog') distinct from
    PATCH's 'leave unchanged'. Checking that the sprint exists and belongs to the
    issue's project is the command's job; the foreign key is the backstop (raises
    sqlite3.IntegrityError on an unknown non-NULL id).

    Commands pass ``commit=False`` so the row and its sprint audit event share
    one transaction.
    """
    try:
        cur = conn.execute(
            "UPDATE issues SET sprint_id = ? WHERE id = ?", (sprint_id, issue_id)
        )
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def set_archived(
    conn: sqlite3.Connection, issue_id: int, archived: bool, *, commit: bool = True
) -> dict | None:
    """Archive (soft-delete) an issue or restore it. Returns the updated issue, or
    None if no issue has that id. The row is never destroyed — archiving only stamps
    archived_at (and clearing it restores the issue), so the history is preserved and
    the default lists simply hide it. Idempotent: re-archiving an archived issue just
    re-stamps the time; the boundary records the audit fact only on a real change.
    ``commit=False`` lets the audited command fold the flip and its event together."""
    if archived:
        conn.execute(
            "UPDATE issues SET archived_at = datetime('now') WHERE id = ?", (issue_id,)
        )
    else:
        conn.execute("UPDATE issues SET archived_at = NULL WHERE id = ?", (issue_id,))
    if commit:
        conn.commit()
    return get_issue(conn, issue_id)


def set_project(
    conn: sqlite3.Connection,
    issue_id: int,
    project_id: int | None,
    *,
    commit: bool = True,
) -> dict | None:
    """Move the issue into a project, or remove it from one (project_id=None ->
    no project). Returns the updated issue, or None if no issue has that id.
    Checking that project_id is a real project is the boundary's job; the DB's
    foreign key is the backstop (raises sqlite3.IntegrityError on an unknown
    non-NULL id). Mirrors set_assignee — a single nullable column, so a dedicated
    operation keeps None ('remove') distinct from PATCH's 'leave unchanged'.

    Moving an issue reassigns its key: it gives up its old number (retired, never
    reused) and is handed a FRESH number in the destination project. So we only
    touch project_seq when the project actually changes — re-setting an issue to
    the project it's already in is a no-op that keeps its current number. Removing
    it from a project (project_id=None) clears the number too: a backlog issue has
    no key. Any ACTUAL project change also clears sprint membership, because a
    sprint belongs to exactly one project. A command that explicitly supplies a
    destination sprint sets it afterward in the same outer transaction.

    Commands pass ``commit=False`` so the key allocation, status remap, sprint
    clear, audit facts, and any explicit destination sprint share one transaction.
    """
    try:
        current = conn.execute(
            "SELECT project_id, status FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        if current is None:
            return None
        if current["project_id"] == project_id:
            return get_issue(conn, issue_id)  # same project — keep key and sprint
        project_seq = (
            projects.next_seq(conn, project_id) if project_id is not None else None
        )
        # The destination may not offer the issue's current status (projects can have
        # different status sets). If so, remap to the destination's first status — a
        # defined, predictable rule — rather than leaving the issue in a status its new
        # project doesn't know. If the status is valid there, keep it.
        new_status = current["status"]
        if not statuses.is_valid(conn, project_id, new_status):
            new_status = statuses.first_status(conn, project_id)
        conn.execute(
            "UPDATE issues SET project_id = ?, project_seq = ?, status = ?, "
            "sprint_id = NULL WHERE id = ?",
            (project_id, project_seq, new_status, issue_id),
        )
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise
    return get_issue(conn, issue_id)


def set_parent(
    conn: sqlite3.Connection,
    issue_id: int,
    parent_id: int | None,
    *,
    commit: bool = True,
) -> dict | None:
    """Nest an issue under a parent (parent_id=None clears it to top-level).
    Returns the updated issue, or None if no issue has that id. Validation
    (existence, no self-parent, no cycle) is the boundary's job — see
    validate_parent; the FK is the backstop for a non-NULL unknown parent.
    ``commit=False`` lets the audited command fold the write and its event together."""
    cur = conn.execute(
        "UPDATE issues SET parent_id = ? WHERE id = ?", (parent_id, issue_id)
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def list_children(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    visible_project_ids: set[int] | None = None,
) -> list[dict]:
    """The direct children of an issue (one level), oldest first.

    visible_project_ids gates the returned children by project visibility exactly as
    list_issues does: None (the default) means no gating — for internal/admin callers;
    a set restricts to children whose project is in it, PLUS backlog children (no
    project to gate on), so an empty set narrows to the backlog alone. This matters
    because parenting has NO same-project rule — a child can legitimately live in a
    private project under a public parent — so without this gate a non-member reading
    the public parent's children would leak the private child's title/body/key/status.
    """
    clauses = ["i.parent_id = ?"]
    params: list = [issue_id]
    if visible_project_ids is not None:
        if visible_project_ids:
            vis_ph = ",".join("?" for _ in visible_project_ids)
            clauses.append(f"(i.project_id IS NULL OR i.project_id IN ({vis_ph}))")
            params.extend(visible_project_ids)
        else:
            clauses.append("i.project_id IS NULL")
    where = " WHERE " + " AND ".join(clauses)
    rows = conn.execute(f"{_SELECT}{where} ORDER BY i.id", params).fetchall()
    return [_to_issue(row) for row in rows]


def validate_parent(
    conn: sqlite3.Connection, issue_id: int, parent_id: int | None
) -> str | None:
    """Whether parent_id is a legal parent for issue_id. Returns None if OK, else a
    human reason the boundary turns into a 422. Clearing (None) is always legal.
    Rejects a self-parent and any choice that would form a cycle — walking UP from
    the proposed parent must never reach the issue itself."""
    if parent_id is None:
        return None
    if parent_id == issue_id:
        return "an issue can't be its own parent"
    if get_issue(conn, parent_id) is None:
        return "no such parent issue"
    seen: set[int] = set()
    cursor: int | None = parent_id
    while cursor is not None:
        if cursor == issue_id:
            return "that would create a cycle"
        if cursor in seen:  # safety net against any pre-existing bad data
            break
        seen.add(cursor)
        row = conn.execute(
            "SELECT parent_id FROM issues WHERE id = ?", (cursor,)
        ).fetchone()
        cursor = row["parent_id"] if row else None
    return None


def count_issues_in_project(conn: sqlite3.Connection, project_id: int) -> int:
    """How many issues currently belong to this project. The project-delete path
    uses it to refuse deleting a project that still owns issues (deleting would
    either orphan them at the FK or silently detach them); the web UI uses it to
    explain a disabled Delete button. Lives here because issues.py owns the
    project_id column — projects.py never reaches into the issues table."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM issues WHERE project_id = ?", (project_id,)
    ).fetchone()["n"]


def can_modify(issue: dict, actor_id: int) -> bool:
    """Whether an actor may modify this issue (change status, edit, assign).
    The rule: the issue's creator OR its current assignee. An unassigned issue
    (assignee_id is None) can only be modified by its creator until someone is
    assigned. Reads and commenting are open to all authenticated actors and do
    NOT pass through here — this gate is for writes only.

    This is the pure, connection-free core; the authoritative write gate is
    can_act_on, which adds delegated contributors and the admin override."""
    return actor_id == issue["created_by"] or actor_id == issue["assignee_id"]


def can_act_on(conn: sqlite3.Connection, issue: dict, actor: dict) -> bool:
    """The authoritative write gate: creator, current assignee, a DELEGATED
    CONTRIBUTOR, or an admin. The contributor arm is what lets an agent finish
    work handed to it — delegation adds the agent to issue_contributors, so
    without it the delegated agent could see its assignment but never transition
    the issue (the Assign → Work loop dead-ended one step before completion).
    The admin arm is the operator override (and unbricks issues whose creator
    left). Reads issue_contributors directly (read-only; contributors.py stays
    the sole writer of that table)."""
    if can_modify(issue, actor["id"]):
        return True
    if actor.get("role") == users.ADMIN_ROLE:
        return True
    return (
        conn.execute(
            "SELECT 1 FROM issue_contributors WHERE issue_id = ? AND user_id = ?",
            (issue["id"], actor["id"]),
        ).fetchone()
        is not None
    )


def get_issue(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE i.id = ?", (issue_id,)).fetchone()
    return _to_issue(row) if row else None


# An issue ref is either a bare numeric id ("12") or a project key ("ATH-12").
# The key form is a project prefix (letters, leading letter) + "-" + the
# per-project number. This is the addressable surface used by the API and web
# read routes; writes stay numeric (forms post the issue id we control).
_KEY_REF_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$")


def get_by_ref(conn: sqlite3.Connection, ref: str) -> dict | None:
    """Resolve an issue from a ref that is either its numeric id ("12") or its
    project key ("ATH-12"). Returns the issue, or None if nothing matches (unknown
    id, unknown project key, or no issue with that number in that project) so the
    boundary can 404. A key lookup is case-insensitive on the prefix (projects.key
    is COLLATE NOCASE)."""
    ref = ref.strip()
    if ref.isdigit():
        return get_issue(conn, int(ref))
    m = _KEY_REF_RE.match(ref)
    if not m:
        return None
    project = projects.get_project_by_key(conn, m.group(1))
    if project is None:
        return None
    row = conn.execute(
        f"{_SELECT} WHERE i.project_id = ? AND i.project_seq = ?",
        (project["id"], int(m.group(2))),
    ).fetchone()
    return _to_issue(row) if row else None


MAX_SQLITE_INTEGER = (1 << 63) - 1


def is_filter_id(value: object) -> TypeGuard[int]:
    """Whether value is an exact integer id SQLite can bind safely.

    ``bool`` is deliberately excluded even though it subclasses ``int``: filter
    callers must not let ``True`` silently mean row id 1.
    """
    return type(value) is int and 0 <= value <= MAX_SQLITE_INTEGER


def parse_filter_id(value: str | None) -> int | None:
    """Parse a non-negative, SQLite-bindable decimal id from a filter value."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or not normalized.isascii() or not normalized.isdigit():
        return None
    try:
        parsed = int(normalized)
    except ValueError:
        # Python rejects extremely long decimal strings before conversion.
        return None
    return parsed if is_filter_id(parsed) else None


def parse_project_filter(project: str | None) -> tuple[int | None, bool] | None:
    """Interpret the ?project= filter value, shared by the REST API and the web
    list so the two can never drift on what the param means. Returns:

      - (None, False) for None/"" — no project filter at all;
      - (None, True)  for "none" (case-insensitive) — only backlog issues;
      - (id, False)   for an all-digits value — restrict to that project;
      - None          for anything else — INVALID. The caller rejects it (the API
        with 422, the web with 400) instead of silently showing every issue, so a
        garbled filter fails loud on both surfaces rather than lying on one.

    Returning None to mean "invalid" is unambiguous because the valid no-filter
    case is the tuple (None, False); callers check `is None` before unpacking."""
    if not project or not project.strip():
        return None, False
    value = project.strip()
    if value.lower() == "none":
        return None, True
    project_id = parse_filter_id(value)
    return (project_id, False) if project_id is not None else None


# Priority is an ordered concept stored as text, so it is ranked explicitly rather
# than sorted alphabetically (which would read urgent, medium, low, high). The CASE
# is generated from PRIORITIES so adding one cannot silently sort it to the bottom.
# It lives HERE, in the module that owns the issues table, because two readers need
# the same ranking: the work-query grammar (aegis/issue_query.py imports it) and the
# paged list below. Two copies is how the web list drifted into sorting priority
# alphabetically in the first place.
PRIORITY_RANK_SQL = (
    "CASE i.priority "
    + " ".join(f"WHEN '{name}' THEN {index}" for index, name in enumerate(PRIORITIES))
    + f" ELSE {len(PRIORITIES)} END"
)

# The sort keys `list_issues` accepts — a CLOSED set, because the key is
# interpolated into SQL as a column name. Anything else falls back to id order.
LIST_SORTS = ("id", "title", "status", "priority", "created_at")


def order_by_sql(sort: str | None, order: str = "desc") -> str:
    """The ORDER BY expression for one (sort, order) pair from the closed set above.

    sort=None is the historical unordered-by-caller default: plain `i.id`, byte-for-
    byte what every existing caller already gets.

    Every ordering is TOTAL — a tie is broken by id — because the list pages with
    LIMIT/OFFSET, and a non-total ordering makes SQLite free to return the same row
    on two pages (or no page at all). The tie-break is `i.id ASC` for the plain
    columns, which is what the web list's stable Python sort over an id-ASC read
    already produced. Priority is the exception: it reuses the grammar's exact
    expression, tie-break included, so `sort:priority-desc` in a work query and the
    web list's priority sort return one identical ordering rather than two orderings
    that merely agree on the first column."""
    direction = "DESC" if order == "desc" else "ASC"
    if sort == "priority":
        return f"{PRIORITY_RANK_SQL} {direction}, i.id DESC"
    if sort == "id":
        return f"i.id {direction}"
    if sort in ("title", "status", "created_at"):
        return f"i.{sort} {direction}, i.id ASC"
    return "i.id"


def _filter_sql(
    *,
    status: str | None,
    priority: str | None,
    assignee_id: int | None,
    search: str | None,
    project_id: int | None,
    backlog: bool,
    sprint_id: int | None,
    include_archived: bool,
    ids: list[int] | None,
    visible_project_ids: set[int] | None,
) -> tuple[str, list] | None:
    """The one WHERE clause behind every filtered issue read: the page, its total,
    and the status option set. Sharing it is what keeps a filter from meaning one
    thing in the list and another in the count above it.

    Returns (where_sql, params), or None when the filter set CANNOT match anything —
    an unusable id or an empty `ids` list. None is distinct from an empty WHERE
    (which matches everything), so callers must check it before running the query.

    Column names below are hardcoded literals; all values stay parameterized."""
    # These reads also serve direct internal callers, outside FastAPI's bounded
    # query models. Invalid ids are a real filter that matches nothing — never an
    # omitted filter (which would widen the result), and never an oversized value
    # handed to sqlite3 (which would raise OverflowError).
    if any(
        value is not None and not is_filter_id(value)
        for value in (assignee_id, project_id, sprint_id)
    ):
        return None
    if ids is not None and not ids:
        return None  # an empty id set can't match — and "IN ()" isn't valid SQL
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("i.status = ?")
        params.append(status)
    if priority:
        clauses.append("i.priority = ?")
        params.append(priority)
    if assignee_id is not None:
        clauses.append("i.assignee_id = ?")
        params.append(assignee_id)
    if search:
        clauses.append("(i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\')")
        like = _like_contains(search)
        params.extend([like, like])
    if backlog:
        clauses.append("i.project_id IS NULL")
    elif project_id is not None:
        clauses.append("i.project_id = ?")
        params.append(project_id)
    if sprint_id is not None:
        clauses.append("i.sprint_id = ?")
        params.append(sprint_id)
    if not include_archived:
        clauses.append("i.archived_at IS NULL")
    if ids is not None:
        placeholders = ",".join("?" for _ in ids)
        clauses.append(f"i.id IN ({placeholders})")
        params.extend(ids)
    if visible_project_ids is not None:
        # Backlog (no project) has nothing to gate on, so it stays visible; an empty
        # visible set therefore narrows to the backlog alone, not to nothing.
        if visible_project_ids:
            vis_ph = ",".join("?" for _ in visible_project_ids)
            clauses.append(f"(i.project_id IS NULL OR i.project_id IN ({vis_ph}))")
            params.extend(visible_project_ids)
        else:
            clauses.append("i.project_id IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def count_issues(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    priority: str | None = None,
    assignee_id: int | None = None,
    search: str | None = None,
    project_id: int | None = None,
    backlog: bool = False,
    sprint_id: int | None = None,
    include_archived: bool = False,
    ids: list[int] | None = None,
    visible_project_ids: set[int] | None = None,
) -> int:
    """How many issues `list_issues` would return unpaged, for the same filters.

    A paged surface needs the total to render "page 3 of 9" without fetching the
    other eight pages. Every filter parameter mirrors list_issues exactly — same
    shared WHERE — so the count can never disagree with the page it labels. The
    joins list_issues carries are for display columns only and no filter touches
    them, so this counts `issues` alone."""
    built = _filter_sql(
        status=status,
        priority=priority,
        assignee_id=assignee_id,
        search=search,
        project_id=project_id,
        backlog=backlog,
        sprint_id=sprint_id,
        include_archived=include_archived,
        ids=ids,
        visible_project_ids=visible_project_ids,
    )
    if built is None:
        return 0
    where, params = built
    row = conn.execute(f"SELECT COUNT(*) AS n FROM issues i{where}", params).fetchone()
    return int(row["n"])


def statuses_in_use(
    conn: sqlite3.Connection,
    *,
    visible_project_ids: set[int] | None = None,
    include_archived: bool = False,
) -> list[str]:
    """The distinct statuses actually present on issues the viewer may see.

    This is the option set the status filters offer, so it must list a project's
    CUSTOM statuses too — and must never list a private project's custom status to
    someone who cannot see that project, which is why it takes the same
    visible_project_ids gate every other read does. Name order only; the caller
    decides how to group them (the web list orders todo → doing → done, which is a
    presentation concern this layer does not own).

    Bounded by construction: SQLite answers DISTINCT from the table, so this costs
    the same whether the instance holds ten issues or ten thousand — the reason it
    exists instead of the caller collecting statuses off a full unpaged read."""
    built = _filter_sql(
        status=None,
        priority=None,
        assignee_id=None,
        search=None,
        project_id=None,
        backlog=False,
        sprint_id=None,
        include_archived=include_archived,
        ids=None,
        visible_project_ids=visible_project_ids,
    )
    if built is None:
        return []
    where, params = built
    return [
        row["status"]
        for row in conn.execute(
            f"SELECT DISTINCT i.status FROM issues i{where} ORDER BY i.status", params
        )
    ]


def list_issues(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    priority: str | None = None,
    assignee_id: int | None = None,
    search: str | None = None,
    project_id: int | None = None,
    backlog: bool = False,
    sprint_id: int | None = None,
    include_archived: bool = False,
    ids: list[int] | None = None,
    visible_project_ids: set[int] | None = None,
    limit: int | None = None,
    offset: int = 0,
    sort: str | None = None,
    order: str = "desc",
) -> list[dict]:
    """List issues, optionally filtered. This is the ONE filtering path the API
    and the web list both use, so the two never disagree on what matches.

    - status: exact status match.
    - priority: exact priority match (a direct column on the issue).
    - assignee_id: exact assignee match (a direct column). Restricts to issues
      assigned to this user; None means "don't filter by assignee at all" (use
      the dedicated unassigned view via project/backlog semantics if needed).
    - search: case-insensitive substring in title or body (SQLite LIKE).
    - project_id: restrict to issues in this project (a direct column on the
      issue, so unlike labels this module filters it itself).
    - backlog: restrict to issues in NO project (project_id IS NULL). Distinct
      from project_id=None, which means "don't filter by project at all". The two
      are mutually exclusive — the boundary picks one.
    - sprint_id: restrict to issues in this sprint (a direct column on the issue,
      like project_id). None means "don't filter by sprint at all".
    - include_archived: by default (False) archived issues are hidden — the soft-
      delete semantics every list/board wants. Pass True to include them (an
      "archived too" view). A single-issue read (get_issue) is never filtered.
    - ids: restrict to these issue ids. Generic on purpose — the caller resolves
      *what* the ids mean (e.g. labels.py turns a label name into ids), so this
      module stays decoupled from labels. An empty list means "match nothing".
    - visible_project_ids: the project-visibility gate. None (the default) means no
      gating — every issue is eligible, the historical behaviour, kept for internal
      and aggregate callers. A set restricts to issues whose project is in it, PLUS
      backlog issues (no project, so nothing to gate on); an empty set therefore
      yields only the backlog. Callers get the set from access.visible_project_filter
      (which hands back None for an admin, skipping the clause entirely).
    - limit / offset: page the result at the SQL level. limit=None (the default)
      returns every match — the historical behaviour every internal/aggregate caller
      relies on (board, dashboard, saved filters, search resolution). The REST list
      endpoint passes a bounded limit so an anonymous caller can't pull the whole
      table in one request; offset walks the pages. Ordering is stable (id ASC) so
      paging is well-defined.

    - sort / order: order the result IN SQL, from the closed vocabulary in
      LIST_SORTS (see order_by_sql). sort=None (the default) keeps the historical
      plain `i.id` ordering every existing caller already relies on. A paged caller
      must pass the sort it wants rather than reordering the page in Python: sorting
      after LIMIT only reorders the rows that happened to land on the page.

    Column names below are hardcoded literals; all values stay parameterized."""
    if offset < 0 or offset > MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")
    built = _filter_sql(
        status=status,
        priority=priority,
        assignee_id=assignee_id,
        search=search,
        project_id=project_id,
        backlog=backlog,
        sprint_id=sprint_id,
        include_archived=include_archived,
        ids=ids,
        visible_project_ids=visible_project_ids,
    )
    if built is None:
        return []
    where, params = built
    query = f"{_SELECT}{where} ORDER BY {order_by_sql(sort, order)}"
    if limit is not None:
        # Bound the result at the DB, not in Python — an unbounded caller must not be
        # able to make SQLite materialize the whole table. Only appended when a caller
        # opts in, so the uncapped internal callers are byte-for-byte unchanged.
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [_to_issue(row) for row in rows]
