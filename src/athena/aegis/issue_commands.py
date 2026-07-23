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

from athena.aegis import (
    contributors as contributors_data,
    dependencies,
    issue_activity,
    issue_etags,
    issues,
    sprints,
    statuses,
)
from athena.core import access, db, etag, identity, labels, tokens, users

ErrorKind = Literal[
    "unauthorized",
    "forbidden",
    "not_found",
    "invalid",
    "conflict",
    "precondition_required",
    "invalid_precondition",
    "precondition_too_large",
    "precondition_failed",
    "lease_generation_required",
    "invalid_lease_generation",
    "lease_generation_mismatch",
]


class IssueCommandError(Exception):
    """A transport-neutral command rejection.

    Adapters translate ``kind`` into their own status vocabulary (REST uses 422
    for invalid input while an HTML form uses 400) without reimplementing the
    business rule that produced ``detail``.
    """

    def __init__(
        self,
        kind: ErrorKind,
        detail: str,
        *,
        current_etag: str | None = None,
    ):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.current_etag = current_etag


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


def _visible_issue(conn: sqlite3.Connection, actor: dict, issue_id: int) -> dict:
    issue = issues.get_issue(conn, issue_id)
    if issue is None or not access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    ):
        raise IssueCommandError("not_found", "no such issue")
    return issue


def _modifiable_issue(conn: sqlite3.Connection, issue: dict, actor: dict) -> dict:
    if not issues.can_act_on(conn, issue, actor):
        raise IssueCommandError(
            "forbidden",
            "only the issue creator, assignee, a delegated contributor, "
            "or an admin may modify it",
        )
    return issue


def _writable_issue(conn: sqlite3.Connection, actor: dict, issue_id: int) -> dict:
    return _modifiable_issue(conn, _visible_issue(conn, actor, issue_id), actor)


def _check_issue_precondition(
    conn: sqlite3.Connection,
    issue: dict,
    if_match: list[str] | None,
    *,
    required_detail: str | None = None,
    exact: bool = False,
) -> None:
    """Evaluate one issue precondition against the current public representation.

    The caller must hold the write transaction that owns the mutation. Optional
    issue updates retain normal HTTP If-Match list/wildcard semantics; commands
    using exact mode require one actual strong tag so callers cannot bypass a
    reviewed-revision gate with a wildcard or a list.
    """
    if if_match is None:
        if required_detail is not None:
            raise IssueCommandError(
                "precondition_required",
                required_detail,
            )
        return

    current_etag = issue_etags.current_etag(conn, issue)
    try:
        condition = etag.parse_if_match(if_match)
        matches = (
            condition.single_strong_tag() == current_etag
            if exact
            else condition.matches(current_etag)
        )
    except etag.IfMatchTooLarge as exc:
        raise IssueCommandError(
            "precondition_too_large",
            str(exc),
        ) from exc
    except etag.InvalidIfMatch as exc:
        raise IssueCommandError(
            "invalid_precondition",
            str(exc),
        ) from exc
    if not matches:
        raise IssueCommandError(
            "precondition_failed",
            "If-Match precondition failed",
            current_etag=current_etag,
        )


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
    return _modifiable_issue(conn, issue, actor)


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
            raise IssueCommandError("invalid", "no such status for this project")
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
    if_match: list[str] | None = None,
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
        if_match=if_match,
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
        if_match=None,
    )


def _string_value(name: str, value: str | None | _UnsetType) -> str | None:
    if value is UNSET or value is None:
        return None
    if not isinstance(value, str):
        raise IssueCommandError("invalid", f"{name} must be a string")
    return value


def _nullable_int_value(name: str, value: int | None | _UnsetType) -> int | None:
    if value is UNSET:
        return None
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
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
    if_match: list[str] | None,
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
            raise IssueCommandError("invalid", "no such status for this project")
        # Authorization and ordinary payload validation intentionally happen
        # before the precondition result is disclosed. The current representation
        # and comparison are both inside this BEGIN IMMEDIATE transaction, so two
        # writers holding the same tag cannot both pass and mutate.
        _check_issue_precondition(conn, before, if_match)

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
            {before["project_id"], updated["project_id"]} if project_changed else None
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
                before_project_id=before["project_id"],
                after_project_id=updated["project_id"],
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


def set_issue_archived(
    conn: sqlite3.Connection, *, actor: dict | None, issue_id: int, archived: bool
) -> dict:
    """Archive (soft-delete) or restore an issue and record the audit fact
    atomically. Same gate as any issue write (visible + creator/assignee/
    delegated/admin). Idempotent: re-archiving an archived issue re-stamps the
    time but records no new event. Raises IssueCommandError(404/403)."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        before = _writable_issue(conn, actor, issue_id)
        updated = issues.set_archived(conn, issue_id, archived, commit=False)
        assert updated is not None
        issue_activity.record_archive_change(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            before=before["archived_at"],
            after=updated["archived_at"],
            commit=False,
        )
        return updated


def set_issue_parent(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    parent_id: int | None,
) -> dict:
    """Nest an issue under a parent (parent_id=None clears it) and record the
    'set_parent'/'removed_parent' event atomically. The parent must be one the
    actor can SEE — a hidden parent collapses to the same 'no such parent issue'
    a missing one gives, so a write can't nest under (or probe) a private issue.
    Same write gate as status/assign; validation (self, cycle, existence) runs in
    the command. Raises IssueCommandError(404/403/422). No event when unchanged."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        before = _writable_issue(conn, actor, issue_id)
        if parent_id is not None and not access.can_see_issue(conn, actor, parent_id):
            raise IssueCommandError("invalid", "no such parent issue")
        reason = issues.validate_parent(conn, issue_id, parent_id)
        if reason is not None:
            raise IssueCommandError("invalid", reason)
        updated = issues.set_parent(conn, issue_id, parent_id, commit=False)
        assert updated is not None
        issue_activity.record_parent_change(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            before=before["parent_id"],
            after=parent_id,
            commit=False,
        )
        return updated


def attach_label(
    conn: sqlite3.Connection, *, actor: dict | None, issue_id: int, label_id: int
) -> dict:
    """Attach a label to an issue and record the 'labeled' event atomically. Same
    write gate as status/assign. Idempotent: re-attaching records nothing. Raises
    IssueCommandError(404/403) for the issue gate, (422) for an unknown label.
    Returns the (unchanged) issue row so the caller can reshape it."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        issue = _writable_issue(conn, actor, issue_id)
        if labels.get_label(conn, label_id) is None:
            raise IssueCommandError("invalid", "no such label")
        if labels.add_label_to_issue(conn, issue_id, label_id, commit=False):
            issue_activity.record_label_added(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                label_id=label_id,
                commit=False,
            )
        return issue


def detach_label(
    conn: sqlite3.Connection, *, actor: dict | None, issue_id: int, label_id: int
) -> dict:
    """Detach a label and record the 'unlabeled' event atomically. Same write gate.
    Raises IssueCommandError(404/403) for the issue gate, (404) when the label isn't
    on this issue."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        issue = _writable_issue(conn, actor, issue_id)
        if not labels.remove_label_from_issue(conn, issue_id, label_id, commit=False):
            raise IssueCommandError("not_found", "label not on this issue")
        issue_activity.record_label_removed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            label_id=label_id,
            commit=False,
        )
        return issue


def add_contributor(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    user_id: int,
    require_agent: bool = False,
) -> list[dict]:
    """Add a contributor (or, with require_agent, delegate to an agent) and record
    the 'added_contributor'/'delegated' event atomically — the new contributor's
    auto-watch, the audit event, and the membership row land or roll back together.
    Same write gate. Idempotent: re-adding an existing contributor records nothing.
    Raises IssueCommandError(404/403) for the issue gate, (422) for an unknown user
    or (with require_agent) a non-agent target. Returns the contributor list."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        _writable_issue(conn, actor, issue_id)
        target = users.get_user(conn, user_id)
        if target is None:
            raise IssueCommandError("invalid", "no such user")
        if require_agent and not target["is_agent"]:
            raise IssueCommandError("invalid", "delegation target must be an agent")
        if contributors_data.add_contributor(
            conn, issue_id, user_id, actor["id"], commit=False
        ):
            recorder = (
                issue_activity.record_delegated
                if require_agent
                else issue_activity.record_contributor_added
            )
            recorder(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                user_id=user_id,
                commit=False,
            )
        return contributors_data.list_contributors(conn, issue_id)


def remove_contributor(
    conn: sqlite3.Connection, *, actor: dict | None, issue_id: int, user_id: int
) -> list[dict]:
    """Remove a contributor and record the 'removed_contributor' event atomically.
    Same write gate. Raises IssueCommandError(404/403) for the issue gate, (404)
    when the user isn't a contributor. Returns the remaining contributor list."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        _writable_issue(conn, actor, issue_id)
        if not contributors_data.remove_contributor(
            conn, issue_id, user_id, commit=False
        ):
            raise IssueCommandError("not_found", "not a contributor on this issue")
        issue_activity.record_contributor_removed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            user_id=user_id,
            commit=False,
        )
        return contributors_data.list_contributors(conn, issue_id)


def link_issues(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    target_ref: str,
    relation: str,
) -> dict:
    """Create a typed dependency (blocks / blocked_by / relates) FROM issue_id to the
    issue named by ``target_ref``, recording the edge AND its audit event in one
    transaction. Before this, dependency writes emitted no activity at all, so an edge
    an agent created over MCP left no attributable trace.

    Same authorization as any issue write: the actor must be a writer (role + scope)
    who may act on issue_id (creator, assignee, delegated contributor, or admin), and
    the TARGET must be an issue the actor can see — a hidden or missing target collapses to the same "no such target issue",
    so a write can't probe a private issue's existence. Idempotent: re-adding an
    identical edge records no second event. Returns issue_id's relationship summary.
    """
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        _writable_issue(conn, actor, issue_id)
        target = issues.get_by_ref(conn, target_ref)
        if target is None or not access.can_see_issue(conn, actor, target["id"]):
            raise IssueCommandError("invalid", "no such target issue")
        reason, inserted = dependencies.add_link(
            conn,
            from_id=issue_id,
            to_id=target["id"],
            relation=relation,
            created_by=actor["id"],
            commit=False,
        )
        if reason is not None:
            # The direct contradiction (A blocks B while B blocks A) conflicts with
            # existing state; everything else is bad input. Adapters map "conflict"
            # to 409 on REST and 400 on the HTML form, preserving prior behavior.
            kind: ErrorKind = "conflict" if "block each other" in reason else "invalid"
            raise IssueCommandError(kind, reason)
        if inserted:
            issue_activity.record_link_added(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                other_id=target["id"],
                relation=relation,
                commit=False,
            )
        return dependencies.list_links(conn, issue_id, actor=actor)


def unlink_issues(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    target_id: int,
    relation: str,
) -> dict:
    """Remove the typed dependency FROM issue_id toward target_id (addressed by id,
    the form the delete routes use), recording the removal atomically. Same write gate
    as link_issues. Raises not_found with detail "no such relationship" when no such
    edge exists — REST turns that into a 404; the HTML form treats it as an idempotent
    redirect (its buttons only appear for edges that exist). Returns the summary."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        _writable_issue(conn, actor, issue_id)
        removed = dependencies.remove_link(
            conn,
            from_id=issue_id,
            to_id=target_id,
            relation=relation,
            commit=False,
        )
        if not removed:
            raise IssueCommandError("not_found", "no such relationship")
        issue_activity.record_link_removed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            other_id=target_id,
            relation=relation,
            commit=False,
        )
        return dependencies.list_links(conn, issue_id, actor=actor)
