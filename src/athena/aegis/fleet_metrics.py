"""Bounded, visibility-safe fleet throughput metrics.

This module owns metric definitions, request normalization, evidence caps, SQL
visibility gates, coverage accounting, and lifecycle projection. HTTP, HTML, and
MCP are transport adapters over :func:`build_fleet_metrics`; none recalculates
business meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import sqlite3
from statistics import median
from typing import Any, Literal, TypedDict

from athena.core import access, db, users


SCHEMA = "athena.fleet_metrics.v1"
SCOPE = "visible_to_request_actor"
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 90
DEFAULT_ACTOR_LIMIT = 25
MAX_ACTOR_LIMIT = 100
EVIDENCE_EVENT_LIMIT = 20_000
HISTORY_EVENT_LIMIT = 50_000
_HISTORY_CHUNK_SIZE = 250

_DEFINITIONS = {
    "created": (
        "Visible native typed issue creation events in the half-open UTC period."
    ),
    "completed": (
        "Visible typed entries into status category done; reclosures count."
    ),
    "net_flow": (
        "Created minus completion events; event flow, not backlog change."
    ),
    "cycle_time": (
        "Creation or latest reopen to completion for full-visibility admins, "
        "only with a complete typed lifecycle chain."
    ),
    "attribution": (
        "The completion event performer and event-time human/agent snapshot, "
        "not creator or assignee."
    ),
}


class PeriodPayload(TypedDict):
    start: str
    end_exclusive: str
    timezone: Literal["UTC"]
    boundary: Literal["[start,end)"]
    days: int


class FilterPayload(TypedDict):
    project_id: int | None
    actor_id: int | None


class FlowPayload(TypedDict):
    created: int
    completed: int
    net: int


class CyclePayload(TypedDict):
    visibility_complete: bool
    median_seconds: float | None
    sample_count: int
    excluded_completions: int


class ActorTypePayload(TypedDict):
    human: int
    agent: int
    unknown: int


class ActorRowPayload(TypedDict):
    actor_id: int
    actor_name: str
    actor_type: Literal["human", "agent", "mixed", "unknown"]
    completions: int
    human_completions: int
    agent_completions: int
    unknown_completions: int


class CoveragePayload(TypedDict):
    visible_candidate_events: int
    included_typed_events: int
    excluded_legacy_events: int
    excluded_imported_events: int
    excluded_restricted_events: int
    excluded_orphan_events: int
    excluded_malformed_events: int
    cycle_samples_excluded: int
    complete: bool


class LimitsPayload(TypedDict):
    max_window_days: int
    evidence_event_limit: int
    history_event_limit: int
    actor_limit: int
    actor_rows_available: int
    actor_rows_truncated: bool


class FleetMetricsPayload(TypedDict):
    schema: str
    scope: str
    period: PeriodPayload
    filters: FilterPayload
    flow: FlowPayload
    cycle_time: CyclePayload
    completion_by_actor_type: ActorTypePayload
    actors: list[ActorRowPayload]
    coverage: CoveragePayload
    limits: LimitsPayload
    definitions: dict[str, str]


ErrorKind = Literal["invalid", "not_found", "too_large"]


class FleetMetricsError(ValueError):
    """A transport-neutral metrics request rejection."""

    def __init__(self, kind: ErrorKind, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True, slots=True)
class FleetMetricsQuery:
    start: date
    end: date
    project_id: int | None
    actor_id: int | None
    actor_limit: int

    @property
    def start_sql(self) -> str:
        return f"{self.start.isoformat()} 00:00:00"

    @property
    def end_sql(self) -> str:
        return f"{self.end.isoformat()} 00:00:00"


def _invalid(detail: str) -> FleetMetricsError:
    return FleetMetricsError("invalid", detail)


def _parse_date(value: str, field: str) -> date:
    if (
        not isinstance(value, str)
        or len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or not (value[:4] + value[5:7] + value[8:]).isascii()
        or not (value[:4] + value[5:7] + value[8:]).isdigit()
    ):
        raise _invalid(f"{field} must be a YYYY-MM-DD UTC date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _invalid(f"{field} must be a valid YYYY-MM-DD UTC date") from exc
    if parsed.isoformat() != value:
        raise _invalid(f"{field} must be a canonical YYYY-MM-DD UTC date")
    return parsed


def _parse_bounded_int(
    value: str | int | None,
    field: str,
    *,
    default: int | None = None,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise _invalid(f"{field} must be an ASCII-decimal integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        normalized = value.lstrip("0") or "0"
        maximum_text = str(maximum)
        if len(normalized) > len(maximum_text) or (
            len(normalized) == len(maximum_text)
            and normalized > maximum_text
        ):
            raise _invalid(f"{field} must be between 1 and {maximum}")
        parsed = int(normalized)
    else:
        raise _invalid(f"{field} must be an ASCII-decimal integer")
    if parsed < 1 or parsed > maximum:
        raise _invalid(f"{field} must be between 1 and {maximum}")
    return parsed


def parse_query(
    *,
    start: str | None = None,
    end: str | None = None,
    project_id: str | int | None = None,
    actor_id: str | int | None = None,
    actor_limit: str | int | None = None,
    today: date | None = None,
) -> FleetMetricsQuery:
    """Normalize one strict request without touching the database."""

    if (start is None) != (end is None):
        raise _invalid("start and end must be supplied together")
    if start is None:
        utc_today = today or datetime.now(timezone.utc).date()
        parsed_end = utc_today + timedelta(days=1)
        parsed_start = parsed_end - timedelta(days=DEFAULT_WINDOW_DAYS)
    else:
        parsed_start = _parse_date(start, "start")
        parsed_end = _parse_date(end or "", "end")
    days = (parsed_end - parsed_start).days
    if days < 1:
        raise _invalid("start must be before end")
    if days > MAX_WINDOW_DAYS:
        raise _invalid(f"period may not exceed {MAX_WINDOW_DAYS} days")
    parsed_project_id = _parse_bounded_int(project_id, "project_id")
    parsed_actor_id = _parse_bounded_int(actor_id, "actor_id")
    parsed_actor_limit = _parse_bounded_int(
        actor_limit,
        "actor_limit",
        default=DEFAULT_ACTOR_LIMIT,
        maximum=MAX_ACTOR_LIMIT,
    )
    assert parsed_actor_limit is not None
    return FleetMetricsQuery(
        start=parsed_start,
        end=parsed_end,
        project_id=parsed_project_id,
        actor_id=parsed_actor_id,
        actor_limit=parsed_actor_limit,
    )


_QUERY_FIELDS = {"start", "end", "project_id", "actor_id", "actor_limit"}


def parse_query_pairs(
    pairs: list[tuple[str, str]], *, today: date | None = None
) -> FleetMetricsQuery:
    """Reject unknown/repeated HTTP criteria, then use the shared normalizer."""

    raw: dict[str, str] = {}
    for key, value in pairs:
        if key not in _QUERY_FIELDS:
            raise _invalid(f"unsupported query parameter: {key}")
        if key in raw:
            raise _invalid(f"query parameter may appear only once: {key}")
        raw[key] = value
    return parse_query(**raw, today=today)


def _gate(
    conn: sqlite3.Connection, actor: dict | None
) -> tuple[str, list[Any]]:
    clause, params = access.event_visibility_clause(conn, actor, alias="a")
    return clause or "1 = 1", list(params)


def _evidence_statement(
    conn: sqlite3.Connection,
    query: FleetMetricsQuery,
    *,
    actor: dict | None,
    project_scope_key: str | None,
) -> tuple[str, list[Any]]:
    gate, gate_params = _gate(conn, actor)
    clauses = [
        "a.target_kind = 'issue'",
        "a.verb IN ('created', 'changed_status')",
        "a.created_at >= ?",
        "a.created_at < ?",
        f"({gate})",
    ]
    params: list[Any] = [query.start_sql, query.end_sql, *gate_params]
    if query.actor_id is not None:
        clauses.append("a.actor_id = ?")
        params.append(query.actor_id)
    if query.project_id is not None:
        clauses.append(
            "((f.event_id IS NOT NULL AND f.after_project_scope_key = ?) "
            "OR (f.event_id IS NULL AND (a.visibility_restricted = 1 OR EXISTS ("
            "SELECT 1 FROM activity_visibility_projects AS metric_scope "
            "WHERE metric_scope.event_id = a.id "
            "AND metric_scope.project_scope_key = ?))))"
        )
        params.extend((project_scope_key, project_scope_key))
    sql = (
        "SELECT a.id, a.actor_id, a.verb, a.target_id, a.created_at, "
        "a.imported_at, a.visibility_restricted, "
        "i.id AS current_issue_id, u.name AS actor_name, "
        "f.event_id AS fact_event_id, f.issue_id, f.previous_event_id, "
        "f.fact_version, f.event_kind, f.before_status, f.before_category, "
        "f.after_status, f.after_category, f.actor_kind "
        "FROM activity AS a "
        "LEFT JOIN issue_lifecycle_facts AS f ON f.event_id = a.id "
        "LEFT JOIN issues AS i ON i.id = a.target_id "
        "LEFT JOIN users AS u ON u.id = a.actor_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY a.created_at, a.id LIMIT ?"
    )
    params.append(EVIDENCE_EVENT_LIMIT + 1)
    return sql, params


def _event_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def _is_completion(row: sqlite3.Row | dict[str, Any]) -> bool:
    if row["event_kind"] == "created":
        return row["after_category"] == "done"
    return row["before_category"] != "done" and row["after_category"] == "done"


def _load_visible_lifecycle(
    conn: sqlite3.Connection,
    issue_ids: set[int],
    *,
    end_sql: str,
    actor: dict | None,
) -> list[dict[str, Any]]:
    gate, gate_params = _gate(conn, actor)
    ordered_ids = sorted(issue_ids)
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(ordered_ids), _HISTORY_CHUNK_SIZE):
        chunk = ordered_ids[offset : offset + _HISTORY_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        remaining = HISTORY_EVENT_LIMIT - len(rows)
        sql = (
            "SELECT a.id, a.target_id, a.created_at, f.issue_id, "
            "f.previous_event_id, f.event_kind, f.before_status, "
            "f.before_category, f.after_status, f.after_category "
            "FROM issue_lifecycle_facts AS f "
            "INDEXED BY idx_issue_lifecycle_issue_event "
            "CROSS JOIN activity AS a ON a.id = f.event_id "
            f"WHERE f.issue_id IN ({placeholders}) "
            "AND a.target_kind = 'issue' "
            "AND a.verb IN ('created', 'changed_status') "
            "AND a.created_at < ? "
            "AND a.imported_at IS NULL "
            "AND a.visibility_restricted = 0 "
            f"AND ({gate}) "
            "ORDER BY f.issue_id, f.event_id LIMIT ?"
        )
        fetched = conn.execute(
            sql,
            [*chunk, end_sql, *gate_params, remaining + 1],
        ).fetchall()
        rows.extend(dict(row) for row in fetched)
        if len(rows) > HISTORY_EVENT_LIMIT:
            raise FleetMetricsError(
                "too_large",
                "visible lifecycle history exceeds the evidence cap; "
                "narrow the period or filters",
            )
    rows.sort(key=lambda row: (row["issue_id"], row["id"]))
    return rows


def _cycle_seconds(
    completion: dict[str, Any],
    visible_by_event: dict[int, dict[str, Any]],
) -> float | None:
    finish = _event_datetime(completion["created_at"])
    if finish is None:
        return None
    if completion["event_kind"] == "created":
        return 0.0 if completion["after_category"] == "done" else None

    current = completion
    seen: set[int] = set()
    while True:
        current_id = int(current["id"])
        if current_id in seen:
            return None
        seen.add(current_id)
        previous_id = current["previous_event_id"]
        if previous_id is None:
            return None
        previous = visible_by_event.get(int(previous_id))
        if previous is None or previous["issue_id"] != completion["issue_id"]:
            return None
        if (
            current["before_status"] != previous["after_status"]
            or current["before_category"] != previous["after_category"]
        ):
            return None

        if previous["event_kind"] == "created":
            if previous["after_category"] == "done":
                return None
            start = _event_datetime(previous["created_at"])
            if start is None:
                return None
        elif (
            previous["before_category"] == "done"
            and previous["after_category"] != "done"
        ):
            start = _event_datetime(previous["created_at"])
            if start is None:
                return None
        else:
            current = previous
            continue

        elapsed = (finish - start).total_seconds()
        return elapsed if elapsed >= 0 else None


def _actor_rows(
    completions: list[dict[str, Any]], actor_limit: int
) -> tuple[list[ActorRowPayload], ActorTypePayload, int]:
    grouped: dict[int, dict[str, Any]] = {}
    type_totals: ActorTypePayload = {"human": 0, "agent": 0, "unknown": 0}
    for row in completions:
        kind = row["actor_kind"]
        if kind not in ("human", "agent"):
            kind = "unknown"
        type_totals[kind] += 1
        actor_id = int(row["actor_id"])
        item = grouped.setdefault(
            actor_id,
            {
                "actor_id": actor_id,
                "actor_name": row["actor_name"] or "Unknown actor",
                "completions": 0,
                "human_completions": 0,
                "agent_completions": 0,
                "unknown_completions": 0,
            },
        )
        item["completions"] += 1
        item[f"{kind}_completions"] += 1

    result: list[ActorRowPayload] = []
    for item in grouped.values():
        kinds = [
            kind
            for kind in ("human", "agent", "unknown")
            if item[f"{kind}_completions"]
        ]
        actor_type: Literal["human", "agent", "mixed", "unknown"]
        actor_type = kinds[0] if len(kinds) == 1 else "mixed"
        item["actor_type"] = actor_type
        result.append(item)
    result.sort(
        key=lambda item: (-item["completions"], item["actor_id"])
    )
    return result[:actor_limit], type_totals, len(result)


def build_fleet_metrics(
    conn: sqlite3.Connection,
    query: FleetMetricsQuery,
    *,
    actor: dict | None,
) -> FleetMetricsPayload:
    """Build one exact bounded projection from a single SQLite snapshot."""

    with db.transaction(conn):
        project_scope_key: str | None = None
        if query.project_id is not None:
            project_gate, project_gate_params = (
                access.project_visibility_clause(
                    actor, alias="metric_project"
                )
            )
            project = conn.execute(
                "SELECT activity_scope_key FROM projects AS metric_project "
                f"WHERE metric_project.id = ? AND ({project_gate})",
                [query.project_id, *project_gate_params],
            ).fetchone()
            if project is None:
                raise FleetMetricsError("not_found", "no such project")
            project_scope_key = project["activity_scope_key"]

        sql, params = _evidence_statement(
            conn,
            query,
            actor=actor,
            project_scope_key=project_scope_key,
        )
        evidence = conn.execute(sql, params).fetchall()
        if len(evidence) > EVIDENCE_EVENT_LIMIT:
            raise FleetMetricsError(
                "too_large",
                "visible metric evidence exceeds the cap; "
                "narrow the period or filters",
            )

        coverage: CoveragePayload = {
            "visible_candidate_events": len(evidence),
            "included_typed_events": 0,
            "excluded_legacy_events": 0,
            "excluded_imported_events": 0,
            "excluded_restricted_events": 0,
            "excluded_orphan_events": 0,
            "excluded_malformed_events": 0,
            "cycle_samples_excluded": 0,
            "complete": True,
        }
        created = 0
        completions: list[dict[str, Any]] = []
        for raw_row in evidence:
            row = dict(raw_row)
            if row["imported_at"] is not None:
                coverage["excluded_imported_events"] += 1
                continue
            if row["visibility_restricted"]:
                coverage["excluded_restricted_events"] += 1
                continue
            if row["fact_event_id"] is None:
                key = (
                    "excluded_orphan_events"
                    if row["current_issue_id"] is None
                    else "excluded_legacy_events"
                )
                coverage[key] += 1
                continue
            if (
                row["fact_version"] != 1
                or row["event_kind"] not in ("created", "status_transition")
                or _event_datetime(row["created_at"]) is None
            ):
                coverage["excluded_malformed_events"] += 1
                continue
            coverage["included_typed_events"] += 1
            if row["event_kind"] == "created":
                created += 1
            if _is_completion(row):
                completions.append(row)

        cycle_visibility_complete = (
            actor is not None and actor.get("role") == users.ADMIN_ROLE
        )
        cycle_samples: list[float] = []
        if completions and cycle_visibility_complete:
            history = _load_visible_lifecycle(
                conn,
                {int(row["issue_id"]) for row in completions},
                end_sql=query.end_sql,
                actor=actor,
            )
            history_by_event = {int(row["id"]): row for row in history}
            for completion in completions:
                seconds = _cycle_seconds(completion, history_by_event)
                if seconds is None:
                    coverage["cycle_samples_excluded"] += 1
                else:
                    cycle_samples.append(seconds)
        elif completions:
            coverage["cycle_samples_excluded"] = len(completions)

        actor_rows, actor_type_totals, actor_rows_available = _actor_rows(
            completions, query.actor_limit
        )
        excluded_keys = (
            "excluded_legacy_events",
            "excluded_imported_events",
            "excluded_restricted_events",
            "excluded_orphan_events",
            "excluded_malformed_events",
            "cycle_samples_excluded",
        )
        coverage["complete"] = not any(coverage[key] for key in excluded_keys)
        completed = len(completions)
        cycle_median = (
            float(median(cycle_samples)) if cycle_samples else None
        )
        return {
            "schema": SCHEMA,
            "scope": SCOPE,
            "period": {
                "start": query.start.isoformat(),
                "end_exclusive": query.end.isoformat(),
                "timezone": "UTC",
                "boundary": "[start,end)",
                "days": (query.end - query.start).days,
            },
            "filters": {
                "project_id": query.project_id,
                "actor_id": query.actor_id,
            },
            "flow": {
                "created": created,
                "completed": completed,
                "net": created - completed,
            },
            "cycle_time": {
                "median_seconds": cycle_median,
                "sample_count": len(cycle_samples),
                "excluded_completions": coverage["cycle_samples_excluded"],
                "visibility_complete": cycle_visibility_complete,
            },
            "completion_by_actor_type": actor_type_totals,
            "actors": actor_rows,
            "coverage": coverage,
            "limits": {
                "max_window_days": MAX_WINDOW_DAYS,
                "evidence_event_limit": EVIDENCE_EVENT_LIMIT,
                "history_event_limit": HISTORY_EVENT_LIMIT,
                "actor_limit": query.actor_limit,
                "actor_rows_available": actor_rows_available,
                "actor_rows_truncated": actor_rows_available > len(actor_rows),
            },
            "definitions": dict(_DEFINITIONS),
        }
