"""Canonical public project representations and their strong HTTP validators."""

from __future__ import annotations

from typing import Any

from athena.core import etag

_PROJECT_FIELDS = (
    "id",
    "name",
    "key",
    "description",
    "created_by",
    "created_at",
    "visibility",
    "block_agent_closes_when_blocked",
)


def representation(project: dict) -> dict[str, Any]:
    """Return exactly the JSON shape covered by ``ProjectOut`` and its ETag."""
    return {field: project.get(field) for field in _PROJECT_FIELDS}


def resource_and_etag(project: dict) -> tuple[dict[str, Any], str]:
    """Build one public project representation and its strong validator."""
    public = representation(project)
    return public, etag.strong_etag("project-v1", public)


def current_etag(project: dict) -> str:
    """Return the strong validator for the project's current public state."""
    return resource_and_etag(project)[1]
