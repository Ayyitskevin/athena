"""Handing an Aegis work item to an external executor.

The command owns authorization, the budget charge, the approval check, the policy
digest, the dispatch record, and the `dispatch_requested` event — all in one
transaction, **before** anything leaves the process.

**The outbound call is a post-commit side effect, never part of the write.** Athena
holds SQLite's single writer while a transaction is open; making a network call
inside one would block every other writer for as long as a stranger's server feels
like taking. It is also wrong on its own terms: the durable fact is "Athena decided
to dispatch this", and that fact must survive whether or not the far side answers.
This is exactly how webhook delivery already works.

So delivery happens after the commit, and its outcome is recorded as a *follow-up*
event. A dispatch nobody could deliver stays visible as `undeliverable` with the
reason — an operator needs to see that Athena tried and failed, not to find nothing
at all.

Egress reuses `core/webhooks`' SSRF hardening in full: URL validation, DNS-pinned
connections, no redirects, and HMAC signing. A control plane that can be made to
POST anywhere is a control plane that can be turned into a probe.
"""

from __future__ import annotations

import json
import secrets
import sqlite3

from athena import config
from athena.aegis import issues
from athena.core import (
    access,
    activity,
    approvals,
    budgets,
    db,
    dispatch,
    identity,
    run_context,
    tokens,
    webhooks,
)

#: What an executor may be asked to do. A closed vocabulary, like every other
#: policy surface here: an open one would mean Athena forwarding capability names
#: it has never heard of and cannot reason about.
CAPABILITY_REPO_EDIT = "repo.edit"
CAPABILITY_CI_RUN = "ci.run"
CAPABILITIES: frozenset[str] = frozenset({CAPABILITY_REPO_EDIT, CAPABILITY_CI_RUN})

MAX_REPO_CHARS = 400
MAX_COMMIT_CHARS = 200


def _validated_text(value: object, *, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise dispatch.DispatchError("invalid", f"{name} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise dispatch.DispatchError(
            "invalid", f"{name} must be 1-{maximum} characters"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise dispatch.DispatchError(
            "invalid", f"{name} must not contain control characters"
        )
    return text


def _policy_facts(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    work_item_id: int,
    repo: str,
    base_commit: str,
    capability: str,
    approval_state: str,
) -> dispatch.PolicyFacts:
    budget = budgets.observed(conn, actor["id"])
    scopes = actor.get("_token_scopes")
    return dispatch.PolicyFacts(
        actor_id=int(actor["id"]),
        scopes=tuple(scopes) if scopes is not None else (),
        work_item_id=work_item_id,
        repo=repo,
        base_commit=base_commit,
        capability=capability,
        approval_state=approval_state,
        budget_window=None if budget is None else budget.window,
        budget_limit=None if budget is None else budget.action_limit,
    )


def request_dispatch(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    work_item_id: int,
    repo: object,
    base_commit: object,
    capability: object,
    idempotency_key: str | None = None,
) -> dict:
    """Record the decision to dispatch, atomically with its audit event.

    Returns the dispatch row. Delivery is a separate step
    (:func:`deliver_dispatch`) that the caller performs after this commits.

    Refuses when no executor is configured: half-working is worse than absent, and
    a deployment with no execution fleet should hear so plainly.
    """
    if actor is None:
        raise dispatch.DispatchError("unauthorized", "authentication required")
    if not identity.can_write(actor):
        raise dispatch.DispatchError("forbidden", "viewer role is read-only")
    if not identity.token_has_scope(actor, tokens.ISSUE_WRITE_SCOPE):
        raise dispatch.DispatchError(
            "forbidden", f"token scope required: {tokens.ISSUE_WRITE_SCOPE}"
        )
    if not config.icarus_configured():
        raise dispatch.DispatchError(
            "unavailable",
            "no execution fleet is configured "
            "(set ATHENA_ICARUS_URL and ATHENA_ICARUS_SECRET)",
        )
    validated_repo = _validated_text(repo, name="repo", maximum=MAX_REPO_CHARS)
    validated_commit = _validated_text(
        base_commit, name="base_commit", maximum=MAX_COMMIT_CHARS
    )
    if capability not in CAPABILITIES:
        raise dispatch.DispatchError(
            "invalid", f"capability must be one of: {', '.join(sorted(CAPABILITIES))}"
        )
    if not access.can_see_issue(conn, actor, work_item_id):
        raise dispatch.DispatchError("not_found", "no such issue")

    with db.transaction(conn, immediate=True):
        issue = issues.get_issue(conn, work_item_id)
        if issue is None:
            raise dispatch.DispatchError("not_found", "no such issue")
        key = idempotency_key or secrets.token_hex(16)
        existing = dispatch.get_by_idempotency_key(conn, key)
        if existing is not None:
            # Single-flight: the same intent asked twice is one dispatch. Returning
            # the existing row is the whole point of the key.
            return existing

        # Dispatching is a metered write like any other agent action, and it is
        # gated like one — under its OWN action kind. Dispatch originally borrowed
        # `issue.close`'s policy row, which conflated two intents: gating an
        # agent's closes silently gated its dispatches too, and — worse — an
        # approval the operator granted for CLOSING an issue could be spent by a
        # DISPATCH of that issue instead. An approval authorizes the intent the
        # operator read on the ask, so the kinds must never be shared. An operator
        # who wants both gated gates both; each is one policy row. The gate is
        # consumed inside this transaction, so a failure below leaves it unspent.
        budgets.charge(conn, actor)
        approval_state = "not_required"
        if approvals.is_gated(conn, actor["id"], approvals.ACTION_DISPATCH_REQUEST):
            approvals.require(
                conn,
                actor,
                action_kind=approvals.ACTION_DISPATCH_REQUEST,
                target_kind="issue",
                target_id=work_item_id,
            )
            approval_state = "approved"

        run_id = f"{dispatch.RUN_PREFIX}{key}"
        facts = _policy_facts(
            conn,
            actor=actor,
            work_item_id=work_item_id,
            repo=validated_repo,
            base_commit=validated_commit,
            capability=str(capability),
            approval_state=approval_state,
        )
        dispatch_id = dispatch.create_dispatch(
            conn,
            work_item_id=work_item_id,
            run_id=run_id,
            # The run the operator or agent was working under when they asked. The
            # dispatch's own run descends from it, so the execution shows up in the
            # lineage of the work that caused it.
            parent_run_id=run_context.get_run_id(),
            repo=validated_repo,
            base_commit=validated_commit,
            capability=str(capability),
            policy_digest=facts.digest(),
            approval_state=approval_state,
            idempotency_key=key,
            dispatched_by=int(actor["id"]),
        )
        activity.record(
            conn,
            actor_id=int(actor["id"]),
            verb=dispatch.VERB_REQUESTED,
            target_kind="issue",
            target_id=work_item_id,
            detail=f"{capability} on {validated_repo}@{validated_commit}",
            commit=False,
        )
        recorded = dispatch.get_dispatch(conn, dispatch_id)
        assert recorded is not None
        return recorded


def deliver_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: int,
    poster: webhooks.Poster | None = None,
) -> dict:
    """Hand the envelope over, after the record committed.

    Never call this inside a transaction. The result — accepted with the executor's
    run id, or undeliverable with a reason — is recorded on its own connection, and
    either way the dispatch record survives.
    """
    record = dispatch.get_dispatch(conn, dispatch_id)
    if record is None:
        raise dispatch.DispatchError("not_found", "no such dispatch")
    if record["state"] != dispatch.PENDING_DELIVERY:
        return record

    url = f"{config.ICARUS_URL.rstrip('/')}/dispatch"
    safe, reason = webhooks.is_safe_url(url)
    if not safe:
        dispatch.mark_undeliverable(conn, dispatch_id=dispatch_id, reason=reason)
        _record_delivery_failure(conn, record, reason)
        return dispatch.get_dispatch(conn, dispatch_id) or record

    body = json.dumps(
        dispatch.envelope(
            record, fork_runs=dispatch.fork_run_ids(conn, record["run_id"])
        ),
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Athena-Signature": webhooks.sign(config.ICARUS_SECRET, body),
        "Idempotency-Key": record["idempotency_key"],
    }
    send = poster or webhooks.urllib_poster(float(config.ICARUS_TIMEOUT_SECONDS))
    ok, detail = send(url, body, headers)
    if not ok:
        dispatch.mark_undeliverable(
            conn, dispatch_id=dispatch_id, reason=detail or "delivery failed"
        )
        _record_delivery_failure(conn, record, detail or "delivery failed")
        return dispatch.get_dispatch(conn, dispatch_id) or record

    # The executor's run id is its claim about itself. Athena stores it to correlate
    # callbacks — it does not verify that any work is happening, because it cannot.
    icarus_run_id = _accepted_run_id(detail, fallback=record["idempotency_key"])
    dispatch.mark_accepted(conn, dispatch_id=dispatch_id, icarus_run_id=icarus_run_id)
    activity.record(
        conn,
        actor_id=record["dispatched_by"],
        verb=dispatch.VERB_ACCEPTED,
        target_kind="issue",
        target_id=record["work_item_id"],
        detail=f"executor run {icarus_run_id}",
    )
    return dispatch.get_dispatch(conn, dispatch_id) or record


def _accepted_run_id(detail: str | None, *, fallback: str) -> str:
    """The executor's run id from its 202 body, or a deterministic stand-in.

    A well-behaved executor returns `{"icarus_run_id": "..."}`. One that returns
    nothing useful still gets correlated, by the idempotency key both sides already
    share — better than dropping the dispatch on the floor over a missing field."""
    if detail:
        try:
            payload = json.loads(detail)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            candidate = payload.get("icarus_run_id")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[: dispatch.MAX_REF_CHARS]
    return fallback


def _record_delivery_failure(
    conn: sqlite3.Connection, record: dict, reason: str
) -> None:
    activity.record(
        conn,
        actor_id=record["dispatched_by"],
        verb=dispatch.VERB_UNDELIVERABLE,
        target_kind="issue",
        target_id=record["work_item_id"],
        detail=reason[: dispatch.MAX_REF_CHARS],
    )
