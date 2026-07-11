"""Data access for Aegis issues.

All issue SQL lives here. HTTP handlers call these functions instead of writing
queries, so if the storage ever changes, only this file does.
"""
from __future__ import annotations

import re
import sqlite3

from athena.aegis import projects, statuses
from athena.core import links, search

# The lifecycle an issue moves through. This is the canonical set the whole app
# agrees on — the REST API and the web forms both validate against it, and the
# boards view lays out one column per status. 'open' is the create default
# (matches the schema). Keep this in sync with templates' status <option>s.
STATUSES = ("open", "in_progress", "done")

# How urgent an issue is, lowest → highest. 'medium' is the create default and
# the value existing rows backfilled to (migration 0006). Like STATUSES, this is
# the one canonical set the REST API and the web forms both validate against.
PRIORITIES = ("low", "medium", "high", "urgent")


def _like_contains(value: str) -> str:
    """A LIKE pattern matching rows that CONTAIN `value` literally: %, _ and \\ are
    escaped so a search term like "run_id" doesn't have its underscore act as a
    single-char wildcard (silently over-matching "runXid"). Pair with ESCAPE '\\' in the
    query. Mirrors core.activity._like_pattern; kept local to stay in the aegis lane."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

# Every read returns the assignee's display name and the project's name + key
# alongside the row (NULL when unassigned / no project), so callers never resolve
# the ids themselves. LEFT JOINs, not JOINs: an unassigned or project-less issue
# must still come back.
_SELECT = (
    "SELECT i.*, u.name AS assignee_name, "
    "p.name AS project_name, p.key AS project_key "
    "FROM issues i "
    "LEFT JOIN users u ON u.id = i.assignee_id "
    "LEFT JOIN projects p ON p.id = i.project_id"
)


def _to_issue(row: sqlite3.Row) -> dict:
    """Turn a _SELECT row into an issue dict, adding the computed display key.

    The key is the project's prefix + the issue's per-project number (ATH-12). It
    exists only when the issue is in a project (project_seq is NULL otherwise), so
    a backlog issue has key=None and callers fall back to "#<id>". Computed here,
    in the one place every read funnels through, so the API and the web view can
    never disagree on an issue's key."""
    issue = dict(row)
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
    # If the issue is born into a project, allocate its per-project number from
    # that project's counter. next_seq doesn't commit, so the counter bump and the
    # INSERT below land together under the single commit (or roll back together).
    try:
        project_seq = (
            projects.next_seq(conn, project_id) if project_id is not None else None
        )
        cur = conn.execute(
            "INSERT INTO issues "
            "(title, body, status, priority, created_by, project_id, project_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, body, status, priority, created_by, project_id, project_seq),
        )
        # Derived rows belong to the same unit as their source. The command layer
        # may keep all three uncommitted until it appends the required audit event.
        links.sync_links(
            conn,
            source_kind="issue",
            source_id=cur.lastrowid,
            body=body,
            commit=False,
        )
        search.index_document(
            conn, kind="issue", source_id=cur.lastrowid, commit=False
        )
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    return get_issue(conn, cur.lastrowid)


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
            search.index_document(
                conn, kind="issue", source_id=issue_id, commit=False
            )
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    return get_issue(conn, issue_id)


def update_status(
    conn: sqlite3.Connection, issue_id: int, status: str
) -> dict | None:
    """Move an issue to a new status. Thin data-layer wrapper over update_issue.

    User-facing and automation writes go through issue_commands so this mutation
    and its audit event share one transaction.
    """
    return update_issue(conn, issue_id, status=status)


def set_assignee(
    conn: sqlite3.Connection, issue_id: int, assignee_id: int | None
) -> dict | None:
    """Assign the issue to a user, or clear it (assignee_id=None -> Unassigned).
    Returns the updated issue, or None if no issue has that id. Checking that
    assignee_id is a real user is the boundary's job; the DB's foreign key is
    the backstop (raises sqlite3.IntegrityError on an unknown non-NULL id)."""
    cur = conn.execute(
        "UPDATE issues SET assignee_id = ? WHERE id = ?", (assignee_id, issue_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def set_sprint(
    conn: sqlite3.Connection, issue_id: int, sprint_id: int | None
) -> dict | None:
    """Put the issue in a sprint, or move it back to the backlog (sprint_id=None).
    Returns the updated issue, or None if no issue has that id. Like set_assignee:
    a single nullable column, so a dedicated op keeps None ('backlog') distinct from
    PATCH's 'leave unchanged'. Checking that the sprint exists and belongs to the
    issue's project is the boundary's job; the foreign key is the backstop (raises
    sqlite3.IntegrityError on an unknown non-NULL id)."""
    cur = conn.execute(
        "UPDATE issues SET sprint_id = ? WHERE id = ?", (sprint_id, issue_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_issue(conn, issue_id)


def set_archived(
    conn: sqlite3.Connection, issue_id: int, archived: bool
) -> dict | None:
    """Archive (soft-delete) an issue or restore it. Returns the updated issue, or
    None if no issue has that id. The row is never destroyed — archiving only stamps
    archived_at (and clearing it restores the issue), so the history is preserved and
    the default lists simply hide it. Idempotent: re-archiving an archived issue just
    re-stamps the time; the boundary records the audit fact only on a real change."""
    if archived:
        conn.execute(
            "UPDATE issues SET archived_at = datetime('now') WHERE id = ?", (issue_id,)
        )
    else:
        conn.execute(
            "UPDATE issues SET archived_at = NULL WHERE id = ?", (issue_id,)
        )
    conn.commit()
    return get_issue(conn, issue_id)


def set_project(
    conn: sqlite3.Connection, issue_id: int, project_id: int | None
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
    no key."""
    current = conn.execute(
        "SELECT project_id, status FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()
    if current is None:
        return None
    if current["project_id"] == project_id:
        return get_issue(conn, issue_id)  # same project — keep the existing number
    project_seq = projects.next_seq(conn, project_id) if project_id is not None else None
    # The destination may not offer the issue's current status (projects can have
    # different status sets). If so, remap to the destination's first status — a
    # defined, predictable rule — rather than leaving the issue in a status its new
    # project doesn't know. If the status is valid there, keep it.
    new_status = current["status"]
    if not statuses.is_valid(conn, project_id, new_status):
        new_status = statuses.first_status(conn, project_id)
    conn.execute(
        "UPDATE issues SET project_id = ?, project_seq = ?, status = ? WHERE id = ?",
        (project_id, project_seq, new_status, issue_id),
    )
    conn.commit()
    return get_issue(conn, issue_id)


def set_parent(
    conn: sqlite3.Connection, issue_id: int, parent_id: int | None
) -> dict | None:
    """Nest an issue under a parent (parent_id=None clears it to top-level).
    Returns the updated issue, or None if no issue has that id. Validation
    (existence, no self-parent, no cycle) is the boundary's job — see
    validate_parent; the FK is the backstop for a non-NULL unknown parent."""
    cur = conn.execute(
        "UPDATE issues SET parent_id = ? WHERE id = ?", (parent_id, issue_id)
    )
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
    NOT pass through here — this gate is for writes only."""
    return actor_id == issue["created_by"] or actor_id == issue["assignee_id"]


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
    if value.isdigit():
        return int(value), False
    return None


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

    Column names below are hardcoded literals; all values stay parameterized."""
    if ids is not None and not ids:
        return []  # an empty id set can't match — and "IN ()" isn't valid SQL
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
    query = f"{_SELECT}{where} ORDER BY i.id"
    if limit is not None:
        # Bound the result at the DB, not in Python — an unbounded caller must not be
        # able to make SQLite materialize the whole table. Only appended when a caller
        # opts in, so the uncapped internal callers are byte-for-byte unchanged.
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [_to_issue(row) for row in rows]
