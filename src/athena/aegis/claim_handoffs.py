"""Typed continuation context for yielded issue claims.

The table is authoritative operational state; activity supplies immutable audit,
actor, time, and run provenance. Handoff text is untrusted advisory input and must
only be rendered as escaped text. It never grants approval or executes instructions.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any


HANDOFF_TOKEN_HEX_CHARS = 32
DEFAULT_HISTORY_LIMIT = 10
MAX_HISTORY_LIMIT = 100


def new_handoff_token() -> str:
    return secrets.token_hex(HANDOFF_TOKEN_HEX_CHARS // 2)


def is_valid_handoff_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == HANDOFF_TOKEN_HEX_CHARS
        and all(char in "0123456789abcdef" for char in value)
    )


def encode_evidence(evidence: list[str]) -> str:
    return json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))


def _row_to_handoff(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "handoff_token": row["handoff_token"],
        "issue_id": row["issue_id"],
        "lease_generation": row["lease_generation"],
        "schema_version": row["schema_version"],
        "state": "awaiting_resume" if row["resume_event_id"] is None else "resumed",
        "reason": row["reason"],
        "note": row["note"],
        "attempted_work": row["attempted_work"],
        "evidence": json.loads(row["evidence_json"]),
        "blocking_question": row["blocking_question"],
        "resume_instructions": row["resume_instructions"],
        "yielded": {
            "event_id": row["yield_event_id"],
            "actor_id": row["yielded_by_id"],
            "actor_name": row["yielded_by_name"],
            "created_at": row["yielded_at"],
            "run_id": row["yield_run_id"],
        },
        "resumed": None
        if row["resume_event_id"] is None
        else {
            "event_id": row["resume_event_id"],
            "actor_id": row["resumed_by_id"],
            "actor_name": row["resumed_by_name"],
            "created_at": row["resumed_at"],
            "run_id": row["resume_run_id"],
            "lease_generation": row["resume_lease_generation"],
            "note": row["resume_note"],
        },
        "advisory_untrusted": True,
    }


_SELECT = (
    "SELECT h.*, "
    "yield_event.actor_id AS yielded_by_id, "
    "yield_actor.name AS yielded_by_name, "
    "yield_event.created_at AS yielded_at, "
    "yield_event.run_id AS yield_run_id, "
    "resume_event.actor_id AS resumed_by_id, "
    "resume_actor.name AS resumed_by_name, "
    "resume_event.created_at AS resumed_at, "
    "resume_event.run_id AS resume_run_id "
    "FROM issue_claim_handoffs h "
    "JOIN activity yield_event ON yield_event.id = h.yield_event_id "
    "JOIN users yield_actor ON yield_actor.id = yield_event.actor_id "
    "LEFT JOIN activity resume_event ON resume_event.id = h.resume_event_id "
    "LEFT JOIN users resume_actor ON resume_actor.id = resume_event.actor_id "
)


def create_handoff(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    yield_event_id: int,
    lease_generation: str,
    reason: str,
    note: str | None,
    attempted_work: str,
    evidence: list[str],
    blocking_question: str,
    resume_instructions: str,
    handoff_token: str | None = None,
) -> dict[str, Any]:
    token = handoff_token or new_handoff_token()
    conn.execute(
        "INSERT INTO issue_claim_handoffs "
        "(handoff_token, issue_id, yield_event_id, lease_generation, reason, "
        "note, attempted_work, evidence_json, blocking_question, resume_instructions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token,
            issue_id,
            yield_event_id,
            lease_generation,
            reason,
            note,
            attempted_work,
            encode_evidence(evidence),
            blocking_question,
            resume_instructions,
        ),
    )
    handoff = get_handoff(conn, issue_id=issue_id, handoff_token=token)
    assert handoff is not None
    return handoff


def get_handoff(
    conn: sqlite3.Connection, *, issue_id: int, handoff_token: str
) -> dict[str, Any] | None:
    row = conn.execute(
        _SELECT + "WHERE h.issue_id = ? AND h.handoff_token = ?",
        (issue_id, handoff_token),
    ).fetchone()
    return None if row is None else _row_to_handoff(row)


def get_open_handoff(conn: sqlite3.Connection, issue_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        _SELECT + "WHERE h.issue_id = ? AND h.resume_event_id IS NULL",
        (issue_id,),
    ).fetchone()
    return None if row is None else _row_to_handoff(row)


def open_handoffs_for_issues(
    conn: sqlite3.Connection, issue_ids: list[int]
) -> dict[int, dict[str, Any]]:
    ordered = sorted(set(issue_ids))
    if not ordered:
        return {}
    placeholders = ",".join("?" for _ in ordered)
    rows = conn.execute(
        _SELECT + f"WHERE h.issue_id IN ({placeholders}) "
        "AND h.resume_event_id IS NULL ORDER BY h.issue_id",
        ordered,
    ).fetchall()
    return {int(row["issue_id"]): _row_to_handoff(row) for row in rows}


def list_handoffs(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_HISTORY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY_LIMIT}")
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM issue_claim_handoffs WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()["n"]
    )
    rows = conn.execute(
        _SELECT + "WHERE h.issue_id = ? ORDER BY h.yield_event_id DESC LIMIT ?",
        (issue_id, limit),
    ).fetchall()
    return {
        "open": get_open_handoff(conn, issue_id),
        "items": [_row_to_handoff(row) for row in rows],
        "visible_total": total,
        "clipped": total > len(rows),
    }


def resume_handoff(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    handoff_token: str,
    resume_event_id: int,
    lease_generation: str,
    resume_note: str | None,
) -> dict[str, Any] | None:
    cursor = conn.execute(
        "UPDATE issue_claim_handoffs "
        "SET resume_event_id = ?, resume_lease_generation = ?, resume_note = ? "
        "WHERE issue_id = ? AND handoff_token = ? AND resume_event_id IS NULL",
        (
            resume_event_id,
            lease_generation,
            resume_note,
            issue_id,
            handoff_token,
        ),
    )
    if cursor.rowcount != 1:
        return None
    return get_handoff(conn, issue_id=issue_id, handoff_token=handoff_token)
