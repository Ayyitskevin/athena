"""Canonical public issue representations and their strong HTTP validators."""

from __future__ import annotations

import sqlite3
from typing import Any

from athena.core import etag, labels

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


def representation(issue: dict, label_rows: list[dict]) -> dict[str, Any]:
    """Return exactly the JSON shape covered by ``IssueOut`` and its ETag.

    Keeping this projection outside the HTTP adapter lets the transactional issue
    command compare the same representation that a later response serializes.
    Extra joined/data-layer fields can never silently enter the validator.
    """
    public = {field: issue.get(field) for field in _ISSUE_FIELDS}
    public["labels"] = [
        {field: label.get(field) for field in _LABEL_FIELDS} for label in label_rows
    ]
    return public


def resource(conn: sqlite3.Connection, issue: dict) -> dict[str, Any]:
    """Load label state and build one canonical public issue representation."""
    return representation(issue, labels.labels_for_issue(conn, issue["id"]))


def representation_and_etag(
    issue: dict, label_rows: list[dict]
) -> tuple[dict[str, Any], str]:
    """Build one public representation and its strong validator.

    List-style surfaces that already loaded labels in bulk use this helper to avoid
    an N+1 query while keeping the canonical fields and ETag namespace here.
    """
    public = representation(issue, label_rows)
    return public, etag.strong_etag("issue-v1", public)


def resource_and_etag(
    conn: sqlite3.Connection, issue: dict
) -> tuple[dict[str, Any], str]:
    """Build one representation once, then derive its strong validator."""
    return representation_and_etag(issue, labels.labels_for_issue(conn, issue["id"]))


def current_etag(conn: sqlite3.Connection, issue: dict) -> str:
    """Return the strong validator for the issue's current public state."""
    return resource_and_etag(conn, issue)[1]
