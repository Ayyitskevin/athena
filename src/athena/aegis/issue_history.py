"""Reconstructing an issue's lifecycle state as of a past point — the inverse of
issue_activity's recorders.

issue_activity APPENDS a diff event for each lifecycle change (status `before → after`,
assigned/unassigned, labeled/unlabeled, moved/removed sprint, set/removed parent,
archived/unarchived). This folds those same events forward into the issue's state AS OF
any event id — deterministic time-travel over the append-only log, with no stored
snapshots.

HONEST SCOPE — only the fields the log records as diffs are reconstructed: status,
priority, assignee, labels, sprint, parent, archived. An issue is born with all of these
empty/at-default (create_issue sets only title/body/status/priority/project), so folding
the events forward is EXACT — and status/priority even recover their CREATION value from
the first change's recorded `before`, so a cutoff before any change still resolves
exactly. NOT reconstructed: title/body CONTENT (never snapshotted into the log) and
project membership (its event detail is the new name only, so the value before the first
move isn't recoverable). Those are intentionally absent rather than guessed — the log is
the source of truth, and this never invents a value it can't derive from it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from athena.aegis import issues
from athena.core import access, activity, db

# The separator issue_activity.record_status_change / record_priority_change write
# between the old and new value: detail = f"{before} → {after}". Splitting on it inverts
# the recorder. (Kept in sync with that module by this single literal.)
_ARROW = " → "

# Bound one exact projection. We query one extra row and reject over-cap histories;
# silently dropping the oldest events would turn time-travel into a false reconstruction.
_MAX_EVENTS = 10000

_UNGATED = object()


class IncompleteIssueHistory(PermissionError):
    """The actor can see the issue now, but not its complete event-time history."""


class IssueHistoryTooLarge(RuntimeError):
    """The exact projection cap was exceeded; no partial state was returned."""


def _before_after(detail: str) -> tuple[str, str] | None:
    """Split a "before → after" detail back into its two sides, or None if it doesn't
    carry the arrow (a malformed/foreign detail contributes no transition)."""
    before, sep, after = detail.partition(_ARROW)
    return (before, after) if sep else None


def _project_before_after(
    events: list[dict], cutoff_id: int | None, verb: str, current: str
) -> str:
    """A field logged as `before → after` (status, priority), as of the cutoff. The
    first change's `before` is the creation value, so a cutoff before any change resolves
    to it exactly; with no changes at all the field never moved, so it equals `current`."""
    changes = [
        (e["id"], ba)
        for e in events
        if e["verb"] == verb and (ba := _before_after(e["detail"])) is not None
    ]
    if not changes:
        return current
    applicable = [c for c in changes if cutoff_id is None or c[0] <= cutoff_id]
    if applicable:
        return applicable[-1][1][1]  # the most recent `after` at or before the cutoff
    return changes[0][1][0]  # before the first change ever = the creation value


def _project_set_clear(
    events: list[dict],
    cutoff_id: int | None,
    set_verb: str,
    clear_verb: str,
    value_of: Callable[[dict], object],
    default: object,
) -> object:
    """A field set/cleared by paired verbs (assigned/unassigned, moved/removed sprint,
    set/removed parent, archived/unarchived), as of the cutoff. Starts at `default` (its
    born value, since creation never sets these) and folds each event forward."""
    result = default
    for e in events:
        if cutoff_id is not None and e["id"] > cutoff_id:
            break
        if e["verb"] == set_verb:
            result = value_of(e)
        elif e["verb"] == clear_verb:
            result = default
    return result


def _project_labels(events: list[dict], cutoff_id: int | None) -> list[str]:
    """The label set as of the cutoff — labeled adds a name, unlabeled removes it,
    starting from the empty set an issue is born with."""
    names: set[str] = set()
    for e in events:
        if cutoff_id is not None and e["id"] > cutoff_id:
            break
        if e["verb"] == "labeled":
            names.add(e["detail"])
        elif e["verb"] == "unlabeled":
            names.discard(e["detail"])
    return sorted(names)


def project_issue_state(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    as_of_event_id: int | None = None,
    actor: dict | None | object = _UNGATED,
) -> dict | None:
    """Project one exact, access-consistent snapshot of an issue's history."""
    with db.transaction(conn):
        return _project_issue_state(
            conn, issue_id, as_of_event_id=as_of_event_id, actor=actor
        )


def _project_issue_state(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    as_of_event_id: int | None = None,
    actor: dict | None | object = _UNGATED,
) -> dict | None:
    """Reconstruct exact lifecycle state as of an activity event.

    Internal callers may use the ungated default. Actor-facing callers pass the viewer;
    if any event is legacy-restricted or scoped to a project the viewer cannot see, the
    whole projection is rejected instead of folding a partial trail and calling it fact.
    """
    issue = issues.get_issue(conn, issue_id)
    if issue is None:
        return None
    if actor is not _UNGATED:
        assert actor is None or isinstance(actor, dict)
        if not access.can_see_complete_issue_history(conn, actor, issue_id):
            raise IncompleteIssueHistory(
                "complete issue history is not available to this actor"
            )
    newest_first = activity.list_activity(
        conn,
        target_kind="issue",
        target_id=issue_id,
        limit=_MAX_EVENTS + 1,
    )
    if len(newest_first) > _MAX_EVENTS:
        raise IssueHistoryTooLarge("issue history exceeds the exact projection limit")
    events = list(reversed(newest_first))
    visible = [e for e in events if as_of_event_id is None or e["id"] <= as_of_event_id]
    last = visible[-1] if visible else None
    return {
        "issue_id": issue_id,
        "as_of_event_id": last["id"] if last else None,
        "as_of": last["created_at"] if last else None,
        # "Current" means viewing the present: no cutoff, an event-less issue, or a
        # cutoff at/after the latest event. A cutoff that resolves to NO visible event
        # (as_of below the issue's first event id) is the born/PAST state, not the
        # present — so `last is None` with events present must read False, not True.
        "is_current": (
            not events
            or as_of_event_id is None
            or (last is not None and last["id"] == events[-1]["id"])
        ),
        "state": {
            "status": _project_before_after(
                events, as_of_event_id, "changed_status", issue["status"]
            ),
            "priority": _project_before_after(
                events, as_of_event_id, "changed_priority", issue["priority"]
            ),
            "assignee": _project_set_clear(
                events,
                as_of_event_id,
                "assigned",
                "unassigned",
                lambda e: e["detail"],
                None,
            ),
            "labels": _project_labels(events, as_of_event_id),
            "sprint": _project_set_clear(
                events,
                as_of_event_id,
                "moved_to_sprint",
                "removed_from_sprint",
                lambda e: e["detail"],
                None,
            ),
            "parent": _project_set_clear(
                events,
                as_of_event_id,
                "set_parent",
                "removed_parent",
                lambda e: e["detail"],
                None,
            ),
            "archived": _project_set_clear(
                events,
                as_of_event_id,
                "archived",
                "unarchived",
                lambda e: True,
                False,
            ),
        },
    }
