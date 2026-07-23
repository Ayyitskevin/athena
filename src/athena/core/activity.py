"""Data access for the activity log: an append-only audit trail.

Every meaningful write in Athena (an issue created, a status changed, an
assignment) records one row here through record(), stamped with WHO did it. This
is the history the architecture promises — "Grok closed AEGIS-88" as a recorded
fact, not a guess. All activity SQL lives here, mirroring aegis/issues.py and
aegis/comments.py.
"""

from __future__ import annotations

import csv
from collections.abc import Collection
from datetime import datetime
from io import StringIO
import sqlite3
from typing import cast

from athena.core import access, notifications, run_context

# Every read returns the actor's display name alongside the row, so a feed can
# render "Kevin closed AEGIS-12" without a second lookup.
_SELECT = (
    "SELECT a.id, a.actor_id, a.verb, a.target_kind, a.target_id, a.detail, "
    "a.created_at, a.run_id, a.parent_run_id, a.forked_from_event_id, "
    "a.imported_at, u.name AS actor_name FROM activity a "
    "JOIN users u ON u.id = a.actor_id"
)

# Sentinel for the read functions' `actor`: "no visibility gating" (internal callers,
# per-entity sections already gated upstream by their detail route's 404, and tests).
# Distinct from actor=None, a real anonymous viewer who sees only public targets.
_UNGATED = object()

# Native issue events snapshot their current project automatically. Transition
# recorders pass every project whose facts the event contains instead, so a later
# move cannot re-scope old private history through the issue's new container.
_AUTO_ISSUE_PROJECTS = object()


class RunBindingError(Exception):
    """A tagged write tried to continue a run id already bound to a DIFFERENT
    actor. Transport-neutral (main.py maps it to a 403); raised inside the
    caller's write transaction, so the refused write rolls back whole — the
    forged event never lands and the run's replay stays single-author."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run '{run_id}' is bound to another actor")
        self.run_id = run_id


def _bind_run(conn: sqlite3.Connection, run_id: str, actor_id: int) -> None:
    """First tagged writer claims the run id; anyone else is refused.

    Run ids are client-chosen strings, so without this any actor could write
    events into another actor's run and poison its replay/lineage. The check
    and the claim share the caller's immediate transaction, so two racing
    first-writers serialize on SQLite's single-writer reservation."""
    row = conn.execute(
        "SELECT actor_id FROM run_bindings WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO run_bindings (run_id, actor_id) VALUES (?, ?)",
            (run_id, actor_id),
        )
    elif row["actor_id"] != actor_id:
        raise RunBindingError(run_id)


def _like_pattern(value: str) -> str:
    """Return a literal LIKE pattern for operator search text."""
    escaped = (
        value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


_CSV_FIELDS = [
    "id",
    "created_at",
    "actor_id",
    "actor_name",
    "verb",
    "target_kind",
    "target_id",
    "detail",
    # Appended last so existing column positions are stable. Empty for app-recorded rows;
    # the import instant for rows replayed from a bundle — so the compliance/audit export
    # can tell imported foreign history from natively recorded provenance, the same
    # distinction the JSON feed's imported_at makes.
    "imported_at",
]


def to_csv(rows: list[dict]) -> str:
    """Serialize activity rows to a stable operator-export CSV."""
    out = StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=_CSV_FIELDS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def record(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    verb: str,
    target_kind: str,
    target_id: int,
    detail: str = "",
    commit: bool = True,
    issue_project_ids: Collection[int | None] | object = _AUTO_ISSUE_PROJECTS,
) -> dict:
    """Append one activity row and return it. Raises sqlite3.IntegrityError if
    actor_id isn't a real user (the foreign key refuses the orphan). Callers pass
    a controlled verb; this layer only writes.

    The event is stamped with the ambient run id, parent run id, and fork point (the
    X-Athena-Run / X-Athena-Parent-Run / X-Athena-Fork-From-Event headers for the
    request in flight, via run_context) so the caller never threads them through —
    each is NULL when not supplied, which is every untagged or top-level action.

    A tagged write also claims (or must match) the run id's binding: the first
    actor to write under a run id owns it, and a write by anyone else raises
    RunBindingError — rolling back the whole command, so a run's trail can never
    be spliced by a second author."""
    # Resolve every access-envelope input after acquiring SQLite's writer
    # reservation. A standalone commit=False call leaves this transaction open for
    # its command owner; nested command calls already hold the reservation.
    opened_transaction = not conn.in_transaction
    if opened_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        scope_is_complete = True
        if target_kind == "issue" and issue_project_ids is _AUTO_ISSUE_PROJECTS:
            row = conn.execute(
                "SELECT project_id FROM issues WHERE id = ?", (target_id,)
            ).fetchone()
            if row is None:
                resolved_project_ids = set()
                scope_is_complete = False
            else:
                resolved_project_ids = {row["project_id"]}
        elif target_kind == "issue":
            resolved_project_ids = set(cast(Collection[int | None], issue_project_ids))
        elif target_kind == "project":
            resolved_project_ids = {target_id}
        else:
            resolved_project_ids = set()
        resolved_project_ids.discard(None)

        resolved_scope_keys: set[str] = set()
        for project_id in sorted(resolved_project_ids):
            scope = conn.execute(
                "SELECT activity_scope_key FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if scope is None or scope["activity_scope_key"] is None:
                scope_is_complete = False
            else:
                resolved_scope_keys.add(scope["activity_scope_key"])

        run_id = run_context.get_run_id()
        if run_id is not None:
            _bind_run(conn, run_id, actor_id)
        cur = conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, run_id, parent_run_id, "
            "forked_from_event_id, visibility_restricted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor_id,
                verb,
                target_kind,
                target_id,
                detail,
                run_id,
                run_context.get_parent_run_id(),
                run_context.get_forked_from_event_id(),
                0 if scope_is_complete else 1,
            ),
        )
        event_id = cur.lastrowid
        assert event_id is not None
        conn.executemany(
            "INSERT INTO activity_visibility_projects "
            "(event_id, project_scope_key) VALUES (?, ?)",
            [
                (event_id, project_scope_key)
                for project_scope_key in sorted(resolved_scope_keys)
            ],
        )
        # Fan the event out to the inbox of anyone watching this target (not the actor).
        # Scope rows are already present, and notify_watchers doesn't commit, so the
        # event, its access envelope, and notifications land or roll back together.
        notifications.notify_watchers(
            conn,
            event_id=event_id,
            actor_id=actor_id,
            target_kind=target_kind,
            target_id=target_id,
        )
        if commit:
            conn.commit()
    except BaseException:
        if commit or opened_transaction:
            conn.rollback()
        raise
    recorded = get_activity(conn, event_id)
    assert recorded is not None
    return recorded


def get_activity(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE a.id = ?", (activity_id,)).fetchone()
    return dict(row) if row else None


def list_activity(
    conn: sqlite3.Connection,
    *,
    target_kind: str | None = None,
    target_id: int | None = None,
    actor_id: int | None = None,
    actor_is_agent: bool | None = None,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    verb: str | None = None,
    search: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """Activity newest first. Every filter is optional and independent: pass
    target_kind+target_id for one target's timeline, target_kind alone to scope
    the global feed to a kind, actor_id/verb/search to narrow who/what. actor_is_agent
    is the actor-type lens — True for agents only, False for humans only — answering
    "what did the agents do?" distinctly from human activity. run_id is deterministic
    replay: exactly the events tagged with that run. parent_run_id walks lineage the
    other way: the events of the CHILD runs a given run spawned. before_id is the
    paging cursor — only rows older than it (a.id < before_id), so the caller can walk
    back through history one page at a time on a stable, append-only ordering.

    `actor` gates the feed by visibility: an event whose target is an issue/page/space
    the actor can't see is dropped (in SQL, before the limit, so paging stays exact).
    The default (_UNGATED) applies no gate — internal callers, and the per-entity
    timelines whose detail route already 404s when the entity is hidden. Pass the real
    actor (a user dict, or None for anonymous) to gate the global feed."""
    clauses: list[str] = []
    params: list = []
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            clauses.append(gate)
            params.extend(gate_params)
    if target_kind is not None:
        clauses.append("a.target_kind = ?")
        params.append(target_kind)
    if target_id is not None:
        clauses.append("a.target_id = ?")
        params.append(target_id)
    if actor_id is not None:
        clauses.append("a.actor_id = ?")
        params.append(actor_id)
    if actor_is_agent is not None:
        # u is already joined for actor_name, so the lens is a cheap predicate on it.
        clauses.append("u.is_agent = ?")
        params.append(1 if actor_is_agent else 0)
    if run_id is not None:
        clauses.append("a.run_id = ?")
        params.append(run_id)
    if parent_run_id is not None:
        clauses.append("a.parent_run_id = ?")
        params.append(parent_run_id)
    if verb is not None:
        clauses.append("a.verb = ?")
        params.append(verb)
    if search is not None and search.strip():
        pattern = _like_pattern(search)
        clauses.append(
            "("
            "u.name LIKE ? ESCAPE '\\' OR "
            "a.verb LIKE ? ESCAPE '\\' OR "
            "a.target_kind LIKE ? ESCAPE '\\' OR "
            "CAST(a.target_id AS TEXT) LIKE ? ESCAPE '\\' OR "
            "a.detail LIKE ? ESCAPE '\\' OR "
            "a.created_at LIKE ? ESCAPE '\\' OR "
            "(a.target_kind || ' #' || a.target_id) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern] * 7)
    if before_id is not None:
        clauses.append("a.id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id DESC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def list_events(
    conn: sqlite3.Connection,
    *,
    after_id: int | None = None,
    target_kind: str | None = None,
    target_id: int | None = None,
    actor_id: int | None = None,
    actor_is_agent: bool | None = None,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    verb: str | None = None,
    limit: int = 50,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """Events in FORWARD (oldest-first) order for cursor consumption — the agent/
    integration view of the same append-only trail `list_activity` serves to humans.

    The audit log IS the event log: every recorded action already has a monotonic
    id, an actor, a verb, and a target. This inverts list_activity's cursor so a
    consumer can resume exactly where it left off: only rows with a.id > after_id,
    ordered ASC, so processing them in order and remembering the last id seen is a
    complete, gap-free subscription. (list_activity pages BACKWARD with before_id /
    DESC for a human scrolling recent history; an agent draining a stream wants the
    opposite.) Filters mirror list_activity so a consumer can narrow to one kind,
    one target, one actor, one verb, one actor type (actor_is_agent: True for agents
    only, False for humans only — so an integration can subscribe to just the agents'
    stream, or just the humans'), one run_id (the exact events of a known run, for
    deterministic replay), or one parent_run_id (the events of the child runs a run
    spawned, for walking lineage).

    `actor` gates the stream by visibility, exactly like list_activity: an event on a
    target the consumer can't see is dropped (in SQL, before the limit). The default
    (_UNGATED) is no gate, for the system webhook-delivery worker (an admin-configured
    firehose with no per-actor scope); the authenticated GET /events consumer passes
    its token's actor so an agent only drains the trail it may see."""
    clauses: list[str] = []
    params: list = []
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            clauses.append(gate)
            params.extend(gate_params)
    if after_id is not None:
        clauses.append("a.id > ?")
        params.append(after_id)
    if target_kind is not None:
        clauses.append("a.target_kind = ?")
        params.append(target_kind)
    if target_id is not None:
        clauses.append("a.target_id = ?")
        params.append(target_id)
    if actor_id is not None:
        clauses.append("a.actor_id = ?")
        params.append(actor_id)
    if actor_is_agent is not None:
        # u is already joined for actor_name, so the lens is a cheap predicate on it.
        clauses.append("u.is_agent = ?")
        params.append(1 if actor_is_agent else 0)
    if run_id is not None:
        clauses.append("a.run_id = ?")
        params.append(run_id)
    if parent_run_id is not None:
        clauses.append("a.parent_run_id = ?")
        params.append(parent_run_id)
    if verb is not None:
        clauses.append("a.verb = ?")
        params.append(verb)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id ASC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(value: str) -> datetime | None:
    """Parse the trail's stored timestamp ('YYYY-MM-DD HH:MM:SS', as datetime('now')
    writes it). Returns None on anything that doesn't match — a malformed stamp must
    not crash run reconstruction; it just can't contribute a gap."""
    try:
        return datetime.strptime(value, _TS_FORMAT)
    except (ValueError, TypeError):
        return None


def _run_summary(events: list[dict]) -> dict:
    """Wrap a contiguous group of one actor's events as a run: its span, its size,
    its run id (the X-Athena-Run all its events share, or None for a heuristic run),
    its parent run id (the run that spawned it, for lineage), and the events
    themselves (oldest-first) so a caller can replay the sequence."""
    first, last = events[0], events[-1]
    return {
        "actor_id": first["actor_id"],
        "actor_name": first["actor_name"],
        "run_id": first["run_id"],
        "parent_run_id": first["parent_run_id"],
        "forked_from_event_id": first["forked_from_event_id"],
        "started_at": first["created_at"],
        "ended_at": last["created_at"],
        "first_id": first["id"],
        "last_id": last["id"],
        "event_count": len(events),
        # True when this run may be clipped by the reconstruction window — i.e. older
        # events of it exist beyond the `limit` we loaded, so the count/start describe
        # only the part we can see. Set on the oldest run when the window was full.
        "partial": False,
        "events": events,
    }


def _starts_new_run(
    prev: dict,
    prev_ts: datetime | None,
    cur: dict,
    cur_ts: datetime | None,
    gap_seconds: int,
) -> bool:
    """Whether `cur` begins a NEW run after `prev`. An explicit run id is
    authoritative: if EITHER event carries one, they continue the same run only when
    BOTH carry the SAME id — so a tagged run holds together across any pause, two
    different ids split, and a tagged event never shares a run with an untagged one.
    Only when both events are untagged does the boundary fall back to the time gap."""
    prev_run, cur_run = prev["run_id"], cur["run_id"]
    if prev_run is not None or cur_run is not None:
        return not (
            prev_run is not None and cur_run is not None and prev_run == cur_run
        )
    if prev_ts is None or cur_ts is None:
        # No measurable gap → keep them together rather than split on a bad stamp.
        return False
    return (cur_ts - prev_ts).total_seconds() > gap_seconds


def reconstruct_runs(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    gap_seconds: int = 1800,
    limit: int = 200,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """Reconstruct one actor's recent activity into RUNS — a reading lens over the
    append-only log. A run is a maximal sequence of that actor's events that belong
    to one unit of work.

    Where events carry an explicit run id (the X-Athena-Run header, see migration
    0029), that id is AUTHORITATIVE: a run is a contiguous block of the actor's
    events sharing one run id, however far apart in time — deterministic replay, not
    a guess. Untagged events fall back to the gap heuristic: a run is a stretch with
    no quiet pause longer than gap_seconds. A tagged and an untagged event never
    share a run, nor do two different run ids.

    Reconstructed from the actor's most recent `limit` events, newest run first;
    WITHIN a run, events stay oldest-first so the run reads in the order it happened.
    Because list_activity already scopes to this actor, other actors' events neither
    appear in a run nor split one — a run is one actor's stretch of work, regardless
    of what anyone else did in between.

    The window is bounded by `limit`, so the OLDEST run may extend further back than
    we loaded. When the window came back full, that oldest run is marked
    "partial": True — its count/start describe only the events inside the window, not
    necessarily the whole run. Every other run is complete (a newer run cannot be
    clipped by an older boundary). Callers should treat a partial run's totals as a
    lower bound and widen `limit` to see the rest."""
    # Pull the actor's most recent events (newest-first), then walk them oldest-first
    # so each comparison weighs an event against the one immediately before it. The
    # viewing `actor` gates which events are visible (the runs reader can't see a run's
    # work on a target they can't see); _UNGATED leaves it ungated.
    ascending = list(
        reversed(list_activity(conn, actor_id=actor_id, limit=limit, actor=actor))
    )
    # A full window means there may be older events we didn't load, so the oldest
    # run could be clipped. A short window means we've seen all of this actor's
    # history — every run is whole.
    window_full = len(ascending) == limit

    runs: list[dict] = []
    current: list[dict] = []
    prev: dict | None = None
    prev_ts: datetime | None = None
    for event in ascending:
        ts = _parse_ts(event["created_at"])
        if current and _starts_new_run(
            cast(dict, prev), prev_ts, event, ts, gap_seconds
        ):
            runs.append(_run_summary(current))
            current = []
        current.append(event)
        prev, prev_ts = event, ts
    if current:
        runs.append(_run_summary(current))
    # The oldest run (first built, since we walked ascending) is the only one the
    # window can clip — flag it when the window was full.
    if window_full and runs:
        runs[0]["partial"] = True
    # Newest run first, matching how the feeds present recent activity.
    runs.reverse()
    return runs


# --- run lineage ------------------------------------------------------------

# How many of a run's events one lineage node loads. A run is one unit of work, so
# this is generous; a run beyond it has its node marked partial (a lower bound), the
# same honesty rule reconstruct_runs uses for a window-clipped run.
_LINEAGE_MAX_EVENTS = 500

# A fork contract returns the visible tail of the shared prefix. The cap keeps one
# request bounded while still including the fork point itself.
_FORK_PREFIX_MAX_EVENTS = 500


def _gated_run_events(
    conn: sqlite3.Connection, run_id: str, actor: dict | None | object
) -> list[dict]:
    """One run's events (every event tagged with run_id), oldest-first, with the
    viewing `actor`'s visibility gate applied so an event on a target the actor can't
    see is dropped — exactly like list_activity. Loads one past the cap so the caller
    can tell a clipped run from an exactly-full one. _UNGATED leaves it ungated."""
    clauses = ["a.run_id = ?"]
    params: list = [run_id]
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            clauses.append(gate)
            params.extend(gate_params)
    where = " WHERE " + " AND ".join(clauses)
    params.append(_LINEAGE_MAX_EVENTS + 1)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id ASC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def _run_node(
    conn: sqlite3.Connection,
    run_id: str,
    actor: dict | None | object,
    *,
    with_events: bool,
) -> dict | None:
    """A lineage node for one run, or None when the actor can see none of its events
    (hidden == missing — the run never leaks through lineage). `with_events` carries
    the full event list for the FOCAL run; ancestors/descendants are light (events
    dropped, summary kept) so a deep tree stays a bounded payload. A run past the event
    cap is marked partial, its count a lower bound."""
    events = _gated_run_events(conn, run_id, actor)
    if not events:
        return None
    clipped = len(events) > _LINEAGE_MAX_EVENTS
    if clipped:
        events = events[:_LINEAGE_MAX_EVENTS]
    node = _run_summary(events)
    node["partial"] = clipped
    if not with_events:
        node["events"] = []
    return node


def _child_run_ids(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """The run ids of the runs `run_id` spawned — the distinct run ids of events whose
    parent_run_id is this run — in the order they first appear (chronological), so the
    lineage tree reads in the order children were begotten."""
    rows = conn.execute(
        "SELECT run_id, MIN(id) AS first_id FROM activity "
        "WHERE parent_run_id = ? AND run_id IS NOT NULL AND run_id != ? "
        "GROUP BY run_id ORDER BY first_id",
        (run_id, run_id),
    ).fetchall()
    return [row["run_id"] for row in rows]


def _descendant_tree(
    conn: sqlite3.Connection,
    run_id: str,
    actor: dict | None | object,
    seen: set[str],
    max_depth: int,
    depth: int,
) -> list[dict]:
    """The tree of runs descended from `run_id` (its children, their children, …), each
    a light node carrying its own `children`. `seen` guards against a cycle in the
    parent links; `max_depth` bounds a pathological chain. A child the actor can't see
    is omitted, and its subtree with it."""
    if depth > max_depth:
        return []
    out: list[dict] = []
    for child_id in _child_run_ids(conn, run_id):
        if child_id in seen:
            continue
        seen.add(child_id)
        node = _run_node(conn, child_id, actor, with_events=False)
        if node is None:
            continue
        node["children"] = _descendant_tree(
            conn, child_id, actor, seen, max_depth, depth + 1
        )
        out.append(node)
    return out


def run_lineage(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    actor: dict | None | object = _UNGATED,
    max_depth: int = 25,
) -> dict | None:
    """The lineage of a tagged run, reconstructed from the log alone: its ANCESTORS
    (walk parent_run_id up to the originating goal — the root run with no parent),
    the RUN itself with its events (oldest-first, replayable), and its DESCENDANTS (the
    tree of child runs it spawned). This is the "end-to-end lineage from a high-level
    goal down to the individual action" the event-sourced design promises — pure
    projection of run_id/parent_run_id, no stored tree.

    Returns None if the run has no events the `actor` may see — a run hidden behind
    visibility is indistinguishable from one that never existed (404, no leak). The
    focal run carries full events; ancestors and descendants are light summaries (drill
    in via their own run_id). Ancestors are ordered root-first, so the list reads as the
    causal path "originating goal → … → this run"."""
    focal = _run_node(conn, run_id, actor, with_events=True)
    if focal is None:
        return None
    seen = {run_id}
    ancestors: list[dict] = []
    cur = focal["parent_run_id"]
    while cur is not None and cur not in seen and len(ancestors) < max_depth:
        seen.add(cur)
        node = _run_node(conn, cur, actor, with_events=False)
        if node is None:
            # An ancestor the actor can't see truncates the visible chain rather than
            # leaking that a hidden run sits above this one.
            break
        ancestors.append(node)
        cur = node["parent_run_id"]
    ancestors.reverse()  # originating goal first
    descendants = _descendant_tree(conn, run_id, actor, seen, max_depth, 1)
    return {
        "run_id": run_id,
        "ancestors": ancestors,
        "run": focal,
        "descendants": descendants,
    }


def _visible_event_in_run(
    conn: sqlite3.Connection,
    run_id: str,
    event_id: int,
    actor: dict | None | object,
) -> dict | None:
    """Return one visible event when it belongs to `run_id`, otherwise None."""
    clauses = ["a.run_id = ?", "a.id = ?"]
    params: list = [run_id, event_id]
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            clauses.append(gate)
            params.extend(gate_params)
    where = " WHERE " + " AND ".join(clauses)
    row = conn.execute(f"{_SELECT}{where}", params).fetchone()
    return dict(row) if row else None


def _fork_prefix_events(
    conn: sqlite3.Connection,
    run_id: str,
    event_id: int,
    actor: dict | None | object,
) -> tuple[list[dict], bool]:
    """The visible shared-prefix tail through `event_id`, oldest-first.

    The query walks backward from the fork point so the fork event is always present
    even when the prefix is larger than the cap; the returned list is reversed back
    into replay order.
    """
    clauses = ["a.run_id = ?", "a.id <= ?"]
    params: list = [run_id, event_id]
    if actor is not _UNGATED:
        gate, gate_params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            clauses.append(gate)
            params.extend(gate_params)
    where = " WHERE " + " AND ".join(clauses)
    params.append(_FORK_PREFIX_MAX_EVENTS + 1)
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY a.id DESC LIMIT ?", params
    ).fetchall()
    clipped = len(rows) > _FORK_PREFIX_MAX_EVENTS
    if clipped:
        rows = rows[:_FORK_PREFIX_MAX_EVENTS]
    return [dict(row) for row in reversed(rows)], clipped


def run_fork_contract(
    conn: sqlite3.Connection,
    parent_run_id: str,
    *,
    fork_from_event_id: int,
    fork_run_id: str,
    actor: dict | None | object = _UNGATED,
) -> dict | None:
    """Validate and describe how to fork a run from a specific event.

    Athena does not store mutable run rows. A fork starts when a client performs
    follow-up writes with three headers: X-Athena-Run (the new child id),
    X-Athena-Parent-Run (the source run), and X-Athena-Fork-From-Event (the exact
    event after whose shared prefix the child diverges). This function is the
    read-only contract endpoint behind that flow: it proves the fork point is visible
    and belongs to the parent run, returns the prefix to replay/inspect, and hands
    back the header set to use for the child run.

    Returns None when the parent run or fork point is not visible to the actor. Raises
    ValueError for malformed ids the caller can fix.
    """
    parent_id = run_context.normalize(parent_run_id)
    child_id = run_context.normalize(fork_run_id)
    if parent_id is None:
        raise ValueError("parent run id is required")
    if child_id is None:
        raise ValueError("fork_run_id is required")
    if child_id == parent_id:
        raise ValueError("fork_run_id must differ from parent run id")
    if fork_from_event_id <= 0:
        raise ValueError("fork_from_event_id must be positive")

    fork_event = _visible_event_in_run(conn, parent_id, fork_from_event_id, actor)
    if fork_event is None:
        return None
    prefix, prefix_partial = _fork_prefix_events(
        conn, parent_id, fork_from_event_id, actor
    )
    return {
        "parent_run_id": parent_id,
        "fork_run_id": child_id,
        "fork_from_event_id": fork_from_event_id,
        "fork_from_event": fork_event,
        "shared_prefix_events": prefix,
        "shared_prefix_event_count": len(prefix),
        "shared_prefix_partial": prefix_partial,
        "headers": {
            "X-Athena-Run": child_id,
            "X-Athena-Parent-Run": parent_id,
            "X-Athena-Fork-From-Event": str(fork_from_event_id),
        },
    }


def can_see_complete_run(
    conn: sqlite3.Connection,
    run_id: str,
    actor: dict | None,
) -> bool:
    """Whether an actor-visible replay would contain every event in the run."""
    total = conn.execute(
        "SELECT COUNT(*) FROM activity WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    if total == 0:
        return False
    gate, gate_params = access.event_visibility_clause(conn, actor, alias="a")
    if not gate:
        return True
    visible = conn.execute(
        f"SELECT COUNT(*) FROM activity a WHERE a.run_id = ? AND {gate}",
        (run_id, *gate_params),
    ).fetchone()[0]
    return visible == total


def distinct_verbs(
    conn: sqlite3.Connection,
    *,
    actor: dict | None | object = _UNGATED,
) -> list[str]:
    """The visible verbs that actually occur in the trail, alphabetical."""
    where = ""
    params: list = []
    if actor is not _UNGATED:
        gate, params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            where = f" WHERE {gate}"
    rows = conn.execute(
        f"SELECT DISTINCT a.verb FROM activity a{where} ORDER BY a.verb",
        params,
    ).fetchall()
    return [row["verb"] for row in rows]


def distinct_target_kinds(
    conn: sqlite3.Connection,
    *,
    actor: dict | None | object = _UNGATED,
) -> list[str]:
    """The visible target kinds that actually occur in the trail, alphabetical."""
    where = ""
    params: list = []
    if actor is not _UNGATED:
        gate, params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            where = f" WHERE {gate}"
    rows = conn.execute(
        f"SELECT DISTINCT a.target_kind FROM activity a{where} ORDER BY a.target_kind",
        params,
    ).fetchall()
    return [row["target_kind"] for row in rows]


def distinct_actors(
    conn: sqlite3.Connection,
    *,
    actor: dict | None | object = _UNGATED,
) -> list[dict]:
    """Actors represented in events visible to the viewer, ordered by id."""
    where = ""
    params: list = []
    if actor is not _UNGATED:
        gate, params = access.event_visibility_clause(
            conn, cast(dict | None, actor), alias="a"
        )
        if gate:
            where = f" WHERE {gate}"
    rows = conn.execute(
        "SELECT DISTINCT u.id, u.name, u.is_agent FROM activity a "
        f"JOIN users u ON u.id = a.actor_id{where} ORDER BY u.id",
        params,
    ).fetchall()
    return [
        {"id": row["id"], "name": row["name"], "is_agent": bool(row["is_agent"])}
        for row in rows
    ]
