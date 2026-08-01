"""Deterministic, model-free evidence packets for Athena Rooms."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from athena.aegis import room_commands, room_timeline, rooms
from athena.core import access, db, search


SCHEMA = "athena.room-context.v1"
MAX_QUESTION_CHARS = 1_000
MAX_QUERY_TERMS = 12
MAX_SCOPE_ISSUES = 200
MAX_RELATED_PAGES = 200
DEFAULT_SELECTION_LIMIT = 12
MAX_SELECTION_LIMIT = 25
CANDIDATE_LIMIT_PER_TERM = 25
TIMELINE_CANDIDATE_LIMIT = 40
SNIPPET_LIMIT = 800
_SCOPE_CHUNK = 100

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "did",
        "do",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "were",
        "what",
        "which",
        "who",
        "with",
    }
)
_EXPANSIONS = {
    "blocked": ("blocker", "block"),
    "blockers": ("blocked", "block"),
    "decided": ("decision",),
    "decisions": ("decision",),
    "approved": ("approval",),
    "approvals": ("approval",),
}


class InvalidQuestion(ValueError):
    """A question violates the bounded room-context input contract."""

    kind = "invalid_question"
    status_code = 422


def normalize_question(question: str) -> str:
    """Return one whitespace-normalized question or reject unsafe ambiguity."""
    if not isinstance(question, str):
        raise InvalidQuestion("question must be a string")
    if (
        any(ord(char) < 32 and char not in "\t\n\r" for char in question)
        or "\x7f" in question
    ):
        raise InvalidQuestion("question contains control characters")
    normalized = " ".join(question.split())
    if not normalized:
        raise InvalidQuestion("question is required")
    if len(normalized) > MAX_QUESTION_CHARS:
        raise InvalidQuestion(
            f"question must be at most {MAX_QUESTION_CHARS} characters"
        )
    return normalized


def _selection_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1 or limit > MAX_SELECTION_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SELECTION_LIMIT}")
    return limit


def _query_terms(question: str) -> tuple[list[str], bool]:
    raw = [match.group(0).casefold() for match in _TOKEN_RE.finditer(question)]
    meaningful = [term for term in raw if term not in _STOP_WORDS] or raw
    ordered: list[str] = []
    seen: set[str] = set()
    for term in meaningful:
        for candidate in (term, *_EXPANSIONS.get(term, ())):
            if candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
    return ordered[:MAX_QUERY_TERMS], len(ordered) > MAX_QUERY_TERMS


def _issue_scope(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
) -> tuple[list[int], bool]:
    room_type = room["room_type"]
    if room_type == "work_item":
        issue_id = room.get("issue_id")
        if issue_id is None or not access.can_see_issue(conn, actor, int(issue_id)):
            return [], False
        return [int(issue_id)], False
    if room_type == "agent":
        agent_id = int(room["agent_id"])
        rows = conn.execute(
            "SELECT DISTINCT i.id FROM issues i WHERE i.project_id = ? "
            "AND i.archived_at IS NULL AND ("
            "i.assignee_id = ? OR EXISTS (SELECT 1 FROM issue_contributors ic "
            "WHERE ic.issue_id = i.id AND ic.user_id = ?) OR EXISTS ("
            "SELECT 1 FROM issue_leases lease WHERE lease.issue_id = i.id "
            "AND lease.holder_id = ?) OR EXISTS (SELECT 1 FROM activity a "
            "WHERE a.target_kind = 'issue' AND a.target_id = i.id "
            "AND a.actor_id = ? AND a.imported_at IS NULL)) ORDER BY i.id LIMIT ?",
            (
                room["project_id"],
                agent_id,
                agent_id,
                agent_id,
                agent_id,
                MAX_SCOPE_ISSUES + 1,
            ),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM issues WHERE project_id = ? AND archived_at IS NULL "
            "ORDER BY id LIMIT ?",
            (room["project_id"], MAX_SCOPE_ISSUES + 1),
        ).fetchall()
    ids = [int(row["id"]) for row in rows]
    return ids[:MAX_SCOPE_ISSUES], len(ids) > MAX_SCOPE_ISSUES


def _issue_scope_ids(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None = None,
) -> list[int]:
    """Compatibility helper for other bounded room projections."""
    return _issue_scope(conn, room, actor)[0]


def _chunks(values: list[int]) -> list[list[int]]:
    return [
        values[start : start + _SCOPE_CHUNK]
        for start in range(0, len(values), _SCOPE_CHUNK)
    ]


def _page_access_clause(
    actor: dict[str, Any] | None,
) -> tuple[str, list[Any]]:
    if actor is not None and actor.get("role") == "admin":
        return "1 = 1", []
    if actor is None:
        return "s.visibility = 'public'", []
    return (
        "(s.visibility = 'public' OR s.created_by = ? OR EXISTS ("
        "SELECT 1 FROM space_members sm "
        "WHERE sm.space_id = s.id AND sm.user_id = ?))",
        [actor["id"], actor["id"]],
    )


def _related_visible_page_scope(
    conn: sqlite3.Connection,
    issue_ids: list[int],
    actor: dict[str, Any] | None,
) -> tuple[list[int], bool]:
    page_ids: set[int] = set()
    access_sql, access_params = _page_access_clause(actor)
    clipped = False
    for chunk in _chunks(issue_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT DISTINCT candidate.page_id FROM ("
            "SELECT target_id AS page_id FROM links WHERE source_kind = 'issue' "
            f"AND source_id IN ({placeholders}) AND target_kind = 'page' "
            "UNION SELECT source_id AS page_id FROM links WHERE source_kind = 'page' "
            f"AND target_kind = 'issue' AND target_id IN ({placeholders}) "
            "UNION SELECT page_id FROM issue_runbooks "
            f"WHERE issue_id IN ({placeholders})"
            ") candidate JOIN pages pg ON pg.id = candidate.page_id "
            "JOIN spaces s ON s.id = pg.space_id "
            f"WHERE pg.archived_at IS NULL AND ({access_sql}) "
            "ORDER BY candidate.page_id LIMIT ?",
            [
                *chunk,
                *chunk,
                *chunk,
                *access_params,
                MAX_RELATED_PAGES + 1,
            ],
        ).fetchall()
        clipped = clipped or len(rows) > MAX_RELATED_PAGES
        page_ids.update(int(row["page_id"]) for row in rows)
    ordered = sorted(page_ids)
    return ordered[:MAX_RELATED_PAGES], clipped or len(ordered) > MAX_RELATED_PAGES


def _related_visible_page_ids(
    conn: sqlite3.Connection,
    issue_ids: list[int],
    actor: dict[str, Any] | None,
) -> list[int]:
    """Compatibility helper for other bounded room projections."""
    return _related_visible_page_scope(conn, issue_ids, actor)[0]


def _search_hits(
    conn: sqlite3.Connection,
    *,
    terms: list[str],
    kind: str,
    ids: list[int],
    actor: dict[str, Any] | None,
) -> tuple[dict[tuple[str, int], dict[str, Any]], bool]:
    hits: dict[tuple[str, int], dict[str, Any]] = {}
    clipped = False
    for chunk in _chunks(ids):
        for term in terms:
            batch = search.search(
                conn,
                term,
                kind=kind,
                ids=chunk,
                actor=actor,
                limit=CANDIDATE_LIMIT_PER_TERM,
            )
            clipped = clipped or len(batch) == CANDIDATE_LIMIT_PER_TERM
            for position, hit in enumerate(batch):
                key = (str(hit["kind"]), int(hit["source_id"]))
                candidate = {**hit, "_fts_position": position}
                current = hits.get(key)
                if current is None or position < int(current["_fts_position"]):
                    hits[key] = candidate
    return hits, clipped


def _latest_visible_activity_id(
    conn: sqlite3.Connection,
    *,
    actor: dict[str, Any] | None,
    target_kind: str,
    target_id: int,
) -> int | None:
    visible, params = access.event_visibility_clause(conn, actor, alias="a")
    where = "a.target_kind = ? AND a.target_id = ? AND a.imported_at IS NULL"
    query_params: list[Any] = [target_kind, target_id]
    if visible:
        where += f" AND ({visible})"
        query_params.extend(params)
    row = conn.execute(
        f"SELECT MAX(a.id) AS activity_id FROM activity a WHERE {where}",
        query_params,
    ).fetchone()
    return int(row["activity_id"]) if row and row["activity_id"] is not None else None


def _digest(*parts: str) -> str:
    canonical = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bounded_snippet(value: str | None) -> tuple[str, bool]:
    text = value or ""
    return text[:SNIPPET_LIMIT], len(text) > SNIPPET_LIMIT


def _unsafe_source(*values: object) -> bool:
    return any(
        room_commands.unsafe_room_payload_reason(str(value), reject_structured=True)
        is not None
        for value in values
        if value is not None and str(value)
    )


def _issue_record(
    conn: sqlite3.Connection,
    actor: dict[str, Any] | None,
    hit: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    issue_id = int(hit["source_id"])
    row = conn.execute(
        "SELECT i.title, i.body, i.created_at, i.project_seq, p.key AS project_key "
        "FROM issues i LEFT JOIN projects p ON p.id = i.project_id WHERE i.id = ?",
        (issue_id,),
    ).fetchone()
    assert row is not None
    raw_title = str(row["title"])
    raw_body = str(row["body"])
    raw_snippet = str(hit.get("snippet") or "")
    title, _ = room_timeline.project_authoritative_text(
        raw_title,
        redacted=room_timeline.REDACTED_TITLE,
        max_chars=rooms.MAX_TITLE_CHARS,
    )
    snippet, truncated = room_timeline.project_authoritative_text(
        raw_snippet,
        max_chars=SNIPPET_LIMIT,
    )
    source_unsafe = _unsafe_source(raw_title, raw_body, raw_snippet)
    activity_id = _latest_visible_activity_id(
        conn, actor=actor, target_kind="issue", target_id=issue_id
    )
    key = (
        f"{row['project_key']}-{row['project_seq']}"
        if row["project_key"] and row["project_seq"] is not None
        else f"#{issue_id}"
    )
    searchable = f"{raw_title} {raw_body}"
    return (
        {
            "record_type": "issue",
            "record_id": issue_id,
            "title": f"{key}: {title}",
            "snippet": snippet,
            "source_revision": activity_id or row["created_at"],
            "source_activity_id": activity_id,
            "digest_sha256": (None if source_unsafe else _digest(raw_title, raw_body)),
            "receipt": {"method": "GET", "path": f"/issues/{issue_id}"},
            "rank": 0,
            "snippet_truncated": truncated,
        },
        searchable,
    )


def _page_record(
    conn: sqlite3.Connection,
    actor: dict[str, Any] | None,
    hit: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    page_id = int(hit["source_id"])
    row = conn.execute(
        "SELECT title, body, created_at FROM pages WHERE id = ?", (page_id,)
    ).fetchone()
    assert row is not None
    revision = int(
        conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS revision "
            "FROM page_versions WHERE page_id = ?",
            (page_id,),
        ).fetchone()["revision"]
    )
    raw_title = str(row["title"])
    raw_body = str(row["body"])
    raw_snippet = str(hit.get("snippet") or "")
    title, _ = room_timeline.project_authoritative_text(
        raw_title,
        redacted=room_timeline.REDACTED_TITLE,
        max_chars=rooms.MAX_TITLE_CHARS,
    )
    snippet, truncated = room_timeline.project_authoritative_text(
        raw_snippet,
        max_chars=SNIPPET_LIMIT,
    )
    source_unsafe = _unsafe_source(raw_title, raw_body, raw_snippet)
    activity_id = _latest_visible_activity_id(
        conn, actor=actor, target_kind="page", target_id=page_id
    )
    searchable = f"{raw_title} {raw_body}"
    return (
        {
            "record_type": "page",
            "record_id": page_id,
            "title": title,
            "snippet": snippet,
            "source_revision": revision,
            "source_activity_id": activity_id,
            "digest_sha256": (None if source_unsafe else _digest(raw_title, raw_body)),
            "receipt": {"method": "GET", "path": f"/pages/{page_id}"},
            "rank": 0,
            "snippet_truncated": truncated,
        },
        searchable,
    )


def _timeline_records(
    conn: sqlite3.Connection,
    room_id: int,
    actor: dict[str, Any] | None,
    terms: list[str],
) -> tuple[list[tuple[dict[str, Any], str]], bool]:
    page = room_timeline.list_timeline(
        conn,
        room_id,
        actor=actor,
        limit=TIMELINE_CANDIDATE_LIMIT,
        native_only=True,
        current_room_events_only=True,
    )
    assert page is not None
    selected: list[tuple[dict[str, Any], str]] = []
    for item in page["items"]:
        searchable = " ".join(
            str(value or "")
            for value in (
                item["event_kind"],
                item["classification"],
                item["verb"],
                item["body"],
                item["actor"]["name"],
            )
        )
        folded = searchable.casefold()
        if not any(term in folded for term in terms):
            continue
        snippet, truncated = _bounded_snippet(item["body"])
        selected.append(
            (
                {
                    "record_type": "activity",
                    "record_id": item["activity_id"],
                    "title": f"{item['actor']['name']}: {item['verb']}",
                    "snippet": snippet,
                    "source_revision": item["activity_id"],
                    "source_activity_id": item["activity_id"],
                    "digest_sha256": (
                        None
                        if item["body"] == room_timeline.REDACTED_DETAIL
                        else item["content_sha256"]
                    ),
                    "receipt": {
                        "method": "GET",
                        "path": f"/events?after={int(item['activity_id']) - 1}",
                    },
                    "rank": 0,
                    "snippet_truncated": truncated,
                },
                searchable,
            )
        )
    return selected, bool(page["page"]["has_more"])


def _score(text: str, normalized_question: str, terms: list[str]) -> int:
    folded = text.casefold()
    overlap = sum(1 for term in terms if term in folded)
    phrase = 1 if normalized_question.casefold() in folded else 0
    return phrase * 1_000 + overlap * 10


def _build(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    actor: dict[str, Any] | None,
    question: str,
    limit: int,
) -> dict[str, Any]:
    snapshot_at = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS now"
    ).fetchone()["now"]
    terms, query_terms_clipped = _query_terms(question)
    issue_ids, scope_issues_clipped = _issue_scope(conn, room, actor)
    page_ids, related_pages_clipped = _related_visible_page_scope(
        conn, issue_ids, actor
    )

    issue_hits, issue_clipped = _search_hits(
        conn, terms=terms, kind="issue", ids=issue_ids, actor=actor
    )
    page_hits, page_clipped = _search_hits(
        conn, terms=terms, kind="page", ids=page_ids, actor=actor
    )
    candidates: list[tuple[dict[str, Any], str]] = [
        _issue_record(conn, actor, hit) for hit in issue_hits.values()
    ]
    candidates.extend(_page_record(conn, actor, hit) for hit in page_hits.values())
    timeline_candidates, timeline_clipped = _timeline_records(
        conn, int(room["id"]), actor, terms
    )
    candidates.extend(timeline_candidates)

    unique: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
    for record, searchable in candidates:
        key = (record["record_type"], str(record["record_id"]))
        unique[key] = (record, searchable)
    ranked = sorted(
        unique.values(),
        key=lambda item: (
            -_score(item[1], question, terms),
            item[0]["record_type"],
            str(item[0]["record_id"]),
        ),
    )
    selected = ranked[:limit]
    for rank, (record, _) in enumerate(selected, start=1):
        record["rank"] = rank

    candidate_scan_clipped = any(
        (
            query_terms_clipped,
            scope_issues_clipped,
            related_pages_clipped,
            issue_clipped,
            page_clipped,
            timeline_clipped,
        )
    )
    selection_clipped = len(ranked) > len(selected)
    omissions: list[dict[str, Any]] = []
    if query_terms_clipped:
        omissions.append(
            {
                "kind": "query_terms",
                "reason": "query_term_limit",
                "visible_count": len(terms),
            }
        )
    if scope_issues_clipped:
        omissions.append(
            {
                "kind": "room_issue_scope",
                "reason": "scope_issue_limit",
                "visible_count": len(issue_ids),
            }
        )
    if related_pages_clipped:
        omissions.append(
            {
                "kind": "related_knowledge_scope",
                "reason": "related_page_limit",
                "visible_count": len(page_ids),
            }
        )
    if not page_ids:
        omissions.append(
            {
                "kind": "related_knowledge",
                "reason": "none_visible_in_room_scope",
                "visible_count": 0,
            }
        )
    if candidate_scan_clipped:
        omissions.append(
            {
                "kind": "search_candidates",
                "reason": "bounded_candidate_scan",
                "visible_count": len(ranked),
            }
        )
    if selection_clipped:
        omissions.append(
            {
                "kind": "selected_records",
                "reason": "selection_limit",
                "visible_count": len(ranked) - len(selected),
            }
        )
    requester_name = (
        room_timeline.project_authoritative_text(
            actor["name"],
            redacted=room_timeline.REDACTED_TITLE,
            max_chars=rooms.MAX_TITLE_CHARS,
        )[0]
        if actor is not None
        else "anonymous"
    )
    requester = (
        {
            "id": actor["id"],
            "name": requester_name,
            "role": actor["role"],
            "is_agent": bool(actor["is_agent"]),
        }
        if actor is not None
        else {"id": None, "name": requester_name, "role": None, "is_agent": False}
    )
    return {
        "schema": SCHEMA,
        "room": room_timeline.public_room(room),
        "requester": requester,
        "query": {
            "normalized": question,
            "characters": len(question),
            "max_characters": MAX_QUESTION_CHARS,
        },
        "snapshot_at": snapshot_at,
        "records": [record for record, _ in selected],
        "bounds": {
            "query_term_limit": MAX_QUERY_TERMS,
            "selected_query_terms": len(terms),
            "query_terms_clipped": query_terms_clipped,
            "scope_issue_limit": MAX_SCOPE_ISSUES,
            "scoped_issue_count": len(issue_ids),
            "scope_issues_clipped": scope_issues_clipped,
            "related_page_limit": MAX_RELATED_PAGES,
            "related_page_count": len(page_ids),
            "related_pages_clipped": related_pages_clipped,
            "candidate_limit": CANDIDATE_LIMIT_PER_TERM,
            "selection_limit": limit,
            "visible_candidate_count": len(ranked),
            "selected_count": len(selected),
            "candidate_scan_clipped": candidate_scan_clipped,
            "candidate_count_is_lower_bound": candidate_scan_clipped,
            "selection_clipped": selection_clipped,
        },
        "omissions": omissions,
        "truncation": {
            "query": False,
            "query_terms": query_terms_clipped,
            "scope": scope_issues_clipped or related_pages_clipped,
            "candidate_scan": candidate_scan_clipped,
            "selection": selection_clipped,
            "snippets": sum(1 for record, _ in selected if record["snippet_truncated"]),
        },
        "uncertainty": {
            "notice": (
                "This is a bounded packet of currently visible recorded evidence, "
                "not an answer or a completeness guarantee."
            ),
            "does_not_assert": [
                "truth",
                "completeness",
                "approval",
                "current_execution",
                "causality",
            ],
        },
    }


def build_room_context(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    actor: dict[str, Any] | None,
    question: str,
    limit: int = DEFAULT_SELECTION_LIMIT,
) -> dict[str, Any] | None:
    """Build one authorization-consistent athena.room-context.v1 packet."""
    with db.transaction(conn):
        room = rooms.get_visible_room(
            conn, actor=actor, room_id=room_id, include_archived=True
        )
        if room is None:
            return None
        normalized = normalize_question(question)
        bounded = _selection_limit(limit)
        return _build(conn, room, actor, normalized, bounded)


build_context = build_room_context
