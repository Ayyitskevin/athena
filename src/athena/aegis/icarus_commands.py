"""Handing an Aegis work item to an external executor, and applying its reports.

The outbound command owns authorization, the budget charge, the approval check,
the policy digest, the dispatch record, and the `dispatch_requested` event — all
in one transaction, **before** anything leaves the process. The inbound callback
command owns evidence and terminal transitions together with their activity,
after the route has authenticated the exact raw body.

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

import hmac
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
MAX_POLICY_DIGEST_CHARS = 200


class IcarusCommandError(Exception):
    """Transport-neutral command refusal; each adapter owns status mapping."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class _DuplicateIcarusRunId(Exception):
    """The executor claimed a correlation id already owned by another dispatch."""


def _validated_text(value: object, *, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise IcarusCommandError("invalid", f"{name} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise IcarusCommandError("invalid", f"{name} must be 1-{maximum} characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise IcarusCommandError(
            "invalid", f"{name} must not contain control characters"
        )
    return text


def _validated_callback_text(
    value: object,
    *,
    name: str,
    maximum: int,
    optional: bool = False,
) -> str | None:
    """Validate opaque callback text before it reaches SQLite or the audit log."""
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise IcarusCommandError("invalid", f"{name} must be a string")
    if not value or not value.strip() or len(value) > maximum:
        raise IcarusCommandError("invalid", f"{name} must be 1-{maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise IcarusCommandError(
            "invalid", f"{name} must be valid Unicode text"
        ) from exc
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise IcarusCommandError(
            "invalid", f"{name} must not contain control characters"
        )
    return value


def _policy_digest_matches(reported: str, expected: str) -> bool:
    """Constant-time equality for the ASCII digest contract.

    A valid non-ASCII string is evidence of a mismatch, not a process error. The
    callback command still records and flags that report.
    """
    try:
        reported_bytes = reported.encode("ascii")
        expected_bytes = expected.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(reported_bytes, expected_bytes)


def _reported_digest_label(reported: str) -> str:
    """A bounded, SQLite-safe audit label for an untrusted reported digest."""
    try:
        reported.encode("ascii")
    except UnicodeEncodeError:
        return "<malformed>"
    return reported[:16]


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
        raise IcarusCommandError("unauthorized", "authentication required")
    if not identity.can_write(actor):
        raise IcarusCommandError("forbidden", "viewer role is read-only")
    if not identity.token_has_scope(actor, tokens.ISSUE_WRITE_SCOPE):
        raise IcarusCommandError(
            "forbidden", f"token scope required: {tokens.ISSUE_WRITE_SCOPE}"
        )
    if not config.icarus_configured():
        raise IcarusCommandError(
            "unavailable",
            "no execution fleet is configured "
            "(set ATHENA_ICARUS_URL and ATHENA_ICARUS_SECRET)",
        )
    validated_repo = _validated_text(repo, name="repo", maximum=MAX_REPO_CHARS)
    validated_commit = _validated_text(
        base_commit, name="base_commit", maximum=MAX_COMMIT_CHARS
    )
    if capability not in CAPABILITIES:
        raise IcarusCommandError(
            "invalid", f"capability must be one of: {', '.join(sorted(CAPABILITIES))}"
        )

    with db.transaction(conn, immediate=True):
        # Visibility is mutable policy: project membership and project visibility
        # may change concurrently. Read it only after BEGIN IMMEDIATE has reserved
        # SQLite's writer slot, so a revocation cannot land between authorization
        # and the dispatch/audit record.
        if not access.can_see_issue(conn, actor, work_item_id):
            raise IcarusCommandError("not_found", "no such issue")
        issue = issues.get_issue(conn, work_item_id)
        if issue is None:
            raise IcarusCommandError("not_found", "no such issue")
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


def apply_callback(
    conn: sqlite3.Connection,
    *,
    icarus_run_id: object,
    policy_digest: object,
    evidence_ref: object | None = None,
    completion_ref: object | None = None,
    outcome: object | None = None,
) -> dict:
    """Apply one authenticated executor report atomically.

    Callback v1 has one canonical evidence pointer and no sender sequence. The
    first non-null pointer therefore wins; an exact retry is a no-op and a
    different pointer conflicts while the dispatch is open. A terminal dispatch
    absorbs later outcome changes and every evidence overwrite, while allowing a
    delayed progress callback to fill an evidence pointer that is still NULL.
    Digest mismatches remain one-per-run audit facts even after terminal state.
    These rules make retry and reordering deterministic without inventing recency
    Athena cannot prove.
    """
    validated_run_id = _validated_callback_text(
        icarus_run_id,
        name="icarus_run_id",
        maximum=dispatch.MAX_REF_CHARS,
    )
    validated_digest = _validated_callback_text(
        policy_digest,
        name="policy_digest",
        maximum=MAX_POLICY_DIGEST_CHARS,
    )
    validated_evidence = _validated_callback_text(
        evidence_ref,
        name="evidence_ref",
        maximum=dispatch.MAX_REF_CHARS,
        optional=True,
    )
    validated_completion = _validated_callback_text(
        completion_ref,
        name="completion_ref",
        maximum=dispatch.MAX_REF_CHARS,
        optional=True,
    )
    if outcome not in (None, dispatch.COMPLETED, dispatch.FAILED):
        raise IcarusCommandError("invalid", "outcome must be one of: completed, failed")
    if validated_completion is not None and outcome is None:
        raise IcarusCommandError(
            "invalid", "completion_ref requires a terminal outcome"
        )

    assert isinstance(validated_run_id, str)
    assert isinstance(validated_digest, str)
    with db.transaction(conn, immediate=True):
        # Lookup belongs inside the write reservation. The evidence and terminal
        # decisions must see the row as it is under the lock, not a stale snapshot.
        record = dispatch.get_by_icarus_run(conn, validated_run_id)
        if record is None:
            raise IcarusCommandError("not_found", "no such dispatch")

        digest_matches = _policy_digest_matches(
            validated_digest, record["policy_digest"]
        )
        result = {
            "dispatch_id": record["id"],
            "policy_digest_matches": digest_matches,
        }
        is_terminal = record["state"] in dispatch.TERMINAL_STATES
        existing_evidence = record["evidence_ref"]
        conflicting_evidence = (
            not is_terminal
            and validated_evidence is not None
            and existing_evidence is not None
            and validated_evidence != existing_evidence
        )

        token = run_context.set_run_id(record["run_id"])
        try:
            if not digest_matches and not dispatch.digest_mismatch_recorded(
                conn, record["run_id"]
            ):
                activity.record(
                    conn,
                    actor_id=record["dispatched_by"],
                    verb=dispatch.VERB_DIGEST_MISMATCH,
                    target_kind="issue",
                    target_id=record["work_item_id"],
                    detail=(
                        f"dispatched {record['policy_digest'][:16]}…, "
                        f"reported {_reported_digest_label(validated_digest)}…"
                    ),
                    commit=False,
                )
            # A conflicting pointer is inadmissible, but an authenticated wrong
            # policy digest is still an audit fact. Record that warning above,
            # commit it, and return the evidence conflict only after leaving the
            # transaction. Never apply any other part of a conflicting report.
            if not conflicting_evidence:
                # A terminal outcome is absorbing, but a legacy terminal callback
                # may have omitted evidence_ref. Its delayed progress twin may fill
                # that one NULL exactly once; no existing pointer is overwritten.
                if validated_evidence is not None and existing_evidence is None:
                    landed = dispatch.record_evidence(
                        conn,
                        dispatch_id=record["id"],
                        evidence_ref=validated_evidence,
                    )
                    if landed:
                        activity.record(
                            conn,
                            actor_id=record["dispatched_by"],
                            verb=dispatch.VERB_EVIDENCE,
                            target_kind="issue",
                            target_id=record["work_item_id"],
                            detail=validated_evidence,
                            commit=False,
                        )
                if outcome is not None and not is_terminal:
                    landed = dispatch.record_terminal(
                        conn,
                        dispatch_id=record["id"],
                        state=outcome,
                        completion_ref=validated_completion,
                    )
                    if landed:
                        activity.record(
                            conn,
                            actor_id=record["dispatched_by"],
                            verb=dispatch.VERB_TERMINAL,
                            target_kind="issue",
                            target_id=record["work_item_id"],
                            detail=(f"{outcome}: {validated_completion or 'no ref'}")[
                                : dispatch.MAX_REF_CHARS
                            ],
                            commit=False,
                        )
        finally:
            run_context.reset_run_id(token)
    if conflicting_evidence:
        raise IcarusCommandError(
            "conflict",
            "evidence_ref is immutable for this dispatch",
        )
    return result


def deliver_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: int,
    poster: webhooks.Poster | None = None,
) -> dict:
    """Hand the envelope over, after the record committed.

    Never call this inside a transaction. The result — accepted with the executor's
    run id, or undeliverable with a reason — is recorded in its own transaction,
    and either way the dispatch record survives.
    """
    record = dispatch.get_dispatch(conn, dispatch_id)
    if record is None:
        raise IcarusCommandError("not_found", "no such dispatch")
    if record["state"] != dispatch.PENDING_DELIVERY:
        return record

    url = f"{config.ICARUS_URL.rstrip('/')}/dispatch"
    safe, reason = webhooks.is_safe_url(url)
    if not safe:
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
        _record_delivery_failure(conn, record, detail or "delivery failed")
        return dispatch.get_dispatch(conn, dispatch_id) or record

    # The executor's run id is its claim about itself. Athena stores the exact
    # validated value so the executor can echo it back; transforming a correlation
    # key would create a dispatch no callback can find.
    try:
        icarus_run_id = _accepted_run_id(detail, fallback=record["idempotency_key"])
    except IcarusCommandError as exc:
        _record_delivery_failure(conn, record, exc.detail)
        return dispatch.get_dispatch(conn, dispatch_id) or record
    try:
        _record_delivery_acceptance(conn, record, icarus_run_id)
    except _DuplicateIcarusRunId:
        reason = "executor returned a duplicate icarus_run_id"
        _record_delivery_failure(conn, record, reason)
        return dispatch.get_dispatch(conn, dispatch_id) or record
    return dispatch.get_dispatch(conn, dispatch_id) or record


def _accepted_run_id(detail: str | None, *, fallback: str) -> str:
    """The executor's run id from its successful response, or a deterministic stand-in.

    A well-behaved executor returns `{"icarus_run_id": "..."}`. The value is
    preserved exactly: trimming or truncating it would make a natural echo miss
    Athena's lookup. A response with no usable field falls back to the idempotency
    key both sides already share; a present but invalid field is a protocol failure.
    """
    if detail:
        try:
            payload = json.loads(detail)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and "icarus_run_id" in payload:
            candidate = payload["icarus_run_id"]
            try:
                validated = _validated_callback_text(
                    candidate,
                    name="icarus_run_id",
                    maximum=dispatch.MAX_REF_CHARS,
                )
            except IcarusCommandError as exc:
                raise IcarusCommandError(
                    "unavailable", "executor returned an invalid icarus_run_id"
                ) from exc
            assert isinstance(validated, str)
            return validated
    return fallback


def _record_delivery_acceptance(
    conn: sqlite3.Connection, record: dict, icarus_run_id: str
) -> bool:
    """Land acceptance and its audit event as one first-writer-wins command."""
    with db.transaction(conn, immediate=True):
        try:
            landed = dispatch.mark_accepted(
                conn,
                dispatch_id=record["id"],
                icarus_run_id=icarus_run_id,
            )
        except sqlite3.IntegrityError as exc:
            # Keep this translation around the state statement only: an audit
            # insertion failure must roll the state back and propagate, not be
            # mislabeled as a duplicate executor id.
            raise _DuplicateIcarusRunId from exc
        if landed:
            activity.record(
                conn,
                actor_id=record["dispatched_by"],
                verb=dispatch.VERB_ACCEPTED,
                target_kind="issue",
                target_id=record["work_item_id"],
                detail=f"executor run {icarus_run_id}",
                commit=False,
            )
        return landed


def _record_delivery_failure(
    conn: sqlite3.Connection, record: dict, reason: str
) -> bool:
    """Land an undeliverable state and its audit event atomically."""
    with db.transaction(conn, immediate=True):
        landed = dispatch.mark_undeliverable(
            conn,
            dispatch_id=record["id"],
            reason=reason,
        )
        if landed:
            activity.record(
                conn,
                actor_id=record["dispatched_by"],
                verb=dispatch.VERB_UNDELIVERABLE,
                target_kind="issue",
                target_id=record["work_item_id"],
                detail=reason[: dispatch.MAX_REF_CHARS],
                commit=False,
            )
        return landed
