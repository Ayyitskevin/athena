"""Recording issue lifecycle events onto the activity trail.

One owner for the "what counts as an event, and how is it phrased" rules. Shared
application commands call these helpers so REST, web, bulk, and automation writes
record the SAME facts the same way. Legacy mutation paths are migrating behind
those commands incrementally.

These helpers only ever *append* (through core.activity.record); they never
change the issue itself. The caller does the write, then hands us the before/after
so we record the diff. "Record only on real change" lives here: a no-op edit (same
status, same assignee) writes nothing, on either surface.
"""
from __future__ import annotations

from collections.abc import Collection
import sqlite3

from athena.aegis import issues, projects, sprints
from athena.core import activity, labels, notifications, users


def _scope_kwargs(
    project_ids: Collection[int | None] | None,
) -> dict[str, object]:
    return {} if project_ids is None else {"issue_project_ids": project_ids}


def _project_ids_for_issues(
    conn: sqlite3.Connection, *issue_ids: int | None
) -> set[int | None]:
    project_ids: set[int | None] = set()
    for issue_id in issue_ids:
        if issue_id is None:
            continue
        issue = issues.get_issue(conn, issue_id)
        if issue is not None:
            project_ids.add(issue["project_id"])
    return project_ids


def record_created(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    body: str = "",
    commit: bool = True,
) -> None:
    """An issue was created. The first audit fact in its history. The creator starts
    watching it, so they hear about later activity without opting in; anyone named
    by [[user:N]] in the body is mentioned (notified + auto-watched)."""
    try:
        event = activity.record(
            conn,
            actor_id=actor_id,
            verb="created",
            target_kind="issue",
            target_id=issue_id,
            commit=False,
        )
        notifications.watch(
            conn, actor_id, "issue", issue_id, commit=False
        )
        notifications.process_mentions(
            conn,
            event_id=event["id"],
            actor_id=actor_id,
            text=body,
            commit=False,
        )
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()


def record_edited(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: dict,
    after: dict,
    commit: bool = True,
) -> None:
    """An issue's title or body was edited. No-op if neither actually changed — a
    resubmit of identical content (both surfaces send every field) isn't an audit
    fact. Detail carries the new title so the feed can name what was edited, since
    the global feed otherwise links an issue only by number. Status and priority
    are their own concerns (changed_status and changed_priority), so this
    deliberately ignores them."""
    if before["title"] == after["title"] and before["body"] == after["body"]:
        return
    try:
        event = activity.record(
            conn,
            actor_id=actor_id,
            verb="issue_edited",
            target_kind="issue",
            target_id=issue_id,
            detail=after["title"],
            commit=False,
        )
        # A newly-added [[user:N]] in the edited body mentions that person.
        notifications.process_mentions(
            conn,
            event_id=event["id"],
            actor_id=actor_id,
            text=after["body"],
            commit=False,
        )
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()


def record_status_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: str,
    after: str,
    commit: bool = True,
    issue_project_ids: Collection[int | None] | None = None,
) -> None:
    """Record a status transition as "before → after". No-op if unchanged — the
    lifecycle moment only matters when the status actually moved."""
    if before == after:
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="changed_status",
        target_kind="issue",
        target_id=issue_id,
        detail=f"{before} → {after}",
        commit=commit,
        **_scope_kwargs(issue_project_ids),
    )


def record_priority_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: str,
    after: str,
    commit: bool = True,
) -> None:
    """Record a priority transition as "before -> after". No-op if unchanged."""
    if before == after:
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="changed_priority",
        target_kind="issue",
        target_id=issue_id,
        detail=f"{before} → {after}",
        commit=commit,
    )


def record_assignee_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: int | None,
    after: int | None,
    commit: bool = True,
) -> None:
    """Record an assignment change. No-op if the assignee didn't change. Clearing
    records "unassigned" (no detail); setting records "assigned" with the new
    assignee's display name as the human specifics.

    The new assignee's watch, the activity row, and its notification fan-out use
    one commit. Commands pass ``commit=False`` so those effects also share the
    issue row's outer transaction.
    """
    if before == after:
        return
    try:
        if after is None:
            activity.record(
                conn,
                actor_id=actor_id,
                verb="unassigned",
                target_kind="issue",
                target_id=issue_id,
                commit=False,
            )
        else:
            assignee = users.get_user(conn, after)
            # The new assignee starts watching BEFORE we record the event, so the
            # assignment itself lands in their inbox (they're a watcher when it
            # fans out).
            notifications.watch(
                conn, after, "issue", issue_id, commit=False
            )
            activity.record(
                conn,
                actor_id=actor_id,
                verb="assigned",
                target_kind="issue",
                target_id=issue_id,
                detail=assignee["name"] if assignee else "",
                commit=False,
            )
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


def record_project_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: int | None,
    after: int | None,
    commit: bool = True,
) -> None:
    """Record a project move. No-op if the project didn't change. Clearing records
    "removed_from_project" with the old project's name; setting records
    "changed_project" with the new project's name as the human specifics.

    Both containers scope the transition: after A → B → C, a reader with access
    only to A and C must not learn B's name from the historical A → B event.
    """
    if before == after:
        return
    project_ids = {before, after}
    try:
        if after is None:
            old = projects.get_project(conn, before)
            activity.record(
                conn,
                actor_id=actor_id,
                verb="removed_from_project",
                target_kind="issue",
                target_id=issue_id,
                detail=old["name"] if old else "",
                commit=False,
                issue_project_ids=project_ids,
            )
        else:
            new = projects.get_project(conn, after)
            activity.record(
                conn,
                actor_id=actor_id,
                verb="changed_project",
                target_kind="issue",
                target_id=issue_id,
                detail=new["name"] if new else "",
                commit=False,
                issue_project_ids=project_ids,
            )
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


def record_sprint_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: int | None,
    after: int | None,
    issue_project_ids: Collection[int | None] | None = None,
    include_before_detail: bool = True,
    commit: bool = True,
) -> None:
    """Record a sprint move with every referenced project in its access envelope.

    A project move passes ``include_before_detail=False`` for its automatic clear:
    history folding keeps the transition without copying a former private sprint name.
    The event still carries the old sprint's project scope, so redaction never weakens
    its access envelope.
    """
    if before == after:
        return
    old = sprints.get_sprint(conn, before) if before is not None else None
    new = sprints.get_sprint(conn, after) if after is not None else None
    project_ids = _project_ids_for_issues(conn, issue_id)
    if issue_project_ids is not None:
        project_ids.update(issue_project_ids)
    for sprint in (old, new):
        if sprint is not None:
            project_ids.add(sprint["project_id"])

    try:
        if after is None:
            activity.record(
                conn,
                actor_id=actor_id,
                verb="removed_from_sprint",
                target_kind="issue",
                target_id=issue_id,
                detail=old["name"] if old and include_before_detail else "",
                commit=False,
                issue_project_ids=project_ids,
            )
        else:
            activity.record(
                conn,
                actor_id=actor_id,
                verb="moved_to_sprint",
                target_kind="issue",
                target_id=issue_id,
                detail=new["name"] if new else "",
                commit=False,
                issue_project_ids=project_ids,
            )
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


def record_parent_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: int | None,
    after: int | None,
) -> None:
    """Record a parent (hierarchy) change. No-op if the parent didn't change.
    Clearing records "removed_parent"; setting records "set_parent" with the new
    parent's key (or #id) as the human specifics.

    Parent relations may cross projects. Scope the event to the child plus both
    relationship endpoints so a public child cannot publish a private parent's key.
    """
    if before == after:
        return
    project_ids = _project_ids_for_issues(conn, issue_id, before, after)
    if after is None:
        activity.record(
            conn,
            actor_id=actor_id,
            verb="removed_parent",
            target_kind="issue",
            target_id=issue_id,
            issue_project_ids=project_ids,
        )
        return
    parent = issues.get_issue(conn, after)
    detail = (parent.get("key") or f"#{after}") if parent else f"#{after}"
    activity.record(
        conn,
        actor_id=actor_id,
        verb="set_parent",
        target_kind="issue",
        target_id=issue_id,
        detail=detail,
        issue_project_ids=project_ids,
    )


def record_contributor_added(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, user_id: int
) -> None:
    """Record that someone was delegated/added as a contributor, stamped with their
    display name. The new contributor starts watching the issue BEFORE we record the
    event, so the delegation itself lands in their inbox (they're a watcher when it
    fans out) — exactly how a new assignee is brought in. Caller records only when
    the add actually created a new pairing."""
    notifications.watch(conn, user_id, "issue", issue_id)
    contributor = users.get_user(conn, user_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="added_contributor",
        target_kind="issue",
        target_id=issue_id,
        detail=contributor["name"] if contributor else "",
    )


def record_delegated(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, user_id: int
) -> None:
    """Record explicit agent delegation. Like contributor-add, the delegated agent
    starts watching before the event is recorded, so the delegation itself appears
    in their inbox. Caller records only when a new contributor pairing was created."""
    notifications.watch(conn, user_id, "issue", issue_id)
    agent = users.get_user(conn, user_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="delegated",
        target_kind="issue",
        target_id=issue_id,
        detail=agent["name"] if agent else "",
    )


def record_contributor_removed(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, user_id: int
) -> None:
    """Record that a contributor was removed, stamped with their display name. Caller
    records only when a pairing was actually removed."""
    contributor = users.get_user(conn, user_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="removed_contributor",
        target_kind="issue",
        target_id=issue_id,
        detail=contributor["name"] if contributor else "",
    )


def record_label_added(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, label_id: int
) -> None:
    """Record that a label was attached, stamped with the label's name. Caller
    records only when the attach actually created a new pairing."""
    label = labels.get_label(conn, label_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="labeled",
        target_kind="issue",
        target_id=issue_id,
        detail=label["name"] if label else "",
    )


def record_label_removed(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, label_id: int
) -> None:
    """Record that a label was detached, stamped with the label's name. Caller
    records only when a pairing was actually removed."""
    label = labels.get_label(conn, label_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="unlabeled",
        target_kind="issue",
        target_id=issue_id,
        detail=label["name"] if label else "",
    )


def record_commented(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, body: str = ""
) -> None:
    """Record that someone commented on the issue. The event targets the issue (so
    it lands on the issue's History and the global feed links there); the comment
    body itself lives on the issue, not duplicated into the trail's detail. Anyone
    named by [[user:N]] in the comment is mentioned (notified + auto-watched)."""
    event = activity.record(
        conn,
        actor_id=actor_id,
        verb="commented",
        target_kind="issue",
        target_id=issue_id,
    )
    # Commenting is participation — the commenter starts watching the issue.
    notifications.watch(conn, actor_id, "issue", issue_id)
    notifications.process_mentions(
        conn, event_id=event["id"], actor_id=actor_id, text=body
    )


def record_comment_deleted(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int
) -> None:
    """Record that a comment was removed from the issue — the audit-worthy half of
    the comment lifecycle (who took content down). No detail: the comment is gone."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="comment_deleted",
        target_kind="issue",
        target_id=issue_id,
    )


def record_archive_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: str | None,
    after: str | None,
) -> None:
    """Record an archive (soft-delete) or restore. before/after are the issue's
    archived_at values — None means active, a timestamp means archived — so we
    compare PRESENCE, not the exact time: re-archiving an already-archived issue
    re-stamps the time but isn't a lifecycle change, and records nothing."""
    if (before is not None) == (after is not None):
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="archived" if after is not None else "unarchived",
        target_kind="issue",
        target_id=issue_id,
    )
