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

from athena.aegis import issues
from athena.core import labels

# The legal criteria dimensions — anything else in a submitted criteria object is
# dropped by normalize_criteria, so a stored filter can only constrain what the
# issue list actually supports and can always be run. The string-valued keys live
# in _STRING_KEYS; assignee_id (the one int-valued key) is normalized separately
# in normalize_criteria.
#   status      — exact status (free string; an unknown status simply matches none)
#   priority    — exact priority (validated against issues.PRIORITIES)
#   assignee_id — exact assignee user id (an unknown user matches none)
#   label       — label name resolved to issue ids (an unknown label matches none)
#   project     — "none" (backlog) or a project id, via issues.parse_project_filter
#   search      — case-insensitive substring in title/body
_STRING_KEYS = ("status", "priority", "label", "project", "search")


class InvalidFilterCriteria(ValueError):
    """A supplied saved-filter criterion cannot be represented safely."""


def normalize_criteria(raw: dict | None) -> dict:
    """Clean a raw criteria mapping into the canonical stored form.

    Keeps only known keys; strips strings and drops empty ones; canonicalizes a
    valid assignee_id to an int. A supplied assignee that is not an exact integer
    (or an ASCII-decimal web-form string) in SQLite's id range is rejected rather
    than dropped: silently dropping it would widen the saved query to every
    assignee. The result is what we persist and what run_filter consumes, so the
    web form and JSON API land on the SAME shape. An empty result is legal — it
    means "every issue"."""
    raw = raw or {}
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
    return None


def normalized_valid_criteria(raw: dict | None) -> dict | None:
    """Return canonical criteria for a read, or None when the row is invalid.

    Writers surface precise validation errors before persistence. Readers need a
    fail-closed form instead: a malformed stored row must match nothing, while a
    valid empty mapping must remain distinguishable as the intentional
    "all issues" filter.
    """
    try:
        criteria = normalize_criteria(raw)
    except InvalidFilterCriteria:
        return None
    return criteria if validate_criteria(criteria) is None else None


def run_filter(
    conn: sqlite3.Connection,
    criteria: dict,
    *,
    visible_project_ids: set[int] | None = None,
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
    mapping so every caller works with the structured form, never the raw text."""
    out = dict(row)
    try:
        out["criteria"] = json.loads(out["criteria"]) if out["criteria"] else {}
    except (TypeError, ValueError):
        out["criteria"] = {}
    return out


def create_filter(
    conn: sqlite3.Connection, *, owner_id: int, name: str, criteria: dict
) -> dict:
    """Insert a filter and return it. criteria is normalized before storage. Raises
    sqlite3.IntegrityError if the owner already has a filter with this name
    (UNIQUE per owner, case-insensitive) — the boundary maps that to 409."""
    payload = json.dumps(normalize_criteria(criteria))
    cur = conn.execute(
        "INSERT INTO saved_filters (owner_id, name, criteria) VALUES (?, ?, ?)",
        (owner_id, name, payload),
    )
    conn.commit()
    return get_filter(conn, cur.lastrowid)


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
    name: str | None = None,
    criteria: dict | None = None,
) -> dict | None:
    """Partial update: only the fields passed as non-None change. Returns the
    updated filter, or None if no filter has that id. criteria, when given, is
    normalized. Bumps updated_at on any real change. Raises sqlite3.IntegrityError
    on a rename that collides with another of the owner's filters."""
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if criteria is not None:
        sets.append("criteria = ?")
        params.append(json.dumps(normalize_criteria(criteria)))
    if not sets:
        return get_filter(conn, filter_id)
    sets.append("updated_at = datetime('now')")
    cur = conn.execute(
        f"UPDATE saved_filters SET {', '.join(sets)} WHERE id = ?",
        (*params, filter_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_filter(conn, filter_id)


def delete_filter(conn: sqlite3.Connection, filter_id: int) -> bool:
    """Delete a filter. Returns False if no filter had that id (so the caller can
    404). Ownership is the boundary's concern — it fetches-and-checks first."""
    cur = conn.execute("DELETE FROM saved_filters WHERE id = ?", (filter_id,))
    conn.commit()
    return cur.rowcount > 0
