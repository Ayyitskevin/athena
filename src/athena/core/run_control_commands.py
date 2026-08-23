"""Commands for run controls — operator requests on a live run, and the answers.

Two directions meet here, authorized in opposite ways, exactly as they do for the
worker kill lever.

*Downward* (an operator steering a run) is admin-only and audited. It records a
bounded request addressed to the agent that owns the run; it does not, and
cannot, change what any process is doing.

*Upward* (the bound agent answering) repeats ``worker_commands``' discipline: a
bearer token, an agent account, a live write scope, all re-resolved inside the
write transaction — plus one more check with no worker analog: the settling
identity must BE the agent the control was admitted against, and the run's
ownership story must not have drifted since admission.

Each durable write is one command owning authorization, validation, the mutation,
and its activity event in one immediate transaction. Settlements are
compare-and-set (dispatch's ``record_terminal`` pattern), so a raced second
settlement is a 0-row no-op answered with ``conflict``, never a double outcome.

Expiry is nobody's action, so it has no command and no event: an unanswered
control ages into the derived ``expired`` state exactly the way a silent worker
ages into ``stale``.
"""

from __future__ import annotations

from datetime import datetime
import json
import secrets
import sqlite3
from typing import Literal

from athena.core import (
    activity,
    agent_run_checkins,
    db,
    identity,
    run_context,
    run_controls,
    tokens,
)

ErrorKind = Literal[
    "unauthorized", "forbidden", "not_found", "invalid", "conflict", "capacity"
]

VERB_REQUESTED = "run_control_requested"
VERB_ACKNOWLEDGED = "run_control_acknowledged"
VERB_DECLINED = "run_control_declined"
VERB_COMPLETED = "run_control_completed"

TARGET_KIND = "run_control"

MAX_PAYLOAD_CHARS = 4_000
MAX_RESULT_SUMMARY_CHARS = 2_000
MAX_IDEMPOTENCY_KEY_CHARS = 255

# The fresh-context handoff is a closed, bounded envelope: a reader can enumerate
# every field, and nothing unbounded — transcripts, reasoning, environment — has
# anywhere to hide. Limits echo the claim-handoff precedent (lease_commands).
MAX_HANDOFF_SUMMARY_CHARS = 2_000
MAX_HANDOFF_QUESTIONS = 10
MAX_HANDOFF_QUESTION_CHARS = 500
MAX_HANDOFF_REFS = 20
MAX_HANDOFF_REF_CHARS = 200
MAX_HANDOFF_EVIDENCE_ITEMS = 10
MAX_HANDOFF_EVIDENCE_ITEM_CHARS = 500
MAX_HANDOFF_JSON_CHARS = 8_000
_HANDOFF_LIST_FIELDS = {
    "unresolved_questions": (MAX_HANDOFF_QUESTIONS, MAX_HANDOFF_QUESTION_CHARS),
    "athena_refs": (MAX_HANDOFF_REFS, MAX_HANDOFF_REF_CHARS),
    "evidence_refs": (MAX_HANDOFF_EVIDENCE_ITEMS, MAX_HANDOFF_EVIDENCE_ITEM_CHARS),
}

MIN_TTL_SECONDS = run_controls.MIN_TTL_SECONDS
MAX_TTL_SECONDS = run_controls.MAX_TTL_SECONDS

#: (kind, operator-facing label) pairs for the web form — the labels promise
#: only what a control actually is: a recorded request.
CONTROL_KIND_CHOICES = (
    (run_controls.KIND_STEER, "Steer — hand the agent bounded guidance"),
    (
        run_controls.KIND_REQUEST_CANCEL,
        "Request cancel — ask the agent to wind this run down",
    ),
    (
        run_controls.KIND_REQUEST_FRESH_CONTEXT,
        "Request fresh context — ask for a structured handoff",
    ),
)

_WRITE_SCOPES = frozenset(
    {tokens.ISSUE_WRITE_SCOPE, tokens.DOCS_WRITE_SCOPE, tokens.ADMIN_SCOPE}
)


class RunControlCommandError(Exception):
    """A transport-neutral run-control rejection."""

    def __init__(self, kind: ErrorKind, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


STATUS_BY_KIND: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "capacity": 429,
}


def _admin(actor: dict | None) -> dict:
    """The operator side: admin role plus, for bearer callers, the admin scope."""
    if actor is None:
        raise RunControlCommandError("unauthorized", "authentication required")
    if not identity.is_admin(actor):
        raise RunControlCommandError("forbidden", "admin role required")
    if not identity.token_has_scope(actor, tokens.ADMIN_SCOPE):
        raise RunControlCommandError(
            "forbidden", f"token scope required: {tokens.ADMIN_SCOPE}"
        )
    return actor


def _agent_credentials(actor: dict | None) -> tuple[int, int]:
    """The (agent_id, token_id) of a settling agent's credential, or a refusal.

    A browser session or trusted-header actor carries no token and cannot settle:
    a control is consumed by a running process holding a credential, and letting
    a human session answer as the agent would put fiction in the record."""
    if actor is None:
        raise RunControlCommandError("unauthorized", "authentication required")
    token_id = actor.get("_token_id")
    if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 1:
        raise RunControlCommandError("forbidden", "bearer token required")
    if not actor.get("is_agent"):
        raise RunControlCommandError("forbidden", "agent account required")
    scopes = actor.get("_token_scopes")
    if scopes is None or not _WRITE_SCOPES.intersection(scopes):
        raise RunControlCommandError("forbidden", "token scope required: a write scope")
    agent_id = actor.get("id")
    if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id < 1:
        raise RunControlCommandError("forbidden", "agent account required")
    return agent_id, token_id


def _recheck_credentials(
    conn: sqlite3.Connection, *, agent_id: int, token_id: int
) -> None:
    """Re-resolve the security facts inside the write transaction, so a stale or
    fabricated actor dict cannot settle after revocation, pause, or unmarking.
    Mirrors worker_commands, plus the paused check issue_commands carries."""
    owner = conn.execute(
        "SELECT paused_at, removed_at FROM users WHERE id = ? AND is_agent = 1",
        (agent_id,),
    ).fetchone()
    token = conn.execute(
        "SELECT scopes FROM api_tokens WHERE id = ? AND user_id = ? "
        "AND revoked_at IS NULL",
        (token_id, agent_id),
    ).fetchone()
    if owner is None:
        raise RunControlCommandError("forbidden", "agent account required")
    if owner["removed_at"] is not None:
        raise RunControlCommandError("forbidden", "account is removed")
    if owner["paused_at"] is not None:
        raise RunControlCommandError("forbidden", "account is paused")
    if token is None:
        raise RunControlCommandError("forbidden", "live bearer token required")
    try:
        live_scopes = tokens.parse_scopes(token["scopes"])
    except ValueError as exc:
        raise RunControlCommandError(
            "forbidden", "token scope required: a write scope"
        ) from exc
    if not _WRITE_SCOPES.intersection(live_scopes):
        raise RunControlCommandError("forbidden", "token scope required: a write scope")


def _bounded_text(raw: object, *, field: str, maximum: int, required: bool) -> str:
    """Normalize one bounded human-text field: stripped, size-capped, printable
    plus ordinary newlines/tabs. Guidance is prose, so line breaks are legitimate;
    everything else in the control range is not."""
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise RunControlCommandError("invalid", f"{field} must be a string")
    value = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(
        (ord(character) < 0x20 and character not in "\n\t") or ord(character) == 0x7F
        for character in value
    ):
        raise RunControlCommandError(
            "invalid", f"{field} must not contain control characters"
        )
    if len(value) > maximum:
        raise RunControlCommandError(
            "invalid", f"{field} must be at most {maximum} characters"
        )
    if required and not value:
        raise RunControlCommandError("invalid", f"{field} is required")
    return value


def _idempotency_key(raw: object) -> str:
    """A caller-chosen single-flight key, or a freshly minted one (icarus
    precedent). The format matches the transport middleware's header rule so one
    key can serve both layers."""
    if raw is None:
        return secrets.token_hex(16)
    if not isinstance(raw, str):
        raise RunControlCommandError("invalid", "idempotency_key must be a string")
    if not (1 <= len(raw) <= MAX_IDEMPOTENCY_KEY_CHARS) or any(
        not (0x21 <= ord(character) <= 0x7E) for character in raw
    ):
        raise RunControlCommandError(
            "invalid",
            "idempotency_key must be 1-255 visible ASCII characters",
        )
    return raw


def _ttl_seconds(raw: object) -> int:
    if raw is None:
        return run_controls.default_ttl_seconds()
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RunControlCommandError("invalid", "ttl_seconds must be an integer")
    if not (MIN_TTL_SECONDS <= raw <= MAX_TTL_SECONDS):
        raise RunControlCommandError(
            "invalid",
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}",
        )
    return raw


def _validated_handoff(raw: object) -> tuple[str, str]:
    """Validate a fresh-context handoff and return (summary, canonical JSON).

    The envelope is closed: unknown fields are refused rather than stored, so
    nothing unbounded can ride along. Every list item is bounded text."""
    if not isinstance(raw, dict):
        raise RunControlCommandError("invalid", "handoff must be an object")
    unknown = set(raw) - ({"summary"} | set(_HANDOFF_LIST_FIELDS))
    if unknown:
        raise RunControlCommandError(
            "invalid",
            "handoff fields are summary, unresolved_questions, athena_refs, "
            "evidence_refs",
        )
    summary = _bounded_text(
        raw.get("summary"),
        field="handoff summary",
        maximum=MAX_HANDOFF_SUMMARY_CHARS,
        required=True,
    )
    canonical: dict[str, object] = {"summary": summary}
    for field, (max_items, max_chars) in _HANDOFF_LIST_FIELDS.items():
        values = raw.get(field)
        if values is None:
            values = []
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise RunControlCommandError(
                "invalid", f"handoff {field} must be a list of strings"
            )
        if len(values) > max_items:
            raise RunControlCommandError(
                "invalid", f"handoff {field} must contain at most {max_items} items"
            )
        items = [
            _bounded_text(
                value,
                field=f"handoff {field} item",
                maximum=max_chars,
                required=True,
            )
            for value in values
        ]
        canonical[field] = items
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded) > MAX_HANDOFF_JSON_CHARS:
        raise RunControlCommandError(
            "invalid",
            f"encoded handoff must be at most {MAX_HANDOFF_JSON_CHARS} characters",
        )
    return summary, encoded


def _resolve_run_owner(conn: sqlite3.Connection, run_id: str) -> int:
    """The agent a control on this run must bind to, or a refusal.

    The run binding (first tagged writer) is authoritative when present. A run
    that has only checked in cooperatively is steerable through its sole
    reporting agent; two agents reporting one unbound run id is ambiguity this
    command refuses rather than resolves."""
    bound_actor = activity.run_binding_actor(conn, run_id)
    if bound_actor is None:
        checkin_agents = agent_run_checkins.checkin_agent_ids(conn, run_id)
        if not checkin_agents:
            raise RunControlCommandError("not_found", "no such run")
        if len(checkin_agents) > 1:
            raise RunControlCommandError(
                "conflict",
                "run ownership is ambiguous: multiple agents report this run id",
            )
        bound_actor = checkin_agents[0]
    owner = conn.execute(
        "SELECT is_agent, paused_at, removed_at FROM users WHERE id = ?",
        (bound_actor,),
    ).fetchone()
    if owner is None or not owner["is_agent"]:
        raise RunControlCommandError("invalid", "run is not owned by an agent account")
    if owner["removed_at"] is not None:
        raise RunControlCommandError(
            "conflict",
            "the run's agent is removed; its runs accept no further controls",
        )
    if owner["paused_at"] is not None:
        raise RunControlCommandError(
            "conflict",
            "the run's agent is paused; resume it before issuing controls",
        )
    return bound_actor


def _control_matches_request(
    existing: dict,
    *,
    run_id: str,
    kind: str,
    payload: str,
    worker_id: int | None,
    ttl_seconds: int,
) -> bool:
    """Whether an existing control IS the request being retried, field for field.

    The stored row keeps timestamps rather than the requested lifetime, so the
    TTL is compared as the exact created→expires span — a retry asking for a
    different lifetime is a different request and must refuse, not silently
    return a control that dies at the original deadline."""
    stored_ttl = int(
        (
            datetime.strptime(existing["expires_at"], "%Y-%m-%d %H:%M:%S")
            - datetime.strptime(existing["created_at"], "%Y-%m-%d %H:%M:%S")
        ).total_seconds()
    )
    return (
        existing["run_id"] == run_id
        and existing["kind"] == kind
        and existing["payload"] == payload
        and existing["worker_id"] == worker_id
        and stored_ttl == ttl_seconds
    )


def create_control(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    run_id: object,
    kind: object,
    payload: object = None,
    worker_id: object = None,
    ttl_seconds: object = None,
    idempotency_key: object = None,
    now: datetime | None = None,
) -> dict:
    """Record one operator control request against a run, atomically audited.

    This writes a request addressed to the run's bound agent. It does not steer,
    cancel, or refresh anything itself — the agent answers on its own schedule or
    the request quietly expires. The returned projection carries ``replayed``:
    True when an identical request already existed under the caller's
    idempotency key and that existing control is being returned instead."""
    admin = _admin(actor)
    try:
        normalized_run_id = run_context.strict_run_id(run_id)
    except ValueError as exc:
        raise RunControlCommandError("invalid", str(exc)) from exc
    if run_context.is_reserved_run_id(normalized_run_id):
        raise RunControlCommandError(
            "invalid",
            "server-reserved runs cannot receive controls: no agent polls them",
        )
    if kind not in run_controls.CONTROL_KINDS:
        raise RunControlCommandError(
            "invalid",
            f"kind must be one of: {', '.join(run_controls.CONTROL_KINDS)}",
        )
    normalized_payload = _bounded_text(
        payload,
        field="payload",
        maximum=MAX_PAYLOAD_CHARS,
        # Guidance with nothing to say is not steering; the other kinds carry an
        # optional reason.
        required=kind == run_controls.KIND_STEER,
    )
    if worker_id is not None and (
        isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 1
    ):
        raise RunControlCommandError("invalid", "worker_id must be a positive integer")
    ttl = _ttl_seconds(ttl_seconds)
    key = _idempotency_key(idempotency_key)
    now_stamp = run_controls.stamp(now)

    with db.transaction(conn, immediate=True):
        existing = run_controls.get_by_idempotency_key(
            conn, requested_by=admin["id"], idempotency_key=key
        )
        if existing is not None:
            if _control_matches_request(
                existing,
                run_id=normalized_run_id,
                kind=str(kind),
                payload=normalized_payload,
                worker_id=worker_id,
                ttl_seconds=ttl,
            ):
                replay = run_controls.projection(existing, now=now)
                replay["replayed"] = True
                return replay
            raise RunControlCommandError(
                "conflict",
                "idempotency_key was already used for a different control",
            )
        agent_id = _resolve_run_owner(conn, normalized_run_id)
        if worker_id is not None:
            worker = conn.execute(
                "SELECT agent_id, stopped_at FROM agent_workers WHERE id = ?",
                (worker_id,),
            ).fetchone()
            if worker is None or worker["agent_id"] != agent_id:
                raise RunControlCommandError(
                    "invalid", "worker_id does not name a worker of the run's agent"
                )
            if worker["stopped_at"] is not None:
                raise RunControlCommandError(
                    "conflict", "the targeted worker reported stopped"
                )
        live = run_controls.find_live_control(
            conn, run_id=normalized_run_id, kind=str(kind), now_stamp=now_stamp
        )
        if live is not None:
            raise RunControlCommandError(
                "conflict",
                f"a live {kind} control already exists for this run",
            )
        control_id = run_controls.insert_control(
            conn,
            run_id=normalized_run_id,
            agent_id=agent_id,
            worker_id=worker_id,
            kind=str(kind),
            payload=normalized_payload,
            requested_by=admin["id"],
            idempotency_key=key,
            ttl_seconds=ttl,
            now_stamp=now_stamp,
        )
        event = activity.record(
            conn,
            actor_id=admin["id"],
            verb=VERB_REQUESTED,
            target_kind=TARGET_KIND,
            target_id=control_id,
            detail=f"{kind} for run {normalized_run_id}",
            commit=False,
        )
        run_controls.set_requested_event(
            conn, control_id=control_id, event_id=event["id"]
        )
        # The audit event above stamps the OPERATOR's ambient run context, and
        # for an UNBOUND (check-in-only) target run whose id the operator's own
        # request happens to carry in X-Athena-Run, that record would claim the
        # run binding FOR THE OPERATOR — one transaction writing two ownership
        # stories, an unsettleable control, and an agent locked out of its own
        # run id. Re-reading the binding here catches that (and any other way a
        # conflicting binding could appear mid-transaction) and unwinds the
        # whole admission, binding included.
        binding_now = activity.run_binding_actor(conn, normalized_run_id)
        if binding_now is not None and binding_now != agent_id:
            raise RunControlCommandError(
                "invalid",
                "this request is tagged with the target run id and would bind "
                "the run to you; retry without X-Athena-Run set to it",
            )
        created = run_controls.get_control(conn, control_id)
        assert created is not None
        result = run_controls.projection(created, now=now)
        result["replayed"] = False
        return result


def _settlement_target(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    control_id: int,
    now: datetime | None,
) -> dict:
    """The control this agent may answer, with every fail-closed refusal.

    A control that is missing, or bound to a different agent, answers with the
    same not_found — cross-agent probes learn nothing. A control whose run
    ownership drifted since admission (a binding appearing under a different
    actor than the check-in the admission trusted) refuses settlement rather
    than letting either party act on a stale binding."""
    row = run_controls.get_control(conn, control_id)
    if row is None or row["agent_id"] != agent_id:
        raise RunControlCommandError("not_found", "no such run control")
    current_binding = activity.run_binding_actor(conn, row["run_id"])
    if current_binding is not None and current_binding != row["agent_id"]:
        raise RunControlCommandError(
            "conflict", "run ownership changed since this control was issued"
        )
    state = run_controls.derived_state(row, now=now)
    if state in (run_controls.STATE_COMPLETED, run_controls.STATE_DECLINED):
        raise RunControlCommandError("conflict", "control is already settled")
    if state == run_controls.STATE_EXPIRED:
        raise RunControlCommandError("conflict", "control has expired")
    return row


def acknowledge_control(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    control_id: int,
    now: datetime | None = None,
) -> dict:
    """Record the bound agent's receipt claim.

    Acknowledgement proves the request was READ, nothing more. Re-acknowledging
    is a silent no-op returning current state (the request_kill precedent): a
    repeated receipt is not a lifecycle moment."""
    agent_id, token_id = _agent_credentials(actor)
    with db.transaction(conn, immediate=True):
        _recheck_credentials(conn, agent_id=agent_id, token_id=token_id)
        row = _settlement_target(
            conn, agent_id=agent_id, control_id=control_id, now=now
        )
        if row["acknowledged_at"] is not None:
            return run_controls.projection(row, now=now)
        changed = run_controls.cas_acknowledge(
            conn, control_id=control_id, now_stamp=run_controls.stamp(now)
        )
        if not changed:
            # The CAS predicate re-checked settlement and expiry under the writer
            # lock; losing here means the state moved between read and write.
            raise RunControlCommandError(
                "conflict", "control was settled or expired concurrently"
            )
        event = activity.record(
            conn,
            actor_id=agent_id,
            verb=VERB_ACKNOWLEDGED,
            target_kind=TARGET_KIND,
            target_id=control_id,
            detail=f"{row['kind']} for run {row['run_id']}",
            commit=False,
        )
        run_controls.set_acknowledged_event(
            conn, control_id=control_id, event_id=event["id"]
        )
        updated = run_controls.get_control(conn, control_id)
        assert updated is not None
        return run_controls.projection(updated, now=now)


def _settle(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    control_id: int,
    settlement: str,
    verb: str,
    result_summary: str,
    result_payload: str | None,
    now: datetime | None,
) -> dict:
    agent_id, token_id = _agent_credentials(actor)
    with db.transaction(conn, immediate=True):
        _recheck_credentials(conn, agent_id=agent_id, token_id=token_id)
        row = _settlement_target(
            conn, agent_id=agent_id, control_id=control_id, now=now
        )
        changed = run_controls.cas_settle(
            conn,
            control_id=control_id,
            settled_by=agent_id,
            settlement=settlement,
            result_summary=result_summary,
            result_payload=result_payload,
            now_stamp=run_controls.stamp(now),
        )
        if not changed:
            raise RunControlCommandError(
                "conflict", "control was settled or expired concurrently"
            )
        event = activity.record(
            conn,
            actor_id=agent_id,
            verb=verb,
            target_kind=TARGET_KIND,
            target_id=control_id,
            detail=f"{row['kind']} for run {row['run_id']}",
            commit=False,
        )
        run_controls.set_settled_event(
            conn, control_id=control_id, event_id=event["id"]
        )
        updated = run_controls.get_control(conn, control_id)
        assert updated is not None
        return run_controls.projection(updated, now=now)


def decline_control(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    control_id: int,
    reason: object,
    now: datetime | None = None,
) -> dict:
    """Record the bound agent's refusal, with the reason the operator will read.

    Declining is an answer, not an error: the agent read the request and says it
    will not comply. The reason is required — a silent refusal tells the
    operator nothing they can steer by."""
    normalized_reason = _bounded_text(
        reason, field="reason", maximum=MAX_RESULT_SUMMARY_CHARS, required=True
    )
    return _settle(
        conn,
        actor=actor,
        control_id=control_id,
        settlement=run_controls.SETTLEMENT_DECLINED,
        verb=VERB_DECLINED,
        result_summary=normalized_reason,
        result_payload=None,
        now=now,
    )


def complete_control(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    control_id: int,
    summary: object = None,
    handoff: object = None,
    now: datetime | None = None,
) -> dict:
    """Record the bound agent's completion claim.

    Completion is an identity-bound CLAIM that the agent did what was asked — it
    is never proof of an operating-system effect, and Athena stores it as
    exactly that. ``steer`` and ``request_cancel`` complete with a bounded
    summary; ``request_fresh_context`` completes with the structured handoff
    (and nothing else), so what continues into a fresh context is enumerable."""
    agent_id, token_id = _agent_credentials(actor)
    with db.transaction(conn, immediate=True):
        _recheck_credentials(conn, agent_id=agent_id, token_id=token_id)
        row = _settlement_target(
            conn, agent_id=agent_id, control_id=control_id, now=now
        )
        if row["kind"] == run_controls.KIND_REQUEST_FRESH_CONTEXT:
            if handoff is None:
                raise RunControlCommandError(
                    "invalid",
                    "completing a request_fresh_context control requires a handoff",
                )
            if summary is not None:
                raise RunControlCommandError(
                    "invalid",
                    "a fresh-context completion carries its summary inside the handoff",
                )
            result_summary, result_payload = _validated_handoff(handoff)
        else:
            if handoff is not None:
                raise RunControlCommandError(
                    "invalid",
                    "only request_fresh_context completions carry a handoff",
                )
            result_summary = _bounded_text(
                summary,
                field="summary",
                maximum=MAX_RESULT_SUMMARY_CHARS,
                required=True,
            )
            result_payload = None
        changed = run_controls.cas_settle(
            conn,
            control_id=control_id,
            settled_by=agent_id,
            settlement=run_controls.SETTLEMENT_COMPLETED,
            result_summary=result_summary,
            result_payload=result_payload,
            now_stamp=run_controls.stamp(now),
        )
        if not changed:
            raise RunControlCommandError(
                "conflict", "control was settled or expired concurrently"
            )
        event = activity.record(
            conn,
            actor_id=agent_id,
            verb=VERB_COMPLETED,
            target_kind=TARGET_KIND,
            target_id=control_id,
            detail=f"{row['kind']} for run {row['run_id']}",
            commit=False,
        )
        run_controls.set_settled_event(
            conn, control_id=control_id, event_id=event["id"]
        )
        updated = run_controls.get_control(conn, control_id)
        assert updated is not None
        return run_controls.projection(updated, now=now)


def visible_control(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    control_id: int,
    now: datetime | None = None,
) -> dict:
    """One control, for an admin or for the agent it is addressed to.

    The bound agent must be able to read what it is asked to do — privately.
    Everyone else gets the same 404 a missing control gives, so control traffic
    on a run never becomes an existence oracle."""
    row = run_controls.get_control(conn, control_id)
    if row is None:
        raise RunControlCommandError("not_found", "no such run control")
    if identity.is_admin(actor) or row["agent_id"] == actor.get("id"):
        return run_controls.projection(row, now=now)
    raise RunControlCommandError("not_found", "no such run control")


def readable_controls(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    run_id: str | None = None,
    state: str | None = None,
    limit: int = run_controls.DEFAULT_LIST_LIMIT,
    now: datetime | None = None,
) -> list[dict]:
    """Every control for an admin; an agent's own inbox for anyone else.

    A non-admin, non-agent caller sees an empty list — same shape, no oracle."""
    if state is not None and state not in (
        *run_controls.CONTROL_STATES,
        run_controls.STATE_FILTER_OPEN,
    ):
        raise RunControlCommandError(
            "invalid",
            "state must be one of: "
            + ", ".join((*run_controls.CONTROL_STATES, run_controls.STATE_FILTER_OPEN)),
        )
    if run_id is not None:
        try:
            run_id = run_context.strict_run_id(run_id)
        except ValueError as exc:
            raise RunControlCommandError("invalid", str(exc)) from exc
    try:
        bounded = min(max(int(limit), 1), run_controls.MAX_LIST_LIMIT)
    except (TypeError, ValueError) as exc:
        raise RunControlCommandError("invalid", "limit must be an integer") from exc
    now_stamp = run_controls.stamp(now)
    if identity.is_admin(actor):
        rows = run_controls.list_rows(
            conn, run_id=run_id, state=state, limit=bounded, now_stamp=now_stamp
        )
    else:
        own = actor.get("id")
        if (
            not isinstance(own, int)
            or isinstance(own, bool)
            or not actor.get("is_agent")
        ):
            return []
        rows = run_controls.list_rows(
            conn,
            run_id=run_id,
            agent_id=own,
            state=state,
            limit=bounded,
            now_stamp=now_stamp,
        )
    return [run_controls.projection(row, now=now) for row in rows]
