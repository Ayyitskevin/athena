"""The registry of refusal codes: the one owner of every ``code`` Athena answers.

Every 4xx and 5xx JSON body from a REST route carries a top-level ``code`` from
this registry, beside the unchanged ``detail``. An agent branches on the code,
never on the prose. MCP returns the same object, adding ``next`` where an entry
names the tool call that resolves the refusal.

Shared codes cover the common answers (``not_found``, ``forbidden``,
``validation_error`` ...). A code of its own exists only where an agent would act
differently: a held claim, a stale ETag, a lease generation, a cursor ahead,
approval, budget, and the idempotency states.

A few failures never reach Athena: bad tool arguments, or a check the MCP client
makes itself. They have codes too, so every failed tool call is one object with a
``code``; they are ``McpRefusalCode`` entries, which carry no HTTP status because
no HTTP answer exists. A REST route may not answer with one.

Standard library only, so ``athena.mcp`` can import it without the web stack.
``scripts/check_refusal_codes.py`` reads this file to refuse an unregistered
code anywhere in the source, and ``scripts/generate_agent_api.py`` renders it
into the reference table in ``docs/AGENT_API.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class NextCall:
    """The MCP tool call that resolves a refusal, with a hint about arguments."""

    tool: str
    hint: str

    def public(self) -> dict[str, str]:
        return {"tool": self.tool, "hint": self.hint}


@dataclass(frozen=True, slots=True)
class RefusalCode:
    """One registered code: its HTTP status(es) and what it means to an agent."""

    code: str
    statuses: tuple[int, ...]
    meaning: str
    next: NextCall | None = None


@dataclass(frozen=True, slots=True)
class McpRefusalCode:
    """A failure the MCP layer answers itself, before or instead of an HTTP
    answer: it has no status, by construction."""

    code: str
    meaning: str
    next: NextCall | None = None


_ENTRIES: tuple[RefusalCode, ...] = (
    # --- shared codes -------------------------------------------------------
    RefusalCode(
        code="bad_request",
        statuses=(400,),
        meaning="The request is malformed outside its body schema; fix it and resend.",
    ),
    RefusalCode(
        code="unauthenticated",
        statuses=(401,),
        meaning=(
            "No valid credential: the token or actor is missing, unknown or "
            "revoked. Retrying with the same credential fails the same way."
        ),
    ),
    RefusalCode(
        code="forbidden",
        statuses=(403,),
        meaning=(
            "Authenticated, but your role, ownership or the target's access "
            "rules do not allow this action."
        ),
    ),
    RefusalCode(
        code="scope_denied",
        statuses=(403,),
        meaning=(
            "Your token lacks the scope this call needs (the detail names it); "
            "only an operator can issue a wider token."
        ),
        next=NextCall(
            tool="my_desk",
            hint="identity.scopes lists what this token may do; ask the operator",
        ),
    ),
    RefusalCode(
        code="account_paused",
        statuses=(403,),
        meaning=(
            "The operator paused or removed this account; every authenticated "
            "action is refused until it is resumed. Stop and report."
        ),
    ),
    RefusalCode(
        code="run_binding_conflict",
        statuses=(403,),
        meaning=(
            "The run id on this write belongs to another actor's run; tag the "
            "write with a run of your own."
        ),
    ),
    RefusalCode(
        code="not_found",
        statuses=(404,),
        meaning=(
            "The target does not exist or is not visible to you; the two are "
            "deliberately indistinguishable."
        ),
    ),
    RefusalCode(
        code="method_not_allowed",
        statuses=(405,),
        meaning="The path exists but does not accept this HTTP method.",
    ),
    RefusalCode(
        code="conflict",
        statuses=(409,),
        meaning=(
            "The target's current state does not allow this action; read it "
            "again before deciding what to do."
        ),
    ),
    RefusalCode(
        code="payload_too_large",
        statuses=(413,),
        meaning="The request body or upload exceeds the server's size limit.",
    ),
    RefusalCode(
        code="validation_error",
        statuses=(422,),
        meaning=(
            "The input failed validation; the detail says which part. Resending "
            "it unchanged fails the same way."
        ),
    ),
    RefusalCode(
        code="rate_limited",
        statuses=(429,),
        meaning="Too many requests from this token or address; wait Retry-After seconds.",
    ),
    RefusalCode(
        code="capacity_reached",
        statuses=(409, 429),
        meaning=(
            "A bounded server-side limit is reached (run check-ins, workers, "
            "run controls, playbook steps); the detail says which and how to "
            "stay within it."
        ),
    ),
    RefusalCode(
        code="internal_error",
        statuses=(500,),
        meaning=(
            "The server failed while handling the request; whether a write "
            "applied is unknown. Read the target's state before retrying."
        ),
    ),
    RefusalCode(
        code="unavailable",
        statuses=(503,),
        meaning=(
            "A dependency (the database, the idempotency store or an execution "
            "fleet) cannot answer now; retry later."
        ),
    ),
    RefusalCode(
        code="deployment_boundary",
        statuses=(421, 503),
        meaning=(
            "Refused before any Athena work: the Host or socket is not one this "
            "deployment serves. Check the base URL you were given."
        ),
    ),
    # --- conditional requests -----------------------------------------------
    RefusalCode(
        code="precondition_required",
        statuses=(428,),
        meaning="This write needs If-Match with the target's current ETag.",
        next=NextCall(
            tool="get_issue",
            hint="pass its _etag as if_match (a page's _etag is on get_page)",
        ),
    ),
    RefusalCode(
        code="invalid_if_match",
        statuses=(400,),
        meaning="The If-Match value is not a valid entity tag; send one strong ETag.",
    ),
    RefusalCode(
        code="if_match_too_large",
        statuses=(431,),
        meaning="The If-Match header exceeds the parsing limit; send one ETag.",
    ),
    RefusalCode(
        code="precondition_failed",
        statuses=(412,),
        meaning=(
            "The target changed since the ETag you sent: read it again, merge "
            "your change into the current state, then retry."
        ),
        next=NextCall(
            tool="get_issue",
            hint=(
                "fresh _etag (a page's is on get_page); retry with it and a new "
                "idempotency_key"
            ),
        ),
    ),
    # --- leases and claims --------------------------------------------------
    RefusalCode(
        code="claim_held",
        statuses=(409,),
        meaning=(
            "Another account holds this issue's active lease (the detail names "
            "the holder); do not work it."
        ),
        next=NextCall(tool="claim_next_ready", hint="take other ready work instead"),
    ),
    RefusalCode(
        code="claim_required",
        statuses=(409,),
        meaning=(
            "This action needs your own active lease on the issue, and there is "
            "none: it lapsed, was released, or was never taken."
        ),
        next=NextCall(
            tool="get_issue_lease",
            hint="issue_id; if it is free, claim_issue takes it again",
        ),
    ),
    RefusalCode(
        code="lease_generation_required",
        statuses=(428,),
        meaning=(
            "You already hold this lease; a repeated claim is a renewal and "
            "needs your current generation (the detail names it)."
        ),
        next=NextCall(tool="get_issue_lease", hint="issue_id; pass its generation"),
    ),
    RefusalCode(
        code="invalid_lease_generation",
        statuses=(422,),
        meaning="The generation is not a 32-character lowercase hex token.",
        next=NextCall(tool="get_issue_lease", hint="issue_id; pass its generation"),
    ),
    RefusalCode(
        code="lease_generation_mismatch",
        statuses=(409,),
        meaning=(
            "The generation you sent is not the lease's current one: your "
            "possession lapsed or was replaced."
        ),
        next=NextCall(
            tool="get_issue_lease",
            hint="issue_id; if the lease is no longer yours, stop work on it",
        ),
    ),
    RefusalCode(
        code="blocked_issue_close_policy",
        statuses=(409,),
        meaning=(
            "The project refuses to close an issue that has open blockers; "
            "close or unlink the blockers first."
        ),
        next=NextCall(
            tool="get_issue_work_context",
            hint="dependencies.open_blockers lists them",
        ),
    ),
    # --- desk ----------------------------------------------------------------
    RefusalCode(
        code="cursor_ahead",
        statuses=(422,),
        meaning=(
            "after_id is past the newest event you can see; the body carries "
            "latest_visible_event_id, the highest value you may pass."
        ),
        next=NextCall(
            tool="advance_desk_cursor",
            hint="after_id no higher than latest_visible_event_id",
        ),
    ),
    RefusalCode(
        code="invalid_query",
        statuses=(422,),
        meaning="The work query does not parse; detail.error says where.",
    ),
    # --- approvals and budgets ----------------------------------------------
    RefusalCode(
        code="approval_required",
        statuses=(202,),
        meaning=(
            "The action is gated for you: it was NOT applied, and an operator "
            "decision is pending (the body carries the approval)."
        ),
        next=NextCall(
            tool="wait_for_events",
            hint=(
                "kind=approval.target_kind, target=approval.target_id; once "
                "decided, repeat the call with the same idempotency_key"
            ),
        ),
    ),
    RefusalCode(
        code="approval_rejected",
        statuses=(409,),
        meaning=(
            "The operator rejected this gated action (the note is in the body); "
            "do not retry it."
        ),
    ),
    RefusalCode(
        code="agent_budget_exhausted",
        statuses=(429,),
        meaning=(
            "Your account's write budget for this window is spent; nothing was "
            "applied. Wait Retry-After seconds."
        ),
    ),
    # --- idempotency ----------------------------------------------------------
    RefusalCode(
        code="idempotency_key_invalid",
        statuses=(400,),
        meaning="Send exactly one Idempotency-Key of 1-255 visible ASCII characters.",
    ),
    RefusalCode(
        code="idempotency_key_unsupported",
        statuses=(400,),
        meaning="This endpoint does not take an Idempotency-Key; resend without one.",
    ),
    RefusalCode(
        code="idempotency_mismatch",
        statuses=(409,),
        meaning=(
            "This Idempotency-Key was already used for a different request; "
            "use a new key for a different call."
        ),
    ),
    RefusalCode(
        code="idempotency_in_progress",
        statuses=(409,),
        meaning=(
            "A request with this Idempotency-Key is still running; retry the "
            "same call after Retry-After seconds."
        ),
    ),
    RefusalCode(
        code="idempotency_indeterminate",
        statuses=(409,),
        meaning=(
            "The earlier request under this key ended with an unknown outcome; "
            "read the target's state before choosing a new key."
        ),
    ),
    RefusalCode(
        code="idempotency_authorization_changed",
        statuses=(409,),
        meaning=(
            "Your authorization changed after this key was claimed, so its "
            "stored response cannot be replayed."
        ),
    ),
    # --- undo -------------------------------------------------------------------
    RefusalCode(
        code="undo_event_not_found",
        statuses=(404,),
        meaning="No activity event with that id is visible to you.",
    ),
    RefusalCode(
        code="undo_not_reversible",
        statuses=(401, 422),
        meaning="This event's action has no inverse that undo may apply.",
    ),
    RefusalCode(
        code="undo_already_reversed",
        statuses=(409,),
        meaning="This event was already reversed; the detail names the reversal.",
    ),
    RefusalCode(
        code="undo_imported_event",
        statuses=(422,),
        meaning="An imported event records another system's history; it cannot be undone.",
    ),
    RefusalCode(
        code="undo_no_effect",
        statuses=(409,),
        meaning="The action's effect is no longer in force, so there is nothing to undo.",
    ),
    RefusalCode(
        code="undo_refused_by_command",
        statuses=(400, 401, 403, 404, 409, 412, 422, 428, 431),
        meaning=(
            "Undo is possible, but the inverse command refused it for you or "
            "for now; the status and detail are that command's."
        ),
    ),
)


_MCP_ENTRIES: tuple[McpRefusalCode, ...] = (
    McpRefusalCode(
        code="invalid_arguments",
        meaning=(
            "The tool's arguments failed its own checks, so no request was sent; "
            "the detail names each argument and the rule it broke."
        ),
    ),
    McpRefusalCode(
        code="unknown_tool",
        meaning="No tool of that name is registered in this session's profile.",
    ),
    McpRefusalCode(
        code="coordination_root_unconfirmed",
        meaning=(
            "The claim's answer did not confirm the coordination root you sent; "
            "the claim may already exist. Read the lease before acting."
        ),
        next=NextCall(
            tool="get_issue_lease",
            hint="issue_id; do not retry the claim without its root",
        ),
    ),
    McpRefusalCode(
        code="athena_no_response",
        meaning=(
            "Athena did not answer (refused, reset or timed out); whether a write "
            "applied is unknown. Read the target's state before retrying."
        ),
    ),
    McpRefusalCode(
        code="tool_failed",
        meaning=(
            "The tool failed inside the MCP server after any request it made; "
            "the detail says how. Read the target's state before retrying."
        ),
    ),
)


def _index[Entry: (RefusalCode, McpRefusalCode)](
    entries: tuple[Entry, ...],
) -> Mapping[str, Entry]:
    by_code: dict[str, Entry] = {}
    for entry in entries:
        if entry.code in by_code:
            raise ValueError(f"refusal code registered twice: {entry.code}")
        by_code[entry.code] = entry
    return MappingProxyType(by_code)


#: Every registered code a REST route may answer with, by name.
REFUSAL_CODES: Mapping[str, RefusalCode] = _index(_ENTRIES)

#: The codes only the MCP layer answers with: no HTTP status exists for them.
MCP_REFUSAL_CODES: Mapping[str, McpRefusalCode] = _index(_MCP_ENTRIES)

#: The code an error that names none answers with: FastAPI's own 404 and 405,
#: and any framework refusal raised outside Athena's code. Athena's own call
#: sites always name their code (``scripts/check_refusal_codes.py``).
DEFAULT_CODE_BY_STATUS: Mapping[int, str] = MappingProxyType(
    {
        400: "bad_request",
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        412: "precondition_failed",
        413: "payload_too_large",
        422: "validation_error",
        428: "precondition_required",
        429: "rate_limited",
        431: "if_match_too_large",
        500: "internal_error",
        503: "unavailable",
    }
)


def code_for_status(status_code: int) -> str:
    """The shared code for a status, for a refusal that names none."""
    code = DEFAULT_CODE_BY_STATUS.get(status_code)
    if code is not None:
        return code
    return "internal_error" if status_code >= 500 else "bad_request"


def next_call(code: str | None) -> dict[str, str] | None:
    """The resolving MCP call for a code, or None when the registry names none."""
    if code is None:
        return None
    entry = REFUSAL_CODES.get(code) or MCP_REFUSAL_CODES.get(code)
    if entry is None or entry.next is None:
        return None
    return entry.next.public()
