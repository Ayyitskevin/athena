"""Data access for saved issue filters — named, reusable issue queries.

A saved filter is "query-lite": a small set of optional criteria that, run
together, return the issues matching ALL of them. Filters belong to the user who
created them — a personal, re-runnable "what should I look at" handle for humans
and agents alike.

This module owns three things:

  * STORAGE — CRUD over the saved_filters table (criteria stored as JSON);
  * the CRITERIA CONTRACT — what keys are legal, how raw input is normalized,
    and what counts as a valid value (normalize_criteria + validate_criteria),
    shared by the REST API and the web form so the two can't drift;
  * RUNNING — run_filter turns stored criteria into a list of issues by funnelling
    through issues.list_issues, the same path the ad-hoc issue list uses. A saved
    query and an ad-hoc query therefore can never disagree on what matches.

It lives in the aegis lane and leans on its neighbours (issues, labels) to run a
filter; it never reaches outside the module.
"""

from __future__ import annotations

import json
import sqlite3

from athena.aegis import issue_query, issues
from athena.core import db, labels, work_query

# The legal criteria dimensions — anything else in a submitted criteria object is
# rejected by normalize_criteria, so a stored filter can only constrain what the
# issue list actually supports and can always be run. The string-valued keys live
# in _STRING_KEYS; assignee_id (the one int-valued key) is normalized separately
# in normalize_criteria.
#   status      — exact status (free string; an unknown status simply matches none)
#   priority    — exact priority (validated against issues.PRIORITIES)
#   assignee_id — exact assignee user id (an unknown user matches none)
#   label       — label name resolved to issue ids (an unknown label matches none)
#   project     — "none" (backlog) or a project id, via issues.parse_project_filter
#   search      — case-insensitive substring in title/body
#   query       — a work-query string (core.work_query); when present it REPLACES
#                 the structured dimensions rather than narrowing them, so a saved
#                 filter is either a query or a set of fields, never a confusing
#                 intersection of both.
_STRING_KEYS = ("status", "priority", "label", "project", "search", "query")
_CRITERIA_KEYS = frozenset((*_STRING_KEYS, "assignee_id"))

#: The structured dimensions ``query`` supersedes. Named once so the validation
#: message and the run path cannot disagree about which keys conflict.
_STRUCTURED_KEYS = ("status", "priority", "label", "project", "search", "assignee_id")


#: A query-backed filter pages at the DB, so it needs a bound where the
#: structured path (which returns every match to its uncapped internal callers)
#: has none. Generous enough to serve a board, small enough that a saved filter
#: cannot be used to materialize the whole table.
MAX_QUERY_RESULTS = 500


class InvalidFilterCriteria(ValueError):
    """A supplied saved-filter criterion cannot be represented safely."""


def normalize_criteria(raw: dict | None) -> dict:
    """Clean a raw criteria mapping into the canonical stored form.

    Rejects unknown keys; strips strings and drops empty ones; canonicalizes a
    valid assignee_id to an int. A supplied assignee that is not an exact integer
    (or an ASCII-decimal web-form string) in SQLite's id range is rejected rather
    than dropped: silently dropping it would widen the saved query to every
    assignee. The result is what we persist and what run_filter consumes, so the
    web form and JSON API land on the SAME shape. An empty result is legal — it
    means "every issue"."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise InvalidFilterCriteria("filter criteria must be an object")
    if not set(raw).issubset(_CRITERIA_KEYS):
        raise InvalidFilterCriteria("unknown filter criterion")
    out: dict = {}
    for key in _STRING_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    assignee = raw.get("assignee_id")
    if assignee is not None and str(assignee).strip() != "":
        parsed_assignee: int | None
        if type(assignee) is int:
            parsed_assignee = assignee
        elif isinstance(assignee, str):
            parsed_assignee = issues.parse_filter_id(assignee)
        else:
            parsed_assignee = None
        if not issues.is_filter_id(parsed_assignee):
            raise InvalidFilterCriteria("invalid assignee filter")
        out["assignee_id"] = parsed_assignee
    return out


def validate_criteria(criteria: dict) -> str | None:
    """Whether a normalized criteria mapping is acceptable. Returns None if OK,
    else a human reason the boundary turns into a 422/400.

    Only the closed-set dimensions are validated: priority must be a real priority,
    and project must be a parseable project filter ("none" or a numeric id). The
    open dimensions (status, label, assignee_id) are deliberately NOT existence-
    checked — assignee type/range is enforced during normalization, but an unknown
    in-range user id remains a valid query that simply matches nothing."""
    priority = criteria.get("priority")
    if priority is not None and priority not in issues.PRIORITIES:
        return "no such priority"
    project = criteria.get("project")
    if project is not None and issues.parse_project_filter(project) is None:
        return "invalid project filter"
    query = criteria.get("query")
    if query is not None:
        clashing = sorted(k for k in _STRUCTURED_KEYS if k in criteria)
        if clashing:
            return (
                "query cannot be combined with the structured criteria "
                f"({', '.join(clashing)}); express them in the query"
            )
        # Validated at WRITE time so a saved filter cannot be persisted in a shape
        # that fails every time it is run — the reader (normalized_valid_criteria)
        # fails closed and would silently match nothing, which is exactly the
        # "empty result for a broken query" the grammar exists to prevent.
        try:
            work_query.parse(query)
        except work_query.QueryError as exc:
            return str(exc)
    return None


def normalized_valid_criteria(raw: dict | None) -> dict | None:
    """Return canonical criteria for a read, or None when the row is invalid.

    Writers surface precise validation errors before persistence. Readers need a
    fail-closed form instead: a malformed stored row must match nothing, while a
    valid empty mapping must remain distinguishable as the intentional
    "all issues" filter.
    """
    # Damaged/imported storage may hold malformed JSON, a non-object, or a key this
    # version does not understand. None of those may collapse to the valid empty
    # mapping: that would turn an unknown constraint into "all issues".
    if not isinstance(raw, dict) or not set(raw).issubset(_CRITERIA_KEYS):
        return None

    try:
        criteria = normalize_criteria(raw)
    except InvalidFilterCriteria:
        return None
    return criteria if validate_criteria(criteria) is None else None


def run_filter(
    conn: sqlite3.Connection,
    criteria: dict | None,
    *,
    visible_project_ids: set[int] | None = None,
    actor: dict | None = None,
    limit: int = MAX_QUERY_RESULTS,
) -> list[dict]:
    """Resolve criteria into the issues that match, via issues.list_issues.

    Re-normalizes defensively (stored criteria is already clean, but a caller may
    hand in a raw dict). A label name is resolved to issue ids here so issues.py
    stays decoupled from the label join; the project string is parsed into the
    (project_id, backlog) the list path expects. visible_project_ids is the
    visibility gate, threaded straight to list_issues — None means no gating (an
    admin or an internal caller); the callers get it from
    access.visible_project_filter, so a saved filter never surfaces an issue in a
    project the runner can't see."""
    crit = normalized_valid_criteria(criteria)
    if crit is None:
        # Dropping a malformed dimension would turn a narrow query into a broad
        # one; passing an oversized id through could overflow sqlite3.
        return []
    if "query" in crit:
        # A query filter runs through the query compiler, which applies the SAME
        # visibility clause list_issues does. `actor` is needed only for `@me`;
        # a stored filter naming it, run without one, fails closed to no results
        # rather than silently widening to every assignee.
        try:
            compiled = work_query.parse(crit["query"])
            return issue_query.run_query(
                conn,
                compiled,
                actor=actor,
                visible_project_ids=visible_project_ids,
                limit=limit,
            )
        except (work_query.QueryError, issue_query.QueryCompileError):
            return []
    project_id: int | None = None
    backlog = False
    if "project" in crit:
        parsed = issues.parse_project_filter(crit["project"])
        if parsed is not None:  # invalid is caught at write time; ignore on read
            project_id, backlog = parsed
    ids = labels.issue_ids_for_label(conn, crit["label"]) if "label" in crit else None
    return issues.list_issues(
        conn,
        status=crit.get("status"),
        priority=crit.get("priority"),
        assignee_id=crit.get("assignee_id"),
        search=crit.get("search"),
        project_id=project_id,
        backlog=backlog,
        ids=ids,
        visible_project_ids=visible_project_ids,
    )


# --- storage ----------------------------------------------------------------


def _to_filter(row: sqlite3.Row) -> dict:
    """Turn a saved_filters row into a dict, decoding criteria JSON back to a
    mapping when it is an object, or None for malformed/non-object JSON."""
    out = dict(row)
    try:
        decoded = json.loads(out["criteria"]) if out["criteria"] else None
    except (TypeError, ValueError):
        decoded = None
    out["criteria"] = decoded if isinstance(decoded, dict) else None
    return out


def create_filter(
    conn: sqlite3.Connection, *, owner_id: int, name: str, criteria: dict
) -> dict:
    """Insert a filter and return it. criteria is normalized before storage. Raises
    sqlite3.IntegrityError if the owner already has a filter with this name
    (UNIQUE per owner, case-insensitive) — the boundary maps that to 409.

    One immediate transaction, no self-commit: saved filters are PERSONAL STATE
    (see docs/COMMAND_MIGRATION.md), so this module is the single writer and the
    mutation is owner-scoped by construction (owner_id is an INSERT column)."""
    payload = json.dumps(normalize_criteria(criteria))
    with db.transaction(conn, immediate=True):
        cur = conn.execute(
            "INSERT INTO saved_filters (owner_id, name, criteria) VALUES (?, ?, ?)",
            (owner_id, name, payload),
        )
        filter_id = cur.lastrowid
        assert filter_id is not None
        saved_filter = get_filter(conn, filter_id)
        assert saved_filter is not None
        return saved_filter


def get_filter(conn: sqlite3.Connection, filter_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM saved_filters WHERE id = ?", (filter_id,)
    ).fetchone()
    return _to_filter(row) if row else None


def list_filters(conn: sqlite3.Connection, owner_id: int) -> list[dict]:
    """A user's saved filters, alphabetical by name."""
    rows = conn.execute(
        "SELECT * FROM saved_filters WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
        (owner_id,),
    ).fetchall()
    return [_to_filter(row) for row in rows]


def update_filter(
    conn: sqlite3.Connection,
    filter_id: int,
    *,
    owner_id: int,
    name: str | None = None,
    criteria: dict | None = None,
) -> dict | None:
    """Partial update: only the fields passed as non-None change. Returns the
    updated filter, or None if no filter has that id FOR THIS OWNER. criteria,
    when given, is normalized. Bumps updated_at on any real change. Raises
    sqlite3.IntegrityError on a rename that collides with another of the owner's
    filters.

    Owner-scoped in SQL (``WHERE id = ? AND owner_id = ?``) inside one immediate
    transaction: a filter that isn't the caller's is indistinguishable from a
    missing one (0 rows → None → the boundary's 404), and no fetch-then-check
    outside a transaction can land a stale ownership verdict on a row id that —
    through SQLite rowid reuse — now belongs to another user."""
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if criteria is not None:
        sets.append("criteria = ?")
        params.append(json.dumps(normalize_criteria(criteria)))
    with db.transaction(conn, immediate=True):
        if not sets:
            row = conn.execute(
                "SELECT * FROM saved_filters WHERE id = ? AND owner_id = ?",
                (filter_id, owner_id),
            ).fetchone()
            return _to_filter(row) if row else None
        sets.append("updated_at = datetime('now')")
        cur = conn.execute(
            f"UPDATE saved_filters SET {', '.join(sets)} WHERE id = ? AND owner_id = ?",
            (*params, filter_id, owner_id),
        )
        if cur.rowcount == 0:
            return None
        return get_filter(conn, filter_id)


def delete_filter(conn: sqlite3.Connection, filter_id: int, *, owner_id: int) -> bool:
    """Delete a filter owned by owner_id. Returns False when no row matched —
    missing OR another user's, which are indistinguishable — so the boundary
    404s. Owner-scoped in SQL inside one immediate transaction: ownership is
    enforced by the mutation itself, not by a fetch-then-check a rowid-reuse
    race can stale."""
    with db.transaction(conn, immediate=True):
        cur = conn.execute(
            "DELETE FROM saved_filters WHERE id = ? AND owner_id = ?",
            (filter_id, owner_id),
        )
        return cur.rowcount > 0
