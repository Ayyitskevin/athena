"""Canonical actor-visible issue representations and strong HTTP validators."""

from __future__ import annotations

import sqlite3
from typing import Any

from athena.core import access, etag, labels

_ISSUE_FIELDS = (
    "id",
    "key",
    "title",
    "body",
    "status",
    "priority",
    "created_by",
    "created_at",
    "assignee_id",
    "assignee_name",
    "assignee_is_agent",
    "project_id",
    "project_name",
    "parent_id",
    "sprint_id",
    "archived_at",
)
_LABEL_FIELDS = ("id", "name", "color")
_SQLITE_IN_CHUNK = 900


def _visible_parent_ids(
    conn: sqlite3.Connection, issue_rows: list[dict], actor: dict | None
) -> set[int]:
    """Return parent ids the actor may see without an issue-per-query lookup."""
    parent_ids = sorted(
        {
            parent_id
            for issue in issue_rows
            if isinstance((parent_id := issue.get("parent_id")), int)
        }
    )
    if not parent_ids:
        return set()

    visible_project_ids = access.visible_project_filter(conn, actor)
    visible: set[int] = set()
    for offset in range(0, len(parent_ids), _SQLITE_IN_CHUNK):
        chunk = parent_ids[offset : offset + _SQLITE_IN_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id, project_id FROM issues WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        visible.update(
            row["id"]
            for row in rows
            if visible_project_ids is None
            or row["project_id"] is None
            or row["project_id"] in visible_project_ids
        )
    return visible


def _representation(
    issue: dict, label_rows: list[dict], visible_parent_ids: set[int]
) -> dict[str, Any]:
    """Return exactly the actor-visible JSON shape covered by ``IssueOut``.

    Keeping this projection outside the HTTP adapter lets the transactional issue
    command compare the same representation that a later response serializes.
    Extra joined/data-layer fields can never silently enter the validator.
    """
    public = {field: issue.get(field) for field in _ISSUE_FIELDS}
    if public["parent_id"] not in visible_parent_ids:
        public["parent_id"] = None
    public["labels"] = [
        {field: label.get(field) for field in _LABEL_FIELDS} for label in label_rows
    ]
    return public


def resources_and_etags(
    conn: sqlite3.Connection,
    issue_rows: list[dict],
    *,
    actor: dict | None,
    label_rows_by_issue: dict[int, list[dict]] | None = None,
) -> list[tuple[dict[str, Any], str]]:
    """Build aligned actor-visible representations and validators in bulk.

    The root rows must already be visibility-gated by their caller. This projection
    independently redacts a parent the actor cannot see, then hashes exactly the JSON
    returned to that actor. Callers that already loaded labels may pass the grouped
    rows so list and board surfaces stay free of N+1 queries.
    """
    if not issue_rows:
        return []
    if label_rows_by_issue is None:
        label_rows_by_issue = labels.labels_for_issues(
            conn, [issue["id"] for issue in issue_rows]
        )
    visible_parent_ids = _visible_parent_ids(conn, issue_rows, actor)
    resources: list[tuple[dict[str, Any], str]] = []
    for issue in issue_rows:
        public = _representation(
            issue,
            label_rows_by_issue.get(issue["id"], []),
            visible_parent_ids,
        )
        resources.append((public, etag.strong_etag("issue-v1", public)))
    return resources


def resource_and_etag(
    conn: sqlite3.Connection, issue: dict, *, actor: dict | None
) -> tuple[dict[str, Any], str]:
    """Build one actor-visible representation and its strong validator."""
    return resources_and_etags(conn, [issue], actor=actor)[0]


def current_etag(conn: sqlite3.Connection, issue: dict, *, actor: dict | None) -> str:
    """Return the validator for the actor's current issue representation."""
    return resource_and_etag(conn, issue, actor=actor)[1]
