"""Typed continuation context for yielded issue claims.

The table is authoritative operational state; activity supplies immutable audit,
actor, time, and run provenance. Handoff text is untrusted advisory input and must
only be rendered as escaped text. It never grants approval or executes instructions.
"""

from __future__ import annotations

from datetime import datetime
import json
import secrets
import sqlite3
from typing import Any

from athena.aegis import leases
from athena.core import access, db


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


def _author(
    account_id: int, is_agent: object, imported_at: str | None
) -> dict[str, Any] | None:
    """``authored_by`` (core/authorship.py) from columns the row already joins."""
    if imported_at is not None:
        return None
    return {"id": account_id, "is_agent": bool(is_agent)}


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
        # The note, attempted work, evidence, question and instructions are the
        # yielding account's words; the resume note is the resumer's (below).
        "authored_by": _author(
            row["yielded_by_id"], row["yielded_by_is_agent"], row["yield_imported_at"]
        ),
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
            "authored_by": _author(
                row["resumed_by_id"],
                row["resumed_by_is_agent"],
                row["resume_imported_at"],
            ),
        },
        "advisory_untrusted": True,
    }


_SELECT = (
    "SELECT h.*, "
    "yield_event.actor_id AS yielded_by_id, "
    "yield_actor.name AS yielded_by_name, "
    "yield_actor.is_agent AS yielded_by_is_agent, "
    "yield_event.imported_at AS yield_imported_at, "
    "yield_event.created_at AS yielded_at, "
    "yield_event.run_id AS yield_run_id, "
    "resume_event.actor_id AS resumed_by_id, "
    "resume_actor.name AS resumed_by_name, "
    "resume_actor.is_agent AS resumed_by_is_agent, "
    "resume_event.imported_at AS resume_imported_at, "
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


#: How many replies an open handoff carries (AD6), and how much of each.
MAX_REPLIES = 3
MAX_REPLY_EXCERPT_CHARS = 500


def _with_replies(conn: sqlite3.Connection, handoff: dict[str, Any]) -> dict[str, Any]:
    """An open handoff with what other accounts have said on the issue since
    the yield (AD6): the newest comments posted at or after it by anyone but
    the account that yielded, so whoever picks the work up reads the answer to
    the blocking question with the question. Imported comments are not replies.
    With no reply yet the two fields are absent.
    Advisory text like the handoff itself: data, never instructions."""
    rows = conn.execute(
        "SELECT c.id, c.author_id, u.name AS author_name, u.is_agent, c.body, "
        "c.created_at FROM comments c JOIN users u ON u.id = c.author_id "
        "WHERE c.issue_id = ? AND c.created_at >= ? AND c.author_id != ? "
        "AND c.imported_at IS NULL ORDER BY c.id DESC LIMIT ?",
        (
            handoff["issue_id"],
            handoff["yielded"]["created_at"],
            handoff["yielded"]["actor_id"],
            MAX_REPLIES + 1,
        ),
    ).fetchall()
    if not rows:
        # No reply yet: the handoff is unchanged, so a lane of open handoffs
        # costs nothing more until someone answers.
        return handoff
    total = len(rows)
    if total > MAX_REPLIES:
        total = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM comments WHERE issue_id = ? "
                "AND created_at >= ? AND author_id != ? AND imported_at IS NULL",
                (
                    handoff["issue_id"],
                    handoff["yielded"]["created_at"],
                    handoff["yielded"]["actor_id"],
                ),
            ).fetchone()["n"]
        )
    return {
        **handoff,
        "replies": [
            {
                "comment_id": row["id"],
                "author": {"id": row["author_id"], "name": row["author_name"]},
                "authored_by": {
                    "id": row["author_id"],
                    "is_agent": bool(row["is_agent"]),
                },
                "excerpt": row["body"][:MAX_REPLY_EXCERPT_CHARS],
                "body_chars": len(row["body"]),
                "created_at": row["created_at"],
            }
            for row in rows[:MAX_REPLIES]
        ],
        "replies_total": total,
    }


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
    return None if row is None else _with_replies(conn, _row_to_handoff(row))


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
    return {
        int(row["issue_id"]): _with_replies(conn, _row_to_handoff(row)) for row in rows
    }


def open_handoffs_for_holder(
    conn: sqlite3.Connection, *, actor: dict, now: datetime, limit: int
) -> dict[str, Any]:
    """Visible open asks on every live possession, bounded only after filtering."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    with db.transaction(conn):
        visible = access.visible_project_filter(conn, actor)
        gate = ""
        params: list[Any] = [actor["id"], leases.clock_stamp(now)]
        if visible is not None:
            if visible:
                placeholders = ",".join("?" for _ in visible)
                gate = (
                    f" AND (i.project_id IS NULL OR i.project_id IN ({placeholders}))"
                )
                params.extend(sorted(visible))
            else:
                gate = " AND i.project_id IS NULL"
        rows = conn.execute(
            _SELECT + "JOIN issues i ON i.id = h.issue_id "
            "JOIN issue_leases l ON l.issue_id = h.issue_id "
            "WHERE l.holder_id = ? AND l.expires_at > ? "
            "AND h.resume_event_id IS NULL" + gate + " ORDER BY h.issue_id LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        return {
            "items": [
                _with_replies(conn, _row_to_handoff(row)) for row in rows[:limit]
            ],
            "clipped": len(rows) > limit,
        }


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
