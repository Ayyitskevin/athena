"""Portable replay artifacts for one tagged Athena run.

The activity log remains the source of truth. This module freezes one run's
ordered events plus the run's lineage metadata into a JSON artifact that an
operator or agent can hand off for audit without re-executing side effects.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from athena.core import activity, run_context

SCHEMA = "athena.run_replay.v1"

_EVENT_PAGE_SIZE = 500
# A run is tagged by a client-supplied X-Athena-Run header with no server-side limit on
# how many events may share it, so a single run can accumulate arbitrarily many rows.
# Bound the artifact the way the lineage endpoint bounds a node (_LINEAGE_MAX_EVENTS):
# load at most this many focal events into one artifact, and flag it partial when a run
# exceeds the cap, rather than materializing an unbounded list + JSON body per request.
_MAX_REPLAY_EVENTS = 5000
_DEFAULT_ACTOR = object()

_SAFE_FIELDS = [
    "id",
    "actor_id",
    "verb",
    "target_kind",
    "target_id",
    "detail",
    "run_id",
    "parent_run_id",
    "forked_from_event_id",
]

_OBSERVATIONAL_FIELDS = [
    "actor_name",
    "created_at",
]


def export_run_replay_database(
    db_path: str | Path,
    run_id: str,
    artifact_path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Write one run replay artifact to ``artifact_path`` and return that path."""
    database = Path(db_path)
    destination = Path(artifact_path)
    if not database.exists():
        raise FileNotFoundError(f"database path does not exist: {database}")
    if not database.is_file():
        raise FileNotFoundError(f"database path is not a file: {database}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"run replay path already exists: {destination}")

    conn = _connect_readonly(database)
    try:
        artifact = build_run_replay_artifact(conn, run_id)
    finally:
        conn.close()
    if artifact is None:
        raise ValueError(f"no such run: {run_id}")

    _write_json_file(destination, artifact)
    return destination


def build_run_replay_artifact(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    actor: dict | None | object = _DEFAULT_ACTOR,
) -> dict | None:
    """Return a portable replay artifact for ``run_id``.

    Returns ``None`` when the run has no events visible to ``actor``. The focal run's
    events are paged from ``activity.list_events`` so the artifact is complete even
    when the lineage endpoint would cap the focal node's event payload.
    """
    normalized = run_context.normalize(run_id)
    if normalized is None:
        raise ValueError("run id is required")

    events = _all_run_events(conn, normalized, actor=actor)
    if not events:
        return None
    # Cap the focal payload; if the run has more, clip to the cap and flag it partial so
    # a consumer knows the artifact is a bounded window, not the whole run.
    partial = len(events) > _MAX_REPLAY_EVENTS
    if partial:
        events = events[:_MAX_REPLAY_EVENTS]

    lineage = _run_lineage(conn, normalized, actor=actor)
    if lineage is None:
        return None

    lineage = {
        "run_id": lineage["run_id"],
        "ancestors": [_light_node(node) for node in lineage["ancestors"]],
        "run": _summary_node(events, partial=partial),
        "descendants": [_light_node(node) for node in lineage["descendants"]],
    }

    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "generated_at": _utc_now(),
        "run_id": normalized,
        "event_count": len(events),
        # True when the run exceeded _MAX_REPLAY_EVENTS and this artifact holds only the
        # first cap events (event_count is then a lower bound on the whole run).
        "partial": partial,
        "first_event_id": events[0]["id"],
        "last_event_id": events[-1]["id"],
        "replay_order": "activity.id ASC",
        "determinism_contract": {
            "scope": (
                "Replay artifacts contain recorded activity facts only; consumers "
                "must not re-execute side effects from this JSON."
            ),
            "safe_fields": _SAFE_FIELDS,
            "observational_fields": _OBSERVATIONAL_FIELDS,
        },
        "lineage": lineage,
        "events": events,
    }


def _all_run_events(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    actor: dict | None | object,
) -> list[dict]:
    rows: list[dict] = []
    after_id: int | None = None
    while True:
        batch = activity.list_events(
            conn,
            after_id=after_id,
            run_id=run_id,
            limit=_EVENT_PAGE_SIZE,
            **_actor_kwargs(actor),
        )
        if not batch:
            break
        rows.extend(batch)
        after_id = batch[-1]["id"]
        if len(batch) < _EVENT_PAGE_SIZE:
            break
        # Stop once we hold at least one event past the cap: the caller trims to the cap
        # and flags partial. This bounds the in-memory list to cap + one page.
        if len(rows) > _MAX_REPLAY_EVENTS:
            break
    return rows


def _run_lineage(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    actor: dict | None | object,
) -> dict | None:
    return activity.run_lineage(conn, run_id, **_actor_kwargs(actor))


def _actor_kwargs(actor: dict | None | object) -> dict:
    if actor is _DEFAULT_ACTOR:
        return {}
    return {"actor": actor}


def _summary_node(events: list[dict], *, partial: bool = False) -> dict:
    first, last = events[0], events[-1]
    return {
        "actor_id": first["actor_id"],
        "actor_name": first["actor_name"],
        "run_id": first["run_id"],
        "parent_run_id": first["parent_run_id"],
        "forked_from_event_id": first["forked_from_event_id"],
        "partial": partial,
        "started_at": first["created_at"],
        "ended_at": last["created_at"],
        "first_id": first["id"],
        "last_id": last["id"],
        "event_count": len(events),
        "events": [],
        "children": [],
    }


def _light_node(node: dict) -> dict:
    out = {
        "actor_id": node["actor_id"],
        "actor_name": node["actor_name"],
        "run_id": node["run_id"],
        "parent_run_id": node["parent_run_id"],
        "forked_from_event_id": node["forked_from_event_id"],
        "partial": node["partial"],
        "started_at": node["started_at"],
        "ended_at": node["ended_at"],
        "first_id": node["first_id"],
        "last_id": node["last_id"],
        "event_count": node["event_count"],
        "events": [],
        "children": [],
    }
    if "children" in node:
        out["children"] = [_light_node(child) for child in node["children"]]
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json_file(destination: Path, data: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
