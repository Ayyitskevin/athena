"""Commands for floor rooms. Flavor only — no lease, status, or assignee change."""

from __future__ import annotations

import sqlite3

from athena.aegis import issues, projects, rooms
from athena.core import access, db


class RoomCommandError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _require_writer(actor: dict | None) -> dict:
    if actor is None:
        raise RoomCommandError("unauthorized", "sign in required")
    from athena.core import identity

    if not identity.can_write(actor):
        raise RoomCommandError("forbidden", "read-only actor")
    return actor


def create_room(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    project_id: int,
    name: str,
    blurb: str = "",
) -> dict:
    actor = _require_writer(actor)
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise RoomCommandError("not_found", "no such project")
    name = name.strip()
    if not name:
        raise RoomCommandError("invalid", "room name is required")
    slug = rooms.slugify(name)
    if slug is None:
        raise RoomCommandError("invalid", "room name does not make a usable slug")
    if rooms.get_room_by_slug(conn, project_id, slug) is not None:
        raise RoomCommandError("conflict", "that room already exists")
    with db.transaction(conn):
        return rooms.create_room(
            conn,
            project_id=project_id,
            name=name,
            slug=slug,
            blurb=blurb.strip(),
            commit=False,
        )


def stock_starter_rooms(
    conn: sqlite3.Connection, *, actor: dict | None, project_id: int
) -> list[dict]:
    """Opt-in Dunder Mifflin starter pack. No-op on names that already exist."""
    actor = _require_writer(actor)
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise RoomCommandError("not_found", "no such project")
    created: list[dict] = []
    with db.transaction(conn):
        for slug, name, blurb in rooms.STARTER_ROOMS:
            if rooms.get_room_by_slug(conn, project_id, slug) is not None:
                continue
            created.append(
                rooms.create_room(
                    conn,
                    project_id=project_id,
                    name=name,
                    slug=slug,
                    blurb=blurb,
                    commit=False,
                )
            )
    return created


def place_issue(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    room_id: int | None,
) -> dict:
    """Put an issue in a room, or None to send it back to the annex."""
    actor = _require_writer(actor)
    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        raise RoomCommandError("not_found", "no such issue")
    project_id = issue.get("project_id")
    if project_id is None:
        raise RoomCommandError("invalid", "backlog issues have no floor")
    if not access.can_see_project(conn, actor, int(project_id)):
        raise RoomCommandError("not_found", "no such issue")
    if room_id is not None:
        room = rooms.get_room(conn, room_id)
        if room is None or int(room["project_id"]) != int(project_id):
            raise RoomCommandError("invalid", "that room is not on this floor")
    with db.transaction(conn):
        rooms.set_issue_room(conn, issue_id, room_id, commit=False)
    updated = issues.get_issue(conn, issue_id)
    assert updated is not None
    return updated
