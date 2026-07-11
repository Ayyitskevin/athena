"""Application commands for audited Aegis issue writes.

HTTP is only a transport concern. The REST and browser adapters both call this
module so authorization, normalization, validation, persistence, projections,
and audit emission have one owner. Each command holds one SQLite transaction:
the issue row, cross-links, search index, activity event, watches, and mentions
either all become durable or all roll back.

The migration is intentionally vertical. Create, the editable core fields
(title/body/status/priority), assignee, project, and sprint now live here; the
remaining issue mutations can move command-by-command without a flag day.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from athena.aegis import issue_activity, issues, sprints, statuses
from athena.core import access, db, identity, tokens, users

ErrorKind = Literal["unauthorized", "forbidden", "not_found", "invalid"]


class IssueCommandError(Exception):
    """A transport-neutral command rejection.

    Adapters translate ``kind`` into their own status vocabulary (REST uses 422
    for invalid input while an HTML form uses 400) without reimplementing the
    business rule that produced ``detail``.
    """

    def __init__(self, kind: ErrorKind, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class _UnsetType:
    __slots__ = ()


UNSET = _UnsetType()

# The only internal actor allowed to bypass per-issue creator/assignee policy.
# It has no password or token and is created by the in-process automation engine.
AUTOMATION_ACTOR_EMAIL = "automation@athena.system"


def _require_issue_writer(actor: dict | None) -> dict:
    if actor is None:
        raise IssueCommandError("unauthorized", "authentication required")
    if not identity.can_write(actor):
        raise IssueCommandError("forbidden", "viewer role is read-only")
    if not identity.token_has_scope(actor, tokens.ISSUE_WRITE_SCOPE):
        raise IssueCommandError(
            "forbidden", f"token scope required: {tokens.ISSUE_WRITE_SCOPE}"
        )
    return actor


def _visible_issue(
    conn: sqlite3.Connection, actor: dict, issue_id: int
) -> dict:
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise IssueCommandError("not_found", "no such issue")
    return issue


def _modifiable_issue(issue: dict, actor: dict) -> dict:
    if not issues.can_modify(issue, actor["id"]):
        raise IssueCommandError(
            "forbidden", "only the issue creator or assignee may modify it"
        )
    return issue


def _writable_issue(
    conn: sqlite3.Connection, actor: dict, issue_id: int
) -> dict:
    return _modifiable_issue(_visible_issue(conn, actor, issue_id), actor)


def get_writable_issue(
    conn: sqlite3.Connection, *, actor: dict | None, issue_id: int
) -> dict:
    """Return the issue this actor may modify, using the command policy owner.

    Browser forms use this before rendering edit/close-confirmation UI. Mutating
    commands repeat the check under their write transaction so this convenience
    read is never treated as authorization for a later write.
    """
    if actor is None:
        raise IssueCommandError("unauthorized", "authentication required")
    # Preserve the browser's visibility-first contract: a hidden or missing
    # issue returns the same 404 for every signed-in actor. Only a visible issue
    # proceeds to the role/scope and creator-or-assignee checks.
    issue = _visible_issue(conn, actor, issue_id)
    _require_issue_writer(actor)
    return _modifiable_issue(issue, actor)


def create_issue(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    title: str,
    body: str = "",
    status: str | None = None,
    priority: str = "medium",
    project_id: int | None = None,
) -> dict:
    """Create one issue and its first audit/projection state atomically."""
    actor = _require_issue_writer(actor)
    if not isinstance(title, str):
        raise IssueCommandError("invalid", "title must be a string")
    if not isinstance(body, str):
        raise IssueCommandError("invalid", "body must be a string")
    if status is not None and not isinstance(status, str):
        raise IssueCommandError("invalid", "status must be a string")
    if not isinstance(priority, str):
        raise IssueCommandError("invalid", "priority must be a string")
    if project_id is not None and (
        not isinstance(project_id, int) or isinstance(project_id, bool)
    ):
        raise IssueCommandError("invalid", "project_id must be an integer")
    title = title.strip()
    if not title:
        raise IssueCommandError("invalid", "title cannot be empty")
    if priority not in issues.PRIORITIES:
        raise IssueCommandError("invalid", "unknown priority")

    with db.transaction(conn, immediate=True):
        # Unknown and invisible projects deliberately collapse into one error so
        # a write cannot probe a private container's existence.
        if project_id is not None and not access.can_see_project(
            conn, actor, project_id
        ):
            raise IssueCommandError("invalid", "no such project")
        if status is None:
            status = statuses.first_status(conn, project_id)
        elif not statuses.is_valid(conn, project_id, status):
            raise IssueCommandError(
                "invalid", "no such status for this project"
            )
        issue = issues.create_issue(
            conn,
            title=title,
            body=body,
            status=status,
            priority=priority,
            project_id=project_id,
            created_by=actor["id"],
            commit=False,
        )
        issue_activity.record_created(
            conn,
            actor_id=actor["id"],
            issue_id=issue["id"],
            body=issue["body"],
            commit=False,
        )
    return issue


def update_issue(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    title: str | None | _UnsetType = UNSET,
    body: str | None | _UnsetType = UNSET,
    status: str | None | _UnsetType = UNSET,
    priority: str | None | _UnsetType = UNSET,
    assignee_id: int | None | _UnsetType = UNSET,
    project_id: int | None | _UnsetType = UNSET,
    sprint_id: int | None | _UnsetType = UNSET,
) -> dict:
    """Update editable issue fields and all resulting audit facts atomically."""
    actor = _require_issue_writer(actor)
    return _update_issue(
        conn,
        actor=actor,
        issue_id=issue_id,
        enforce_actor_policy=True,
        title=title,
        body=body,
        status=status,
        priority=priority,
        assignee_id=assignee_id,
        project_id=project_id,
        sprint_id=sprint_id,
    )


def update_issue_as_automation(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    title: str | None | _UnsetType = UNSET,
    body: str | None | _UnsetType = UNSET,
    status: str | None | _UnsetType = UNSET,
    priority: str | None | _UnsetType = UNSET,
    assignee_id: int | None | _UnsetType = UNSET,
) -> dict:
    """Apply a rule action through the same command under explicit system policy.

    Automation historically acts on the target selected by an enabled rule rather
    than as its creator/assignee. Keep that deliberate bypass narrow: only the
    passwordless in-process Automation agent may use it; browser/API actors always
    go through :func:`update_issue` and normal role/scope/visibility checks.
    """
    actor = users.get_user(conn, actor_id)
    if (
        actor is None
        or actor.get("email") != AUTOMATION_ACTOR_EMAIL
        or not actor.get("is_agent")
    ):
        raise IssueCommandError("forbidden", "automation actor required")
    return _update_issue(
        conn,
        actor=actor,
        issue_id=issue_id,
        enforce_actor_policy=False,
        title=title,
        body=body,
        status=status,
        priority=priority,
        assignee_id=assignee_id,
        project_id=UNSET,
        sprint_id=UNSET,
    )


def _string_value(name: str, value: str | None | _UnsetType) -> str | None:
    if value is UNSET or value is None:
        return None
    if not isinstance(value, str):
        raise IssueCommandError("invalid", f"{name} must be a string")
    return value


def _nullable_int_value(
    name: str, value: int | None | _UnsetType
) -> int | None:
    if value is UNSET:
        return None
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise IssueCommandError("invalid", f"{name} must be an integer or null")
    return value


def _update_issue(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    issue_id: int,
    enforce_actor_policy: bool,
    title: str | None | _UnsetType,
    body: str | None | _UnsetType,
    status: str | None | _UnsetType,
    priority: str | None | _UnsetType,
    assignee_id: int | None | _UnsetType,
    project_id: int | None | _UnsetType,
    sprint_id: int | None | _UnsetType,
) -> dict:
    with db.transaction(conn, immediate=True):
        before = (
            _writable_issue(conn, actor, issue_id)
            if enforce_actor_policy
            else issues.get_issue(conn, issue_id)
        )
        if before is None:
            raise IssueCommandError("not_found", "no such issue")

        # Preserve the historical and security-relevant ordering: resolve the
        # target/authorization before disclosing whether submitted fields are valid.
        provided = {
            name
            for name, value in (
                ("title", title),
                ("body", body),
                ("status", status),
                ("priority", priority),
                ("assignee_id", assignee_id),
                ("project_id", project_id),
                ("sprint_id", sprint_id),
            )
            if value is not UNSET
        }
        if not provided:
            raise IssueCommandError("invalid", "no fields to update")

        project_value = _nullable_int_value("project_id", project_id)
        final_project_id = (
            project_value if project_id is not UNSET else before["project_id"]
        )
        project_changed = final_project_id != before["project_id"]
        if (
            project_id is not UNSET
            and project_value is not None
            and not access.can_see_project(conn, actor, project_value)
        ):
            raise IssueCommandError("invalid", "no such project")

        sprint_value = _nullable_int_value("sprint_id", sprint_id)
        if sprint_id is not UNSET and sprint_value is not None:
            sprint = sprints.get_sprint(conn, sprint_value)
            if sprint is None or not access.can_see_project(
                conn, actor, sprint["project_id"]
            ):
                raise IssueCommandError("invalid", "no such sprint")
            if sprint["project_id"] != final_project_id:
                raise IssueCommandError(
                    "invalid",
                    "sprint belongs to a different project than the issue",
                )

        assignee_value = _nullable_int_value("assignee_id", assignee_id)
        if (
            assignee_id is not UNSET
            and assignee_value is not None
            and users.get_user(conn, assignee_value) is None
        ):
            raise IssueCommandError("invalid", "no such user")

        title_value = _string_value("title", title)
        normalized_title = title_value.strip() if title_value is not None else None
        if title_value is not None and not normalized_title:
            raise IssueCommandError("invalid", "title cannot be empty")
        body_value = _string_value("body", body)
        status_value = _string_value("status", status)
        priority_value = _string_value("priority", priority)
        if priority_value is not None and priority_value not in issues.PRIORITIES:
            raise IssueCommandError("invalid", "unknown priority")
        if status_value is not None and not statuses.is_valid(
            conn, final_project_id, status_value
        ):
            raise IssueCommandError(
                "invalid", "no such status for this project"
            )

        if "project_id" in provided:
            updated = issues.set_project(
                conn,
                issue_id,
                project_value,
                commit=False,
            )
            if updated is None:
                raise IssueCommandError("not_found", "no such issue")
        updated = issues.update_issue(
            conn,
            issue_id,
            title=normalized_title,
            body=body_value,
            status=status_value,
            priority=priority_value,
            commit=False,
        )
        # The row was read after the write lock was acquired, so disappearing
        # here would indicate corruption or an unexpected trigger, not a normal
        # request race.
        if updated is None:
            raise IssueCommandError("not_found", "no such issue")
        if "assignee_id" in provided:
            updated = issues.set_assignee(
                conn,
                issue_id,
                assignee_value,
                commit=False,
            )
            if updated is None:
                raise IssueCommandError("not_found", "no such issue")
        if "sprint_id" in provided:
            updated = issues.set_sprint(
                conn,
                issue_id,
                sprint_value,
                commit=False,
            )
            if updated is None:
                raise IssueCommandError("not_found", "no such issue")

        transition_project_ids = (
            {before["project_id"], updated["project_id"]}
            if project_changed
            else None
        )
        if "status" in provided or project_changed:
            issue_activity.record_status_change(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                before=before["status"],
                after=updated["status"],
                commit=False,
                issue_project_ids=transition_project_ids,
            )
        if "priority" in provided:
            issue_activity.record_priority_change(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                before=before["priority"],
                after=updated["priority"],
                commit=False,
            )
        if "assignee_id" in provided:
            issue_activity.record_assignee_change(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                before=before["assignee_id"],
                after=updated["assignee_id"],
                commit=False,
            )
        if "project_id" in provided:
            issue_activity.record_project_change(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                before=before["project_id"],
                after=updated["project_id"],
                commit=False,
            )
        if "sprint_id" in provided or project_changed:
            project_caused_sprint_clear = (
                project_changed
                and before["sprint_id"] is not None
                and updated["sprint_id"] is None
            )
            issue_activity.record_sprint_change(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                before=before["sprint_id"],
                after=updated["sprint_id"],
                include_before_detail=not project_caused_sprint_clear,
                commit=False,
                issue_project_ids=transition_project_ids,
            )
        issue_activity.record_edited(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            before=before,
            after=updated,
            commit=False,
        )
    return updated
