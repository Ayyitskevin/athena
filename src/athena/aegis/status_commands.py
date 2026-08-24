"""Application commands for per-project status configuration.

A project's status set IS its issue lifecycle: adding a ``done``-category status
changes what "closed" means for the board, for ``dependencies.open_blockers``,
and for the optional blocked-close policy. Removing one takes a name out of the
vocabulary that historical events still refer to. Both are durable governance
writes — and both were bare data-layer calls that took no actor, opened their
own ``conn.commit()``, and emitted nothing. The append-only trail could not
answer "who added 'shipped'?" or "who deleted the status this issue used to be
in?", and that was the last unaudited durable write in the Aegis project surface.

These commands own each write end to end: authorization from live rows, the
validation, the row change, and the activity event inside one
``db.transaction(conn, immediate=True)``. REST and the browser are thin callers
that translate ``StatusCommandError.kind`` through their own status tables.

Two things here are deliberate rather than incidental.

**Authorization is unchanged, only relocated.** The rule stays what both
transports already enforced: visibility first, then creator-only, via
``access.container_write_reason`` — so a private project the actor cannot see
reads as 404, never as a 403 that confirms it exists. In particular an *agent*
that created a project may still configure its statuses, exactly as before. That
is not an oversight and not an endorsement: re-authorizing a surface is a
product decision, and a migration slice that silently narrowed (or widened) who
may write would make this diff impossible to review as a migration. The write
role, token scope, and paused flag ARE re-resolved from live rows inside the
write lock, because the transport's resolution happened before the lock and a
credential revoked in between must not still write.

**The duplicate probe is case-insensitive, but only here.** Migration 0024
declares ``UNIQUE (project_id, name COLLATE NOCASE)`` while
``statuses.is_valid`` compares with Python ``==``, so adding "Open" to a project
that already has "open" passed validation and then died on the constraint —
an unhandled ``sqlite3.IntegrityError``, i.e. a 500 on both transports. This
refuses it as a clean 409 instead. The fix is scoped to ``add_status`` on
purpose: making ``statuses.is_valid`` itself case-insensitive would let "Open"
pass issue-status validation and then fall off ``statuses.category_of`` (which
still compares exactly), turning one 500 into a different, worse one inside an
issue write transaction.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import projects, statuses
from athena.core import access, activity, db, identity, tokens, users

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_STATUS_ADDED = "project_status_added"
VERB_STATUS_REMOVED = "project_status_removed"


class StatusCommandError(Exception):
    """A transport-neutral status-configuration rejection.

    Carries an error KIND, never a status code; adapters map kinds through
    their own ``STATUS_BY_KIND``: "not_found" for a missing *or hidden*
    project and an unknown status, "forbidden" for a visible project the
    actor does not own, "conflict" for a duplicate name or a removal the
    project's own state refuses, "invalid" for malformed input.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _authorized_project(
    conn: sqlite3.Connection, *, actor: dict, project_id: int
) -> dict:
    """Resolve a project this actor may configure, from rows read under the write
    lock, or raise.

    Ordering is load-bearing and matches ``access.container_write_reason``'s
    contract: a project the actor cannot see is reported as missing, so neither
    404 nor 403 tells a non-member that a private project exists.
    """
    project = projects.get_project(conn, project_id)
    if project is None:
        raise StatusCommandError("not_found", "no such project")

    live_actor = users.get_user(conn, actor["id"])
    if live_actor is None:
        raise StatusCommandError("not_found", "no such project")
    # Merge rather than replace: the token's scopes live on the resolved actor
    # the transport built, not on the users row.
    effective = {**actor, **live_actor}

    reason = access.container_write_reason(
        conn,
        effective,
        kind="project",
        container_id=project_id,
        created_by=project["created_by"],
    )
    if reason == "not_visible":
        raise StatusCommandError("not_found", "no such project")
    if reason is not None:
        raise StatusCommandError(
            "forbidden", "only the project's creator may configure its statuses"
        )

    # Re-resolved under the lock: the transport authorized a credential that may
    # have been paused, demoted, or narrowed since.
    if (
        effective.get("paused_at")
        or effective.get("removed_at")
        or not identity.can_write(effective)
        or not identity.token_has_scope(effective, tokens.ISSUE_WRITE_SCOPE)
    ):
        raise StatusCommandError(
            "forbidden", "only the project's creator may configure its statuses"
        )
    return project


# Each data-layer refusal mapped to the error kind every transport translates.
# Keyed on the REASON_* constants so a reworded message cannot silently change a
# conflict into an invalid the way substring matching did.
_KIND_BY_REASON = {
    statuses.REASON_NAME_REQUIRED: "invalid",
    statuses.REASON_BAD_CATEGORY: "invalid",
    statuses.REASON_DUPLICATE: "conflict",
    statuses.REASON_UNKNOWN: "not_found",
    statuses.REASON_LAST_STATUS: "conflict",
    statuses.REASON_IN_USE: "conflict",
}


def _refuse(reason: str) -> StatusCommandError:
    return StatusCommandError(_KIND_BY_REASON[reason], reason)


def add_status(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    project_id: int,
    name: str,
    category: str,
) -> list[dict]:
    """Append a status to a project atomically with its ``project_status_added``
    event, and return the project's resulting ordered status set.

    The read-back happens inside the transaction, so the returned menu is the one
    this command produced rather than a second, racier read after the commit.
    """
    with db.transaction(conn, immediate=True):
        _authorized_project(conn, actor=actor, project_id=project_id)
        reason = statuses.add_status(conn, project_id, name, category, commit=False)
        if reason is not None:
            raise _refuse(reason)
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_STATUS_ADDED,
            target_kind="project",
            target_id=project_id,
            detail=f"{name.strip()} ({category})",
            commit=False,
        )
        return statuses.list_statuses(conn, project_id)


def remove_status(
    conn: sqlite3.Connection, *, actor: dict, project_id: int, name: str
) -> list[dict]:
    """Remove a status from a project atomically with its
    ``project_status_removed`` event, and return the resulting status set.

    The guard and the DELETE now share one ``immediate=True`` transaction. Before,
    they were two statements outside any transaction, so an issue could be moved
    INTO this status between the in-use count and the delete and be orphaned by a
    check that had already passed.

    The event's detail is the name, captured before the row is gone: like a project
    deletion, the event outlives the thing it names.
    """
    with db.transaction(conn, immediate=True):
        _authorized_project(conn, actor=actor, project_id=project_id)
        reason = statuses.remove_status(conn, project_id, name, commit=False)
        if reason is not None:
            raise _refuse(reason)
        activity.record(
            conn,
            actor_id=actor["id"],
            verb=VERB_STATUS_REMOVED,
            target_kind="project",
            target_id=project_id,
            detail=name,
            commit=False,
        )
        return statuses.list_statuses(conn, project_id)
