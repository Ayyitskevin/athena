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
from typing import TypedDict

from athena.aegis import issues, projects, sprints
from athena.core import activity, db, labels, notifications, users

_INFER_PROJECT = object()


def _project_scope_key(conn: sqlite3.Connection, project_id: int | None) -> str | None:
    if project_id is None:
        return None
    row = conn.execute(
        "SELECT activity_scope_key FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None or row["activity_scope_key"] is None:
        raise ValueError("project activity scope is unavailable")
    return str(row["activity_scope_key"])


def _status_category(
    conn: sqlite3.Connection, project_id: int | None, status: str
) -> str:
    from athena.aegis import statuses

    category = statuses.category_of(conn, project_id, status)
    if category is None:
        raise ValueError("issue status category is unavailable")
    return category


def _actor_kind(conn: sqlite3.Connection, actor_id: int) -> str:
    actor = users.get_user(conn, actor_id)
    if actor is None:
        raise ValueError("issue lifecycle actor is unavailable")
    return "agent" if actor["is_agent"] else "human"


def _record_lifecycle_fact(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    issue_id: int,
    previous_event_id: int | None,
    event_kind: str,
    before_status: str | None,
    before_category: str | None,
    after_status: str,
    after_category: str,
    before_project_scope_key: str | None,
    after_project_scope_key: str | None,
    actor_kind: str,
) -> None:
    conn.execute(
        "INSERT INTO issue_lifecycle_facts "
        "(event_id, issue_id, previous_event_id, event_kind, before_status, "
        "before_category, after_status, after_category, "
        "before_project_scope_key, after_project_scope_key, actor_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            issue_id,
            previous_event_id,
            event_kind,
            before_status,
            before_category,
            after_status,
            after_category,
            before_project_scope_key,
            after_project_scope_key,
            actor_kind,
        ),
    )


_LIFECYCLE_FACT_COLUMNS = (
    "event_id, issue_id, previous_event_id, event_kind, before_status, "
    "before_category, after_status, after_category, "
    "before_project_scope_key, after_project_scope_key, actor_kind"
)


def lifecycle_fact_for_event(conn: sqlite3.Connection, event_id: int) -> dict | None:
    """The structured lifecycle fact recorded alongside one activity event, or None.

    This module owns the only INSERT into ``issue_lifecycle_facts`` (migration
    0055), so the reader belongs beside it. The table is append-only and 1:1 with
    a native, unrestricted activity event, which makes it the one trustworthy
    source of an issue's *prior* status — ``activity.detail`` carries the same
    transition as prose ("open → done") and must never be parsed to drive a write.

    ``None`` is a real answer, not a default to paper over: 0055 is deliberately
    not backfilled, so an event recorded before that migration has no fact. A
    caller that needs prior state must refuse on None rather than assume one.
    """
    row = conn.execute(
        f"SELECT {_LIFECYCLE_FACT_COLUMNS} FROM issue_lifecycle_facts "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def project_scope_key(conn: sqlite3.Connection, project_id: int | None) -> str | None:
    """The access-envelope key for a project, or None for the backlog.

    Public so a compensator can compare an issue's CURRENT envelope against the one
    a lifecycle fact recorded, without re-implementing the lookup that decides what
    a recorded scope key means.
    """
    return _project_scope_key(conn, project_id)


def assignee_fact_for_event(conn: sqlite3.Connection, event_id: int) -> dict | None:
    """The structured assignee fact recorded alongside one activity event, or None.

    The assignee twin of :func:`lifecycle_fact_for_event`, over 0068's
    ``issue_assignee_facts``: this module owns the only INSERT, so the reader
    lives beside it. ``None`` means the event predates 0068 (no backfill) — a
    caller needing the prior assignee must refuse on None, never reconstruct one
    from the event's display-name detail.
    """
    row = conn.execute(
        "SELECT event_id, issue_id, before_assignee_id, after_assignee_id "
        "FROM issue_assignee_facts WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return dict(row) if row is not None else None


class _ScopeKwargs(TypedDict, total=False):
    """The one optional kwarg this module forwards to ``activity.record``.

    A ``TypedDict`` rather than ``dict[str, object]``, because a broad mapping
    splatted into a keyword-argument list type-checks against ANY parameter it
    could land on. When ``record`` grew an ``imported_at`` argument, the old
    annotation started matching that instead — the checker cannot tell which key
    a plain dict carries. Naming the key makes the splat verifiable.
    """

    issue_project_ids: Collection[int | None]


def _scope_kwargs(project_ids: Collection[int | None] | None) -> _ScopeKwargs:
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
        issue = issues.get_issue(conn, issue_id)
        if issue is None:
            raise ValueError("issue lifecycle target is unavailable")
        event = activity.record(
            conn,
            actor_id=actor_id,
            verb="created",
            target_kind="issue",
            target_id=issue_id,
            commit=False,
        )
        _record_lifecycle_fact(
            conn,
            event_id=event["id"],
            issue_id=issue_id,
            previous_event_id=None,
            event_kind="created",
            before_status=None,
            before_category=None,
            after_status=issue["status"],
            after_category=_status_category(conn, issue["project_id"], issue["status"]),
            before_project_scope_key=None,
            after_project_scope_key=_project_scope_key(conn, issue["project_id"]),
            actor_kind=_actor_kind(conn, actor_id),
        )
        notifications.watch(conn, actor_id, "issue", issue_id, commit=False)
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
    before_project_id: int | None | object = _INFER_PROJECT,
    after_project_id: int | None | object = _INFER_PROJECT,
) -> None:
    """Record a status/category transition as ``before → after``.

    A project move can change the meaning of the same status name, so category
    changes are lifecycle moments even when the text is unchanged.
    """
    if commit:
        with db.transaction(conn, immediate=True):
            record_status_change(
                conn,
                actor_id=actor_id,
                issue_id=issue_id,
                before=before,
                after=after,
                commit=False,
                issue_project_ids=issue_project_ids,
                before_project_id=before_project_id,
                after_project_id=after_project_id,
            )
        return
    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        raise ValueError("issue lifecycle target is unavailable")
    if before_project_id is _INFER_PROJECT:
        resolved_before_project_id: int | None = issue["project_id"]
    else:
        assert before_project_id is None or isinstance(before_project_id, int)
        resolved_before_project_id = before_project_id
    if after_project_id is _INFER_PROJECT:
        resolved_after_project_id: int | None = issue["project_id"]
    else:
        assert after_project_id is None or isinstance(after_project_id, int)
        resolved_after_project_id = after_project_id
    before_category = _status_category(conn, resolved_before_project_id, before)
    after_category = _status_category(conn, resolved_after_project_id, after)
    if before == after and before_category == after_category:
        return
    before_scope_key = _project_scope_key(conn, resolved_before_project_id)
    after_scope_key = _project_scope_key(conn, resolved_after_project_id)
    try:
        previous = conn.execute(
            "SELECT event_id FROM issue_lifecycle_facts "
            "WHERE issue_id = ? ORDER BY event_id DESC LIMIT 1",
            (issue_id,),
        ).fetchone()
        previous_event_id = previous["event_id"] if previous else None
        event = activity.record(
            conn,
            actor_id=actor_id,
            verb="changed_status",
            target_kind="issue",
            target_id=issue_id,
            detail=f"{before} → {after}",
            commit=False,
            **_scope_kwargs(issue_project_ids),
        )
        _record_lifecycle_fact(
            conn,
            event_id=event["id"],
            issue_id=issue_id,
            previous_event_id=previous_event_id,
            event_kind="status_transition",
            before_status=before,
            before_category=before_category,
            after_status=after,
            after_category=after_category,
            before_project_scope_key=before_scope_key,
            after_project_scope_key=after_scope_key,
            actor_kind=_actor_kind(conn, actor_id),
        )
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


def record_blocked_close_override(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    issue_project_ids: Collection[int | None] | None = None,
    commit: bool = True,
) -> None:
    """Record an explicit human override of a project's blocked-close policy.

    The detail is intentionally empty: the policy denial and this event must not
    copy the identities of blockers an otherwise eligible issue writer cannot
    see. Project moves pass both containers so the access envelope cannot weaken
    while the override, status transition, and issue row share one transaction.
    """
    activity.record(
        conn,
        actor_id=actor_id,
        verb="overrode_blocked_issue_close",
        target_kind="issue",
        target_id=issue_id,
        issue_project_ids=(
            _project_ids_for_issues(conn, issue_id)
            if issue_project_ids is None
            else issue_project_ids
        ),
        commit=commit,
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

    Beside the event, the typed ``before``/``after`` ids land in
    ``issue_assignee_facts`` (0068) in the same transaction — the display name in
    the detail is not restorable evidence (names are not unique, and a re-assign
    reads identically to a first assign), and the fact row is what makes the
    assignment reversible. The 0068 trigger re-raises if the fact cannot bind to
    this exact event, which rolls the whole assignment back: an assignment that
    cannot be recorded honestly does not happen, matching the status writer.

    The new assignee's watch, the activity row, and its notification fan-out use
    one commit. Commands pass ``commit=False`` so those effects also share the
    issue row's outer transaction.
    """
    if before == after:
        return
    try:
        if after is None:
            event = activity.record(
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
            notifications.watch(conn, after, "issue", issue_id, commit=False)
            event = activity.record(
                conn,
                actor_id=actor_id,
                verb="assigned",
                target_kind="issue",
                target_id=issue_id,
                detail=assignee["name"] if assignee else "",
                commit=False,
            )
        conn.execute(
            "INSERT INTO issue_assignee_facts "
            "(event_id, issue_id, before_assignee_id, after_assignee_id) "
            "VALUES (?, ?, ?, ?)",
            (event["id"], issue_id, before, after),
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
            assert before is not None
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
    commit: bool = True,
) -> None:
    """Record a parent (hierarchy) change. No-op if the parent didn't change.
    Clearing records "removed_parent"; setting records "set_parent" with the new
    parent's key (or #id) as the human specifics.

    Parent relations may cross projects. Scope the event to the child plus both
    relationship endpoints so a public child cannot publish a private parent's key.
    ``commit=False`` lets the audited command fold the write and this event into
    one transaction."""
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
            commit=commit,
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
        commit=commit,
    )


def _link_detail(conn: sqlite3.Connection, relation: str, other_id: int) -> str:
    other = issues.get_issue(conn, other_id)
    other_key = (other.get("key") or f"#{other_id}") if other else f"#{other_id}"
    return f"{relation} {other_key}"


def record_link_added(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    other_id: int,
    relation: str,
    commit: bool = True,
) -> None:
    """Record that a typed dependency (blocks / blocked_by / relates) was declared
    FROM issue_id toward other_id, phrased as "<relation> <other key>".

    Dependencies may cross projects, so scope the event to both endpoints so a
    public issue cannot publish a private linked issue's key. Commands pass
    ``commit=False`` so the edge row and this event share one transaction — before
    this, dependency writes emitted no audit at all, breaking attribution for an
    edge any agent can create over MCP."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="linked",
        target_kind="issue",
        target_id=issue_id,
        detail=_link_detail(conn, relation, other_id),
        commit=commit,
        issue_project_ids=_project_ids_for_issues(conn, issue_id, other_id),
    )


def record_link_removed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    other_id: int,
    relation: str,
    commit: bool = True,
) -> None:
    """Record that a typed dependency FROM issue_id toward other_id was removed —
    the mirror of record_link_added (verb "unlinked"), same cross-endpoint scoping."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="unlinked",
        target_kind="issue",
        target_id=issue_id,
        detail=_link_detail(conn, relation, other_id),
        commit=commit,
        issue_project_ids=_project_ids_for_issues(conn, issue_id, other_id),
    )


def record_contributor_added(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    user_id: int,
    commit: bool = True,
) -> None:
    """Record that someone was delegated/added as a contributor, stamped with their
    display name. The new contributor starts watching the issue BEFORE we record the
    event, so the delegation itself lands in their inbox (they're a watcher when it
    fans out) — exactly how a new assignee is brought in. Caller records only when
    the add actually created a new pairing. ``commit=False`` folds the watch and the
    event into the audited command's transaction."""
    notifications.watch(conn, user_id, "issue", issue_id, commit=False)
    contributor = users.get_user(conn, user_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="added_contributor",
        target_kind="issue",
        target_id=issue_id,
        detail=contributor["name"] if contributor else "",
        commit=commit,
    )


def record_delegated(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    user_id: int,
    commit: bool = True,
) -> None:
    """Record explicit agent delegation. Like contributor-add, the delegated agent
    starts watching before the event is recorded, so the delegation itself appears
    in their inbox. Caller records only when a new contributor pairing was created.
    ``commit=False`` folds the watch and the event into one transaction."""
    notifications.watch(conn, user_id, "issue", issue_id, commit=False)
    agent = users.get_user(conn, user_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="delegated",
        target_kind="issue",
        target_id=issue_id,
        detail=agent["name"] if agent else "",
        commit=commit,
    )


def record_issue_claimed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    expires_at: str,
    generation: str,
    renewed: bool,
    commit: bool = True,
) -> None:
    """Record that an actor took (or renewed) the exclusive lease on an issue — the
    coordination fact that stops a second agent from silently pulling the same work. The
    detail carries the lease generation and expiry so the trail identifies the exact
    possession and shows how long the claim was good for.
    A renewal (the holder re-claiming) records 'lease_renewed' rather than 'claimed', so
    the run history distinguishes taking the work from extending the window. ``commit=False``
    folds it into the claim command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="lease_renewed" if renewed else "claimed",
        target_kind="issue",
        target_id=issue_id,
        detail=f"generation {generation}; until {expires_at}",
        commit=commit,
    )


def record_claim_completed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    generation: str,
    commit: bool = True,
) -> None:
    """Record that the leaseholder released the issue by completing its claimed work —
    the lease is gone and the issue is free for the next claimant. ``commit=False`` folds
    it into the complete command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="claim_completed",
        target_kind="issue",
        target_id=issue_id,
        detail=f"generation {generation}",
        commit=commit,
    )


def record_claim_yielded(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    generation: str,
    handoff_token: str,
    reason: str,
    blocking_question: str,
    commit: bool = True,
) -> dict:
    """Record an honest release that does not assert completion or reassignment."""
    question_excerpt = blocking_question[:200]
    detail = (
        f"generation {generation}; handoff {handoff_token}; "
        f"{reason}: {question_excerpt}"
    )
    try:
        event = activity.record(
            conn,
            actor_id=actor_id,
            verb="claim_yielded",
            target_kind="issue",
            target_id=issue_id,
            detail=detail,
            commit=False,
        )
        notifications.process_mentions(
            conn,
            event_id=event["id"],
            actor_id=actor_id,
            text=blocking_question,
            commit=False,
        )
    except BaseException:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    return event


def record_claim_handoff_resumed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    handoff_token: str,
    generation: str,
    commit: bool = True,
) -> dict:
    """Record explicit receipt of a handoff without claiming its blocker is solved."""
    return activity.record(
        conn,
        actor_id=actor_id,
        verb="claim_handoff_resumed",
        target_kind="issue",
        target_id=issue_id,
        detail=f"handoff {handoff_token}; generation {generation}",
        commit=commit,
    )


def record_delegation_declined(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    released_generation: str | None = None,
    commit: bool = True,
) -> None:
    """Record that an actor declined a delegation handed to them (removing itself from the
    contributor set) — so the operator sees the work was refused, not silently dropped, and
    can re-route it. ``commit=False`` folds it into the decline command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="delegation_declined",
        target_kind="issue",
        target_id=issue_id,
        detail=(
            ""
            if released_generation is None
            else f"released generation {released_generation}"
        ),
        commit=commit,
    )


def record_contributor_removed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    user_id: int,
    commit: bool = True,
) -> None:
    """Record that a contributor was removed, stamped with their display name. Caller
    records only when a pairing was actually removed. ``commit=False`` folds it into
    the audited command's transaction."""
    contributor = users.get_user(conn, user_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="removed_contributor",
        target_kind="issue",
        target_id=issue_id,
        detail=contributor["name"] if contributor else "",
        commit=commit,
    )


def record_label_added(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    label_id: int,
    commit: bool = True,
) -> None:
    """Record that a label was attached, stamped with the label's name. Caller
    records only when the attach actually created a new pairing. ``commit=False``
    folds it into the audited command's transaction."""
    label = labels.get_label(conn, label_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="labeled",
        target_kind="issue",
        target_id=issue_id,
        detail=label["name"] if label else "",
        commit=commit,
    )


def record_label_removed(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    label_id: int,
    commit: bool = True,
) -> None:
    """Record that a label was detached, stamped with the label's name. Caller
    records only when a pairing was actually removed. ``commit=False`` folds it into
    the audited command's transaction."""
    label = labels.get_label(conn, label_id)
    activity.record(
        conn,
        actor_id=actor_id,
        verb="unlabeled",
        target_kind="issue",
        target_id=issue_id,
        detail=label["name"] if label else "",
        commit=commit,
    )


def record_commented(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    body: str = "",
    commit: bool = True,
) -> None:
    """Record that someone commented on the issue. The event targets the issue (so
    it lands on the issue's History and the global feed links there); the comment
    body itself lives on the issue, not duplicated into the trail's detail. Anyone
    named by [[user:N]] in the comment is mentioned (notified + auto-watched).

    ``commit=False`` composes this inside a command's transaction: the event, the
    watch, and the mentions all defer their commit to the command owner, so the comment
    and its whole activity footprint land or roll back together."""
    event = activity.record(
        conn,
        actor_id=actor_id,
        verb="commented",
        target_kind="issue",
        target_id=issue_id,
        commit=commit,
    )
    # Commenting is participation — the commenter starts watching the issue.
    notifications.watch(conn, actor_id, "issue", issue_id, commit=commit)
    notifications.process_mentions(
        conn, event_id=event["id"], actor_id=actor_id, text=body, commit=commit
    )


def record_comment_edited(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, commit: bool = True
) -> None:
    """Record that a comment's body was edited — the previously-silent half of the
    comment lifecycle (who rewrote content). No detail: the current body lives on the
    comment, and the edit's point is that the change is now on the record at all.
    ``commit=False`` composes inside a command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="comment_edited",
        target_kind="issue",
        target_id=issue_id,
        commit=commit,
    )


def record_comment_deleted(
    conn: sqlite3.Connection, *, actor_id: int, issue_id: int, commit: bool = True
) -> None:
    """Record that a comment was removed from the issue — the audit-worthy half of
    the comment lifecycle (who took content down). No detail: the comment is gone.
    ``commit=False`` composes inside a command's transaction."""
    activity.record(
        conn,
        actor_id=actor_id,
        verb="comment_deleted",
        target_kind="issue",
        target_id=issue_id,
        commit=commit,
    )


def record_archive_change(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    issue_id: int,
    before: str | None,
    after: str | None,
    commit: bool = True,
) -> None:
    """Record an archive (soft-delete) or restore. before/after are the issue's
    archived_at values — None means active, a timestamp means archived — so we
    compare PRESENCE, not the exact time: re-archiving an already-archived issue
    re-stamps the time but isn't a lifecycle change, and records nothing.
    ``commit=False`` lets the audited command fold the write and this event into
    one transaction."""
    if (before is not None) == (after is not None):
        return
    activity.record(
        conn,
        actor_id=actor_id,
        verb="archived" if after is not None else "unarchived",
        target_kind="issue",
        target_id=issue_id,
        commit=commit,
    )
