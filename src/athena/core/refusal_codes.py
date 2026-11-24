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
            "only an operator can issue a wider token, so no call of yours "
            "resolves it (my_desk's identity.scopes lists what this one may do)."
        ),
        # No `next`: my_desk only restated what this token may do; no call made
        # with this token makes progress or waits for a change (F11).
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
        code="run_id_mismatch",
        statuses=(422,),
        meaning=(
            "The run_id you sent is not the run this session is tagged with "
            "(the detail names both); omit run_id to use the session's run."
        ),
        next=NextCall(
            tool="current_run",
            hint="omit run_id, or pass the run_id it returns",
        ),
    ),
    RefusalCode(
        code="run_header_invalid",
        statuses=(422,),
        meaning=(
            "The X-Athena-Run header was sent but would not be recorded as sent: "
            "a server-reserved namespace, over 200 characters, or not a printable "
            "run id (the detail says which). Fix the header; it is never read as "
            "absent."
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
        meaning=(
            "The request body or upload exceeds the server's size limit; "
            "`limit_bytes` names it."
        ),
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
        meaning=(
            "Too many requests from this token or address; `rate_limit` carries "
            "the limit, what remains and retry_after_seconds to wait."
        ),
    ),
    RefusalCode(
        code="capacity_reached",
        statuses=(409, 429),
        meaning=(
            "A bounded server-side limit is reached (run check-ins, workers, "
            "run controls, playbook steps); the detail says which and how to "
            "stay within it, and `limit` names the bound where it is one."
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
            "Another account holds this issue's active lease; `holder` and "
            "`expires_at` name it and when it ends, and `events_after` is the "
            "newest event on the issue you can see. Do not work the issue."
        ),
        # Not claim_next_ready: for a seat whose one delegation is the held
        # issue, pickup answers nothing_ready and names neither the issue nor
        # when it frees (FLEET-15). Not get_issue_lease: it restates the
        # holder and expiry this refusal carries. The wait parks until the
        # holder acts on the issue (tests/test_refusal_next.py).
        next=NextCall(
            tool="wait_for_events",
            hint=(
                "kind=issue, target=the issue, after=events_after: answers when "
                "the holder renews, yields, completes or declines; then claim "
                "again (also after expires_at). Other work: claim_next_ready"
            ),
        ),
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
            "The call needs the lease's current generation (a repeated claim "
            "by its holder is a renewal). When you hold the lease, `generation` "
            "names it: repeat with it. An admin releasing another account's "
            "lease is not told it; get_issue_lease shows it."
        ),
        # No `next`: the holder's refusal carries the generation, so a lease
        # read would only restate it (F11).
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
        code="fence_overlap",
        statuses=(409,),
        meaning=(
            "A path you declared overlaps another active lease's fence; `fence` "
            "names your path and the basis, and the other issue, holder, path "
            "and newest event (`events_after`) when you can read that lease "
            "(else null)."
        ),
        # Not claim_next_ready: with the same paths it restates this fence.
        next=NextCall(
            tool="wait_for_events",
            hint=(
                "kind=issue, target=fence.issue.id, after=fence.events_after: "
                "answers when that holder acts there; then claim again. With "
                "fence.issue null, declare paths that do not overlap instead"
            ),
        ),
    ),
    RefusalCode(
        code="fence_unresolved",
        statuses=(409,),
        meaning=(
            "Your fence cannot be placed: the instance's root catalog does not "
            "know your coordination_root, or one of two fences has no current "
            "root. Without a catalog omit coordination_root; with one, obtain a "
            "current reviewed selector from the operator."
        ),
    ),
    RefusalCode(
        code="blocked_issue_close_policy",
        statuses=(409,),
        meaning=(
            "The project refuses to close an issue that has open blockers; "
            "close or unlink the blockers first (get_issue_work_context's "
            "dependencies.open_blockers lists them)."
        ),
        # No `next`: reading the blockers neither closes the issue nor waits
        # for them to close, and several blockers have no one event (F11).
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
            "decision is pending (the body carries the approval, and "
            "`events_after`, the newest event on its target you can see)."
        ),
        next=NextCall(
            tool="wait_for_events",
            hint=(
                "kind=approval.target_kind, target=approval.target_id, "
                "after=events_after: answers when the operator decides; then "
                "repeat the call with the same idempotency_key"
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
            "What you may do changed after this key was claimed (the assignee, "
            "creator or a contributor of the issue it named, `issue_id`; or a "
            "role, token, membership, visibility, pause, placement, agent flag, "
            "close policy, approval gate or budget limit anywhere), so its stored "
            "response is not replayed and this answer does not say whether the "
            "write was applied. Read the target; repeat under a new key only if "
            "it was not. The key stays refused until its receipt would have "
            "expired."
        ),
        # The read that shows whether the write landed. Honest for a keyed write
        # on an issue (every per-issue fence, the common case); a write on no
        # issue reads its own target instead, which the hint says (F6).
        next=NextCall(
            tool="get_issue_work_context",
            hint=(
                "issue_id: the refusal's issue_id (the issue your keyed write "
                "named); its activity and comments show whether your write was "
                "applied. Repeat it with a new idempotency_key only if it was "
                "not. For a write on no issue, read what it changed instead."
            ),
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
