"""Application commands for the audited project lifecycle.

A project is a workspace container: creating one, renaming/re-keying it, or
deleting it are exactly the "a container appeared or vanished — who did it?"
moments the append-only log exists to answer, yet the bare data-layer writes
recorded nothing. These commands own each write: the row change and its activity
event run in one db.transaction, so a project can never be created, edited, or
destroyed without its trail entry.

Same shape as the other command modules (user_commands, comment_commands, ...).
Authorization and the duplicate-name/key and non-empty-issues preconditions stay
at the transport boundary (the REST/web routes already enforce them and turn a
clash into a clean 409); the command owns the atomic write + audit. Visibility
changes and membership already have their own audited path in project_activity;
this fills the create / edit / delete gap.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import project_activity, project_etags, projects
from athena.core import access, db, etag, identity, tokens, users

_POLICY_ERROR_KINDS = (
    "not_found",
    "forbidden",
    "precondition_required",
    "invalid_precondition",
    "precondition_too_large",
    "precondition_failed",
)


class ProjectPolicyCommandError(Exception):
    """A framework-neutral project-governance rejection."""

    def __init__(
        self, kind: str, detail: str, *, current_etag: str | None = None
    ) -> None:
        super().__init__(detail)
        if kind not in _POLICY_ERROR_KINDS:
            raise ValueError(f"unknown ProjectPolicyCommandError kind: {kind}")
        self.kind = kind
        self.detail = detail
        self.current_etag = current_etag


def create_project(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    name: str,
    key: str,
    description: str = "",
) -> dict:
    """Create a project (with its default statuses) and record a 'created_project'
    event atomically. Raises sqlite3.IntegrityError for a duplicate name/key —
    unchanged from the bare call, so the boundary keeps turning it into a 409."""
    with db.transaction(conn, immediate=True):
        project = projects.create_project(
            conn,
            name=name,
            key=key,
            created_by=actor_id,
            description=description,
            commit=False,
        )
        project_activity.record_project_created(
            conn,
            actor_id=actor_id,
            project_id=project["id"],
            name=project["name"],
            key=project["key"],
        )
        return project


def _edit_summary(before: dict, after: dict) -> str:
    """Human-readable diff of the fields an edit can touch, for the event detail.
    Empty when nothing actually changed (the caller then records nothing)."""
    parts = []
    if before["name"] != after["name"]:
        parts.append(f"name {before['name']!r} → {after['name']!r}")
    if before["key"] != after["key"]:
        parts.append(f"key {before['key']} → {after['key']}")
    if before.get("description", "") != after.get("description", ""):
        parts.append("description")
    return ", ".join(parts)


def update_project(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    project_id: int,
    name: str | None = None,
    key: str | None = None,
    description: str | None = None,
) -> dict | None:
    """Edit a project and record an 'edited_project' event atomically. Returns the
    updated project, or None if no project has that id (so the boundary can 404).
    No event when nothing actually changed — a no-op edit is not a lifecycle
    moment. Raises sqlite3.IntegrityError for a name/key collision, unchanged."""
    with db.transaction(conn, immediate=True):
        before = projects.get_project(conn, project_id)
        if before is None:
            return None
        updated = projects.update_project(
            conn,
            project_id,
            name=name,
            key=key,
            description=description,
            commit=False,
        )
        assert updated is not None
        summary = _edit_summary(before, updated)
        if summary:
            project_activity.record_project_edited(
                conn, actor_id=actor_id, project_id=project_id, changes=summary
            )
        return updated


def delete_project(conn: sqlite3.Connection, *, actor_id: int, project_id: int) -> bool:
    """Delete a project and record a 'deleted_project' event atomically. Returns
    True if a project was deleted, False if none had that id.

    The event is recorded BEFORE the row is removed so its visibility scope still
    resolves from the live project; the event then OUTLIVES the target it names
    (activity.target_id has no FK), preserving who deleted which container."""
    with db.transaction(conn, immediate=True):
        project = projects.get_project(conn, project_id)
        if project is None:
            return False
        project_activity.record_project_deleted(
            conn,
            actor_id=actor_id,
            project_id=project_id,
            name=project["name"],
            key=project["key"],
        )
        return projects.delete_project(conn, project_id, commit=False)


class ProjectAccessCommandError(Exception):
    """A transport-neutral project-access rejection. ``status_code`` lets each adapter
    map it (404 for a vanished project, 404 for a non-member removal)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def set_project_visibility(
    conn: sqlite3.Connection, *, actor_id: int, project_id: int, visibility: str
) -> dict:
    """Flip a project public↔private atomically with its ``project_made_private`` /
    ``project_made_public`` event — the Aegis twin of
    ``space_commands.set_space_visibility``.

    Going private also inserts the creator as an explicit member (they keep access via
    ``created_by`` regardless; this surfaces them in the roster), folded into the SAME
    transaction. Previously the flip, that membership row, and the event were THREE
    independent commits, so a crash mid-sequence left a permanently unaudited
    access-control change — a project could become private with nothing on the
    append-only trail saying who narrowed it, or (worse) go private without its
    creator's roster row.

    The boundary validates the value and skips a no-op set-to-same, so this is only
    called on a real transition. Raises ``ProjectAccessCommandError(404)`` if the
    project vanished before the write."""
    with db.transaction(conn, immediate=True):
        before = projects.get_project(conn, project_id)
        if before is None:
            raise ProjectAccessCommandError("no such project", status_code=404)
        updated = projects.set_visibility(conn, project_id, visibility, commit=False)
        if updated is None:
            raise ProjectAccessCommandError("no such project", status_code=404)
        if visibility == "private":
            access.add_project_member(
                conn,
                project_id,
                before["created_by"],
                added_by=actor_id,
                commit=False,
            )
        project_activity.record_project_visibility_changed(
            conn,
            actor_id=actor_id,
            project_id=project_id,
            name=updated["name"],
            visibility=visibility,
            commit=False,
        )
        return updated


def add_project_member(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    project_id: int,
    user_id: int,
    member_name: str,
) -> bool:
    """Grant a user read access to a private project atomically with its
    ``project_member_added`` event. Idempotent: a re-add records nothing and returns
    False. The boundary validates that the user exists (422) and passes the member's
    name for the audit detail. Previously the grant and its event were two commits, so
    a crash between them left an unaudited access grant."""
    with db.transaction(conn, immediate=True):
        added = access.add_project_member(
            conn, project_id, user_id, added_by=actor_id, commit=False
        )
        if added:
            project_activity.record_project_member_added(
                conn,
                actor_id=actor_id,
                project_id=project_id,
                member_name=member_name,
                commit=False,
            )
        return added


def remove_project_member(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    project_id: int,
    user_id: int,
    member_name: str,
) -> bool:
    """Revoke a user's access to a private project atomically with its
    ``project_member_removed`` event. Returns True if a row was removed; no event for a
    removal that didn't happen (the REST boundary 404s a non-member, the browser treats
    it as a no-op). The boundary passes the member's name for the audit detail, since
    the membership row is gone by the time it reads. Previously the revoke and its
    event were two commits, so a crash between them left an unaudited access
    revocation."""
    with db.transaction(conn, immediate=True):
        removed = access.remove_project_member(conn, project_id, user_id, commit=False)
        if removed:
            project_activity.record_project_member_removed(
                conn,
                actor_id=actor_id,
                project_id=project_id,
                member_name=member_name,
                commit=False,
            )
        return removed


def set_blocked_close_policy(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    project_id: int,
    enabled: bool,
    if_match: list[str] | None,
) -> dict:
    """Set the optional blocked-close policy under one guarded write lock.

    Authorization is command-owned and evaluated from live rows: only a human
    project creator or human admin with a write-capable role may configure the
    policy. A hidden project remains indistinguishable from a missing one.

    The exact reviewed project representation is required through If-Match. The
    precondition check, flag mutation, audit event, visibility envelope,
    notifications, and ambient run lineage all commit or roll back together.
    """
    with db.transaction(conn, immediate=True):
        project = projects.get_project(conn, project_id)
        live_actor = users.get_user(conn, actor["id"])
        effective_actor = {**actor, **live_actor} if live_actor is not None else None
        if project is None or not access.can_see_project(
            conn, effective_actor, project_id
        ):
            raise ProjectPolicyCommandError("not_found", "no such project")
        if (
            effective_actor is None
            or effective_actor.get("is_agent")
            or effective_actor.get("paused_at")
            or effective_actor.get("removed_at")
            or not identity.can_write(effective_actor)
            or not identity.token_has_scope(effective_actor, tokens.ISSUE_WRITE_SCOPE)
            or (
                project["created_by"] != effective_actor["id"]
                and effective_actor.get("role") != users.ADMIN_ROLE
            )
        ):
            raise ProjectPolicyCommandError(
                "forbidden",
                "only a human project creator or human admin may configure this policy",
            )

        current = project_etags.current_etag(project)
        if if_match is None:
            raise ProjectPolicyCommandError(
                "precondition_required",
                "If-Match with the current project ETag is required",
                current_etag=current,
            )
        try:
            condition = etag.parse_if_match(if_match)
            reviewed_etag = condition.single_strong_tag()
        except etag.IfMatchTooLarge as exc:
            raise ProjectPolicyCommandError(
                "precondition_too_large", str(exc), current_etag=current
            ) from exc
        except etag.InvalidIfMatch as exc:
            raise ProjectPolicyCommandError(
                "invalid_precondition", str(exc), current_etag=current
            ) from exc
        if reviewed_etag != current:
            raise ProjectPolicyCommandError(
                "precondition_failed",
                "If-Match precondition failed",
                current_etag=current,
            )

        if project["block_agent_closes_when_blocked"] == enabled:
            return project
        updated = projects.set_blocked_close_policy(
            conn, project_id, enabled=enabled, commit=False
        )
        assert updated is not None
        project_activity.record_project_blocked_close_policy_changed(
            conn,
            actor_id=effective_actor["id"],
            project_id=project_id,
            enabled=enabled,
            commit=False,
        )
        return updated
