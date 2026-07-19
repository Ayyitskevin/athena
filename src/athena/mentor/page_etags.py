"""Canonical public page representations and their strong HTTP validators.

The Mentor twin of aegis/issue_etags.py. Building the ETag from a projection that
lives OUTSIDE the HTTP adapter lets the transactional page-edit command compare the
exact representation a later response serializes — extra joined/data-layer columns
can never silently enter the validator, and REST and MCP agree on the tag by
construction.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from athena.core import etag, labels

_PAGE_FIELDS = (
    "id",
    "space_id",
    "parent_id",
    "title",
    "body",
    "created_by",
    "created_at",
    "updated_by",
    "updated_at",
)
_LABEL_FIELDS = ("id", "name", "color")


def representation(page: dict, label_rows: list[dict]) -> dict[str, Any]:
    """Return exactly the JSON shape covered by ``PageOut`` and its ETag.

    Only the public page columns plus the shared labels (id/name/color) — the same
    fields the API serializes — so the strong validator tracks precisely what a
    client can observe, no more.
    """
    public = {field: page.get(field) for field in _PAGE_FIELDS}
    public["labels"] = [
        {field: label.get(field) for field in _LABEL_FIELDS} for label in label_rows
    ]
    return public


def resource(conn: sqlite3.Connection, page: dict) -> dict[str, Any]:
    """Load label state and build one canonical public page representation."""
    return representation(page, labels.labels_for_page(conn, page["id"]))


def resource_and_etag(
    conn: sqlite3.Connection, page: dict
) -> tuple[dict[str, Any], str]:
    """Build one representation once, then derive its strong validator."""
    public = resource(conn, page)
    return public, etag.strong_etag("page-v1", public)


def current_etag(conn: sqlite3.Connection, page: dict) -> str:
    """Return the strong validator for the page's current public state."""
    return resource_and_etag(conn, page)[1]
