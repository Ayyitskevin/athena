"""Application commands for the delegation claim/lease protocol — claim, renew,
yield, decline, and complete — so two agents cannot silently pull the same work.

An issue may be delegated to several agents (the contributor set). A lease is the
exclusive "I am actively working this now" token. Claim and renewal require the caller's
exact current root-issue revision, preventing acceptance of stale work. Completion
releases finished work, yield records a holder's honest non-completion reason, and
decline rejects the delegation outright. The lease remains distinct from the assignee
(accountable) and contributor set (eligible).

Each command owns one transaction: lease state and its audit event commit or roll back
together. Authorization is resolved here, reusing the issue command policy so REST and
MCP share one boundary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
import re
import secrets
import sqlite3
from typing import Literal

from athena import config
from athena.aegis import (
    accountable,
    claim_handoffs,
    contributors as contributors_data,
    issue_activity,
    issue_etags,
    issues,
    lease_lapse_commands,
    lease_progress,
    leases,
    report_credentials,
    review_commands,
    statuses,
)
from athena.aegis.issue_commands import (
    IssueCommandError,
    _check_issue_precondition,
    _require_issue_writer,
    _visible_issue,
)
from athena.core import (
    access,
    activity,
    budgets,
    coordination_roots,
    db,
    identity,
    notifications,
    packet_time,
    reported_usage,
)

ClaimYieldReason = Literal["needs_input", "blocked", "capacity"]
CLAIM_YIELD_REASONS = frozenset({"needs_input", "blocked", "capacity"})
# Refusal codes for the two lease conflicts an agent answers differently from a
# plain conflict: someone else holds the lease (move on), or you hold none (look,
# then claim). Kind stays "conflict"; the code travels on the error.
CLAIM_HELD_CODE = "claim_held"
CLAIM_REQUIRED_CODE = "claim_required"
# The two fence refusals (C6): a declared path overlaps another active lease's
# fence, or the two fences cannot be compared at all. Kind stays "conflict";
# the body's ``fence`` object says which path, on what basis, and against whom.
FENCE_OVERLAP_CODE = "fence_overlap"
FENCE_UNRESOLVED_CODE = "fence_unresolved"
MAX_CLAIM_YIELD_NOTE_CHARS = 500
MAX_HANDOFF_ATTEMPTED_WORK_CHARS = 4_000
MAX_HANDOFF_EVIDENCE_ITEMS = 10
MAX_HANDOFF_EVIDENCE_ITEM_CHARS = 1_000
MAX_HANDOFF_EVIDENCE_JSON_CHARS = 8_000
MAX_HANDOFF_BLOCKING_QUESTION_CHARS = 1_000
MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS = 4_000
MAX_HANDOFF_RESUME_NOTE_CHARS = 2_000
MAX_COMPLETION_RESULTS = 20
MAX_COMPLETION_TEXT_ITEMS = 10
MAX_COMPLETION_TEXT_CHARS = 500
MAX_COMPLETION_SOURCE_CHARS = 200
MAX_COMPLETION_REPORT_JSON_CHARS = 8_000
# The report's closed sets. These tuples are their one owner: the command validates
# against them, and the OpenAPI field descriptions and the complete_claim tool's
# input schema are rendered from them (``report_field_docs``).
COMPLETION_RESULT_STATES = ("met", "not_met", "not_assessed", "inconclusive")
COMPLETION_RESULT_KEYS = ("condition", "state", "note")
COMPLETION_RESULT_OPTIONAL = ("subject",)
# The reporter's own one-word result, in the words this fleet's reviews already use. A closed
# vocabulary, exact and case-sensitive, so a typo refuses instead of becoming a new category.
# It is a fact the reporter asserts, never a score or a verification (docs/ANSWERABILITY.md).
COMPLETION_VERDICTS = ("PASS", "PASS-WITH-NOTES", "HOLD", "BLOCKED")
# Severity keys for the reporter's finding counts. A key that is absent means "not stated",
# not zero; an empty object is refused so that ambiguity cannot be written.
COMPLETION_FINDING_SEVERITIES = ("P1", "P2", "P3")
# "attachment" and "forge_event" point at a row Athena holds by id (an attachment, or an imported
# forge commit / pull-request event); their ref is that id. Shape only: see completion_evidence.py.
COMPLETION_EVIDENCE_KINDS = (
    "event",
    "handoff",
    "doc",
    "file",
    "manifest",
    "attachment",
    "forge_event",
)
COMPLETION_ID_REF_KINDS = frozenset({"attachment", "forge_event"})
COMPLETION_EVIDENCE_KEYS = ("kind", "source", "ref", "scope")
COMPLETION_EVIDENCE_OPTIONAL = ("digest", "bytes", "locator")
MAX_COMPLETION_SUBJECTS = 10
MAX_COMPLETION_SUBJECT_CHARS = 200
MAX_COMPLETION_LOCATOR_CHARS = 500
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_ROW_ID = re.compile(r"[1-9][0-9]{0,17}")
COMPLETION_EVIDENCE_SCOPES = ("source-only", "synthetic", "actual-run", "unrun")
#: The structured field a refused completion report carries: every problem, each
#: as ``{"path", "message"}``. ``$`` is the report as a whole.
REPORT_VIOLATIONS_FIELD = "violations"
CLAIM_PRECONDITION_REQUIRED_DETAIL = (
    "If-Match with exactly one strong issue ETag is required to claim"
)
#: What a renewal's hint says about If-Match: the generation alone renews (A12).
RENEWAL_IF_MATCH_NOTE = (
    "no If-Match is needed unless the renewal names paths or coordination_root"
)
LEASE_GENERATION_REQUIRED_DETAIL = (
    "the exact current lease generation is required for this operation"
)


def claimant_is_eligible(
    conn: sqlite3.Connection, issue: dict, actor: dict | None
) -> bool:
    """Whether ``actor`` passes the command-owned claimant policy.

    Read surfaces may use this to advertise an affordance, but it grants no
    authority: :func:`claim_issue` evaluates the same function again under its
    write transaction.
    """
    return actor is not None and (
        issue["assignee_id"] == actor["id"]
        or contributors_data.is_contributor(conn, issue["id"], actor["id"])
        or identity.is_admin(actor)
    )


def _claimant_or_reject(conn: sqlite3.Connection, issue: dict, actor: dict) -> None:
    """A lease is for whoever actually works the issue: its assignee, a delegated
    contributor, or an admin. Deliberately NARROWER than the general issue-write gate
    (which also lets the creator in) — creating an issue is not the same as being handed
    it to work."""
    if claimant_is_eligible(conn, issue, actor):
        return
    raise IssueCommandError(
        "forbidden",
        "only the assignee, a delegated contributor, or an admin may claim this issue",
    )


def _claim_held(
    conn: sqlite3.Connection, actor: dict, existing: dict, detail: str
) -> IssueCommandError:
    """``claim_held``, with the holder and the expiry its detail names as fields
    (F11). They name another account's lease, so they are filled only when the
    caller could read that lease over REST (``GET /issues/{id}/lease``), the
    fence refusal's rule; otherwise both are null and the body is otherwise the
    same. Every caller today has passed ``_visible_issue`` first.

    ``events_after`` is the cursor the registered ``next`` waits from:
    ``wait_for_events(kind="issue", target=<issue>, after=events_after)`` parks
    until the holder's next event on the issue, never on its history."""
    issue_id = int(existing["issue_id"])
    visible = access.can_see_issue(conn, actor, issue_id)
    return IssueCommandError(
        "conflict",
        detail,
        code=CLAIM_HELD_CODE,
        extra={
            "holder": (
                {"id": int(existing["holder_id"]), "name": existing["holder_name"]}
                if visible
                else None
            ),
            "expires_at": existing["expires_at"] if visible else None,
            "events_after": (
                newest_issue_event_id(conn, actor, issue_id) if visible else None
            ),
        },
    )


def newest_issue_event_id(conn: sqlite3.Connection, actor: dict, issue_id: int) -> int:
    """The newest event on the issue this actor may read, or 0: the cursor a
    refusal hands out so that a wait on the issue answers only with what comes
    next. Read through the gated feed, so it never names a hidden event."""
    newest = activity.list_activity(
        conn, target_kind="issue", target_id=issue_id, limit=1, actor=actor
    )
    return int(newest[0]["id"]) if newest else 0


def _normalize_lease_generation(
    generation: object | None, *, required: bool
) -> str | None:
    if generation is None:
        if required:
            raise IssueCommandError(
                "lease_generation_required",
                LEASE_GENERATION_REQUIRED_DETAIL,
            )
        return None
    if not leases.is_valid_generation(generation):
        raise IssueCommandError(
            "invalid_lease_generation",
            "lease generation must be exactly 32 lowercase hexadecimal characters",
        )
    return generation


def _matching_lease_generation(
    existing: dict, generation: object | None, *, actor: dict
) -> str:
    """The caller's generation, checked against the lease's current one.

    With none given, ``lease_generation_required``: the holder is told its own
    generation as the ``generation`` field (F11), since it could read it from
    its lease anyway; anyone else (an admin releasing another's lease) is not,
    and reads the lease instead."""
    if generation is None and existing["holder_id"] == actor["id"]:
        raise IssueCommandError(
            "lease_generation_required",
            LEASE_GENERATION_REQUIRED_DETAIL,
            extra={"generation": existing["generation"]},
        )
    normalized = _normalize_lease_generation(generation, required=True)
    assert normalized is not None
    if not secrets.compare_digest(existing["generation"], normalized):
        raise IssueCommandError(
            "lease_generation_mismatch",
            "lease generation is stale; fetch the current lease before retrying",
        )
    return normalized


def normalize_declared_paths(raw: object) -> list[str]:
    """Normalize the request with the same grammar used for stored fences."""
    try:
        return coordination_roots.normalize_declared_paths(raw)
    except coordination_roots.RootError as exc:
        raise IssueCommandError(exc.kind, str(exc)) from exc


def paths_overlap(left: str, right: str) -> bool:
    """Whether two normalized paths fence the same file: they are equal, or one
    is a directory of the other. Compared segment by segment, never as string
    prefixes, so ``src/a`` covers ``src/a/x.py`` but not ``src/ab``."""
    mine, theirs = left.split("/"), right.split("/")
    depth = min(len(mine), len(theirs))
    return mine[:depth] == theirs[:depth]


def _canonical_path(prefix: str, path: str) -> str:
    joined = "/".join(part for part in (prefix, path) if part)
    if len(joined) > 512:
        raise IssueCommandError(
            "invalid", "canonical declared path exceeds 512 characters"
        )
    return joined


#: What a fence_unresolved detail tells the caller to do with a catalog. No read
#: returns a selector (docs/ACTIVE_WORK.md): the operator hands one out.
_GET_A_SELECTOR = "obtain a current reviewed selector from the operator"

#: What a fence refusal's ``basis`` says when neither base above applies: with a
#: root catalog configured, one of the two fences has no current root.
FENCE_BASIS_UNRESOLVED = "unresolved"

# A fence as the overlap check compares it: the base it sits on, and each
# declared path with the path compared (its root's prefix joined on).
_Fence = tuple[tuple[str, ...], list[tuple[str, str]]]


def _comparable_fence(
    paths: list[str] | None,
    root: dict | None,
    catalog: coordination_roots.RootCatalog | None,
) -> tuple[_Fence | None, str]:
    """The fence's base and compared paths, or None and why it cannot be placed.

    With no catalog every readable fence is under one implicit root (D5). With
    a catalog, a fence is placed only through a current root; one without a
    root cannot prove separation from anything, so it is None, as is a stored
    fence whose paths are unreadable."""
    if not paths:
        return None, "its stored paths are unreadable"
    if catalog is None:
        return (("lexical",), [(path, path) for path in paths]), ""
    unrooted = "it has no current root in this instance's catalog"
    if root is None:
        return None, unrooted
    try:
        tree_id, prefix = coordination_roots.resolve(root, catalog)
    except coordination_roots.RootError:
        return None, unrooted
    compared = [(_canonical_path(prefix, path), path) for path in paths]
    return (("root", tree_id), compared), ""


def _fence_refusal(
    conn: sqlite3.Connection,
    actor: dict,
    *,
    code: str,
    detail: str,
    basis: str,
    other: dict,
    path: str | None,
    holder_path: str | None,
) -> IssueCommandError:
    """A fence refusal. The other lease's issue, holder and path are named only
    when the caller could read that lease over REST (``GET /issues/{id}/lease``);
    otherwise they are null, and nothing else in the body differs."""
    issue = issues.get_issue(conn, int(other["issue_id"]))
    visible = issue is not None and access.can_see_project_or_backlog(
        conn, actor, issue["project_id"]
    )
    fence = {
        "basis": basis,
        "path": path,
        "issue": (
            {"id": issue["id"], "key": issue.get("key")}
            if visible and issue is not None
            else None
        ),
        "holder": (
            {"id": int(other["holder_id"]), "name": other["holder_name"]}
            if visible
            else None
        ),
        "holder_path": holder_path if visible else None,
        # The cursor the registered `next` waits from, on the other lease's
        # issue: its holder's next event there, never its history.
        "events_after": (
            newest_issue_event_id(conn, actor, int(issue["id"]))
            if visible and issue is not None
            else None
        ),
    }
    return IssueCommandError("conflict", detail, code=code, extra={"fence": fence})


def _refuse_overlapping_paths(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    issue_id: int,
    paths: list[str] | None,
    root: dict | None,
) -> None:
    """Refuse a fence that overlaps another active lease's, or that cannot be
    shown separate from one. An overlap is ``fence_overlap`` and names the
    caller's path; an unplaceable fence is ``fence_unresolved``. Every active
    lease takes part, in every project, whoever can see it."""
    if paths == []:
        return
    catalog = config.LEASE_ROOT_CATALOG
    mine, why_mine = _comparable_fence(paths, root, catalog)
    unresolved: tuple[dict, str] | None = None
    for other in leases.list_active_leases(conn, except_issue_id=issue_id):
        if other["path_fence"] == "issue_only":
            continue
        try:
            theirs, why_theirs = _comparable_fence(
                other["declared_paths"], other.get("coordination_root"), catalog
            )
        except IssueCommandError:
            # A stored join over the bound: unplaceable, not the caller's input.
            theirs, why_theirs = None, "its stored paths are unreadable"
        if mine is None or theirs is None:
            if unresolved is None:
                unresolved = (
                    (
                        other,
                        f"your fence cannot be placed: {why_mine}; "
                        + (
                            _GET_A_SELECTOR
                            if paths is not None
                            else "renew with explicit paths"
                        ),
                    )
                    if mine is None
                    else (
                        other,
                        f"the other lease's fence cannot be placed: {why_theirs}",
                    )
                )
            continue
        if mine[0] != theirs[0]:
            continue  # qualified distinct families coexist
        for compared, declared in mine[1]:
            hit = next(
                (held for seen, held in theirs[1] if paths_overlap(compared, seen)),
                None,
            )
            if hit is not None:
                basis = (
                    leases.BASIS_LEXICAL
                    if mine[0] == ("lexical",)
                    else leases.BASIS_ROOTED
                )
                raise _fence_refusal(
                    conn,
                    actor,
                    code=FENCE_OVERLAP_CODE,
                    detail=(
                        f"declared path {declared} overlaps another active "
                        f"lease's fence ({basis})"
                    ),
                    basis=basis,
                    other=other,
                    path=declared,
                    holder_path=hit,
                )
    if unresolved is not None:
        other, why = unresolved
        raise _fence_refusal(
            conn,
            actor,
            code=FENCE_UNRESOLVED_CODE,
            detail=(
                "declared paths cannot be shown separate from another active "
                f"lease's fence; {why}"
            ),
            basis=FENCE_BASIS_UNRESOLVED,
            other=other,
            path=None,
            holder_path=None,
        )


def claim_issue(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    if_match: list[str] | None = None,
    generation: object | None = None,
    lease_seconds: int = leases.DEFAULT_LEASE_SECONDS,
    paths: object = None,
    coordination_root: object = None,
) -> dict:
    """Take the exclusive lease on an issue (accept). Returns the lease
    {issue_id, holder_id, holder_name, claimed_at, expires_at, active}.

    Rejects with IssueCommandError('conflict') if the issue is already held by a DIFFERENT
    actor whose lease is still active — the interlock that stops double-work; the detail
    names the current holder and expiry. The same actor re-claiming RENEWS the window
    (idempotent extend), and a lease whose window has passed is reclaimable by anyone
    eligible, so a crashed holder never pins the work. 401/403 for the scope/writer and
    claimant gates, 404 for an unseeable issue, 422 for an out-of-range lease window.

    Read-then-write runs under BEGIN IMMEDIATE, so two agents racing to claim the same
    free issue serialize: the first acquires, the second re-reads the now-active lease and
    is rejected."""
    actor = _require_issue_writer(actor)
    if not (leases.MIN_LEASE_SECONDS <= lease_seconds <= leases.MAX_LEASE_SECONDS):
        raise IssueCommandError(
            "invalid",
            f"lease_seconds must be between {leases.MIN_LEASE_SECONDS} and "
            f"{leases.MAX_LEASE_SECONDS}",
        )
    with db.transaction(conn, immediate=True):
        issue = _visible_issue(conn, actor, issue_id)
        _claimant_or_reject(conn, issue, actor)
        existing = leases.get_lease(conn, issue_id)
        renewed = False
        current_generation: str | None = None
        if existing is not None and existing["active"]:
            if existing["holder_id"] != actor["id"]:
                raise _claim_held(
                    conn,
                    actor,
                    existing,
                    f"issue is claimed by {existing['holder_name']} until "
                    f"{packet_time.rfc3339(existing['expires_at'])}",
                )
            if generation is None:
                # The holder repeated the claim without saying so. Say what this
                # call is and what it needs. Only the holder reaches this branch
                # (a rival was refused above, with no generation in the message),
                # so the generation is named to its owner alone, in the detail
                # and as the `generation` field (F11). Same code and status as
                # before: 428 lease_generation_required.
                raise IssueCommandError(
                    "lease_generation_required",
                    "you already hold this issue's lease; a repeated claim is a "
                    f"renewal: pass generation={existing['generation']} to extend it "
                    f"({RENEWAL_IF_MATCH_NOTE})",
                    extra={"generation": existing["generation"]},
                )
            # Supplying the current generation explicitly selects renew mode. An
            # omitted token may never turn a delayed acquire into a renewal.
            current_generation = _matching_lease_generation(
                existing, generation, actor=actor
            )
            renewed = True
        elif generation is not None:
            # Supplying a token explicitly selects renew mode. An expired/absent
            # possession cannot be renewed and must not silently become an acquire.
            _normalize_lease_generation(generation, required=True)
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation is stale; acquire again without a generation",
            )
        # The issue ETag guards a decision to take work against the revision the
        # claimant reviewed. A renewal takes nothing new: ``renewed`` holds only
        # for the active lease's own holder (a rival was refused claim_held above)
        # with its exact current generation, which is the fence. So a renewal that
        # keeps its fence (no paths, no root) may omit If-Match (A12); one it sends
        # is still checked. An acquisition, and a renewal that names paths or a
        # coordination root, need the reviewed revision as before.
        generation_only_renewal = (
            renewed and paths is None and coordination_root is None
        )
        if not (generation_only_renewal and if_match is None):
            _check_issue_precondition(
                conn,
                issue,
                if_match,
                required_detail=CLAIM_PRECONDITION_REQUIRED_DETAIL,
                exact=True,
            )
        preserve_paths = renewed and paths is None
        declared_paths: list[str] | None = normalize_declared_paths(paths)
        root = None
        if renewed and existing is not None:
            root = existing["coordination_root"]
            if preserve_paths:
                declared_paths = existing["declared_paths"]
                if declared_paths == [] and existing["path_fence"] == "unresolved":
                    # A failed stored decode is not an explicit empty request.
                    declared_paths = None
        if coordination_root is not None:
            try:
                requested_root = coordination_roots.parse_selector(
                    coordination_root
                ).as_dict()
                requested_location = coordination_roots.resolve(
                    requested_root, config.LEASE_ROOT_CATALOG
                )
                if renewed:
                    if root is None or requested_location != coordination_roots.resolve(
                        root, config.LEASE_ROOT_CATALOG
                    ):
                        raise IssueCommandError(
                            "conflict",
                            "changing coordination root requires a new possession",
                        )
                else:
                    root = requested_root
            except coordination_roots.RootError as exc:
                # A well-formed selector this instance cannot resolve (no
                # catalog, a stale digest, an unknown root) cannot place the
                # fence: fence_unresolved. No read returns a selector, so the
                # detail says what to do instead of naming a next call.
                detail = str(exc)
                if exc.kind == "conflict":
                    detail = (
                        "this instance has no root catalog: omit coordination_root; "
                        "declared paths are compared under one implicit root "
                        f"({leases.BASIS_LEXICAL})"
                        if config.LEASE_ROOT_CATALOG is None
                        else f"{detail}; {_GET_A_SELECTOR}"
                    )
                raise IssueCommandError(
                    exc.kind,
                    detail,
                    code=FENCE_UNRESOLVED_CODE if exc.kind == "conflict" else None,
                ) from exc
        _refuse_overlapping_paths(
            conn, actor=actor, issue_id=issue_id, paths=declared_paths, root=root
        )
        if not renewed:
            # Taking work is a coordination write (D6). A renewal is not
            # metered: a holder must always be able to keep, yield or complete
            # what it holds. Charged last, after every refusal that would have
            # stopped the claim anyway.
            budgets.charge(conn, actor, counter=budgets.COORDINATION)
        try:
            lease = leases.upsert_lease(
                conn,
                issue_id,
                actor["id"],
                lease_seconds,
                generation=current_generation,
                declared_paths=declared_paths,
                coordination_root=root,
                preserve_coordination_root=renewed,
                preserve_declared_paths=preserve_paths,
                commit=False,
            )
        except sqlite3.IntegrityError as exc:
            # The writer lock excludes rivals, not time: this possession can
            # expire after the active read. Keep the SQL resurrection guard and
            # report its refusal just like an already-expired renewal.
            if (
                not renewed
                or str(exc) != "reactivated issue lease requires a new generation"
            ):
                raise
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation is stale; acquire again without a generation",
            ) from exc
        issue_activity.record_issue_claimed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            expires_at=lease["expires_at"],
            generation=lease["generation"],
            renewed=renewed,
            coordination_root=lease["coordination_root"],
            path_fence=lease["path_fence"],
            commit=False,
        )
        if renewed:
            _count_renewal(conn, actor=actor, issue_id=issue_id, lease=lease)
        lease["open_claim_handoff"] = claim_handoffs.get_open_handoff(conn, issue_id)
        # The issue's current ETag, read in this transaction, so a holder that
        # only renews (no If-Match, A12) still sees that the issue changed:
        # closed, archived, re-scoped. Beside the lease, not part of it: GET
        # /issues/{id}/lease and the lease's generation never carry it.
        current = issues.get_issue(conn, issue_id)
        assert current is not None
        lease["issue_etag"] = issue_etags.current_etag(conn, current)
        return lease


def _count_renewal(
    conn: sqlite3.Connection, *, actor: dict, issue_id: int, lease: dict
) -> None:
    """Put on a renewal's answer how many times this possession has been
    renewed with no work of the holder's recorded on the issue, and raise it
    once, on the renewal that reaches the threshold (C7).

    The count includes the renewal just recorded. It reaches the threshold on
    exactly one renewal of an unbroken run, so the event is recorded once; work
    by the holder starts the count again, and a later run can raise it again.
    The event goes to the accounts that answer for the work, never the holder,
    who reads the count on this answer. Nothing is released or refused: the
    renewal succeeds."""
    count = lease_progress.renewals_without_progress(
        conn,
        issue_id=issue_id,
        holder_id=int(actor["id"]),
        generation=lease["generation"],
    )
    lease["renewals_without_progress"] = count
    if count != lease_progress.threshold():
        return
    event = issue_activity.record_no_progress(
        conn,
        holder_id=int(actor["id"]),
        holder_name=str(actor["name"]),
        issue_id=issue_id,
        generation=lease["generation"],
        renewals=count,
        commit=False,
    )
    notifications.notify_users(
        conn,
        event_id=event["id"],
        user_ids=accountable.answering_for(
            conn, holder_id=int(actor["id"]), issue_id=issue_id
        ),
    )


#: What every completion's answer says: the lease is gone, the issue is as it was.
COMPLETION_NEXT = "complete_claim released the lease only; issue status is unchanged"
#: The verdicts that say the work is not done (W4).
HELD_VERDICTS = ("HOLD", "BLOCKED")


def _completion_next(report: dict | None) -> str:
    """The completion answer's ``next``. A report of HOLD or BLOCKED says what
    is wrong with one piece of work; this asks its author, while the finding is
    in front of it, for the one rule that would have prevented it, and names
    the call that records it (W4). Nothing is recorded or promoted by
    reporting: the learning is the reporter's own later act."""
    verdict = None if report is None else report.get("verdict")
    if verdict not in HELD_VERDICTS:
        return COMPLETION_NEXT
    return (
        f"{COMPLETION_NEXT}. You reported {verdict}: record the one rule that "
        "would have prevented it with record_run_learning(issue_id, summary)"
    )


def _normalize_claim_yield(reason: str, note: str | None) -> tuple[str, str | None]:
    if not isinstance(reason, str) or reason not in CLAIM_YIELD_REASONS:
        raise IssueCommandError(
            "invalid",
            "reason must be one of: needs_input, blocked, capacity",
        )
    normalized_note = _normalize_handoff_text(
        note,
        name="note",
        maximum=MAX_CLAIM_YIELD_NOTE_CHARS,
        required=False,
    )
    return reason, normalized_note or None


def _normalize_handoff_text(
    value: object | None,
    *,
    name: str,
    maximum: int,
    required: bool,
) -> str | None:
    if value is None:
        if required:
            raise IssueCommandError("invalid", f"{name} is required")
        return None
    if not isinstance(value, str):
        suffix = "a string" if required else "a string or null"
        raise IssueCommandError("invalid", f"{name} must be {suffix}")
    if len(value) > maximum:
        raise IssueCommandError(
            "invalid", f"{name} must be at most {maximum} characters"
        )
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise IssueCommandError(
            "invalid", f"{name} contains an unsupported control character"
        )
    normalized = value.strip()
    if required and not normalized:
        raise IssueCommandError("invalid", f"{name} must not be blank")
    return normalized or None


def _normalize_handoff_evidence(evidence: object) -> list[str]:
    if not isinstance(evidence, list):
        raise IssueCommandError("invalid", "evidence must be an array of strings")
    if len(evidence) > MAX_HANDOFF_EVIDENCE_ITEMS:
        raise IssueCommandError(
            "invalid",
            f"evidence must contain at most {MAX_HANDOFF_EVIDENCE_ITEMS} items",
        )
    normalized: list[str] = []
    for index, item in enumerate(evidence):
        text = _normalize_handoff_text(
            item,
            name=f"evidence[{index}]",
            maximum=MAX_HANDOFF_EVIDENCE_ITEM_CHARS,
            required=True,
        )
        assert text is not None
        normalized.append(text)
    if (
        len(claim_handoffs.encode_evidence(normalized))
        > MAX_HANDOFF_EVIDENCE_JSON_CHARS
    ):
        raise IssueCommandError(
            "invalid",
            f"encoded evidence must be at most {MAX_HANDOFF_EVIDENCE_JSON_CHARS} characters",
        )
    return normalized


def _normalize_handoff_token(handoff_token: object) -> str:
    if not claim_handoffs.is_valid_handoff_token(handoff_token):
        raise IssueCommandError(
            "invalid",
            "handoff token must be exactly 32 lowercase hexadecimal characters",
        )
    assert isinstance(handoff_token, str)
    return handoff_token


def _text_problem(value: object, *, maximum: int, blank: bool = False) -> str | None:
    """The handoff text rule a report string breaks, or None: the same checks as
    :func:`_normalize_handoff_text`, reported instead of raised. A report keeps
    the exact submitted text, so nothing here trims or rewrites it."""
    if not isinstance(value, str):
        return "must be a string"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "must be valid Unicode text"
    if len(value) > maximum:
        return f"must be at most {maximum} characters"
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        return "contains an unsupported control character"
    if not blank and not value.strip():
        return "must not be blank"
    return None


def _one_of(words: tuple[str, ...]) -> str:
    return ", ".join(words[:-1]) + f", or {words[-1]}"


def _keys_rule(required: tuple[str, ...], optional: tuple[str, ...]) -> str:
    rule = f"must be an object with exactly {', '.join(required)}"
    return rule + (f", plus optionally {', '.join(optional)}" if optional else "")


def report_field_docs() -> dict[str, str]:
    """One sentence per report field, rendered from the closed sets above. The
    REST model's OpenAPI descriptions and the complete_claim tool's input schema
    both publish these, so the words an agent reads are the words the command
    checks. Documentation only: the command stays the validator."""
    return {
        "verdict": f"One of {_one_of(COMPLETION_VERDICTS)}; exact case.",
        "findings": (
            f"Counts keyed {', '.join(COMPLETION_FINDING_SEVERITIES)}: "
            "non-negative integers, at least one key."
        ),
        "results": (
            f"Up to {MAX_COMPLETION_RESULTS} objects with keys "
            f"{', '.join(COMPLETION_RESULT_KEYS)} (optional "
            f"{', '.join(COMPLETION_RESULT_OPTIONAL)}); state is "
            f"{_one_of(COMPLETION_RESULT_STATES)}."
        ),
        "evidence": (
            f"Up to {MAX_HANDOFF_EVIDENCE_ITEMS} objects with keys "
            f"{', '.join(COMPLETION_EVIDENCE_KEYS)} (optional "
            f"{', '.join(COMPLETION_EVIDENCE_OPTIONAL)}); kind is "
            f"{_one_of(COMPLETION_EVIDENCE_KINDS)}; scope is "
            f"{_one_of(COMPLETION_EVIDENCE_SCOPES)}."
        ),
    }


class _Problems:
    """The violations of one completion report, collected rather than raised one
    at a time, each by field path. A message never quotes the rejected value."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.items.append({"path": path, "message": message})

    def text(
        self, value: object, path: str, *, maximum: int, blank: bool = False
    ) -> None:
        problem = _text_problem(value, maximum=maximum, blank=blank)
        if problem is not None:
            self.add(path, problem)

    def raise_any(self) -> None:
        if not self.items:
            return
        lines = [
            item["message"]
            if item["path"] == "$"
            else f"{item['path']} {item['message']}"
            for item in self.items
        ]
        detail = (
            lines[0]
            if len(lines) == 1
            else f"the completion report has {len(lines)} problems: " + "; ".join(lines)
        )
        raise IssueCommandError(
            "invalid",
            detail,
            extra={REPORT_VIOLATIONS_FIELD: list(self.items)},
        )


def _result_problems(problems: _Problems, label: str, item: dict) -> None:
    if "state" in item and (
        not isinstance(item["state"], str)
        or item["state"] not in COMPLETION_RESULT_STATES
    ):
        problems.add(f"{label}.state", f"must be {_one_of(COMPLETION_RESULT_STATES)}")
    if "condition" in item:
        problems.text(
            item["condition"], f"{label}.condition", maximum=MAX_COMPLETION_TEXT_CHARS
        )
    if "note" in item:
        problems.text(
            item["note"], f"{label}.note", maximum=MAX_COMPLETION_TEXT_CHARS, blank=True
        )
    if "subject" in item:
        subject = item["subject"]
        if (
            not isinstance(subject, list)
            or not subject
            or len(subject) > MAX_COMPLETION_SUBJECTS
        ):
            problems.add(
                f"{label}.subject",
                f"must be an array of 1 to {MAX_COMPLETION_SUBJECTS} items naming "
                "what was judged",
            )
        else:
            for n, entry in enumerate(subject):
                problems.text(
                    entry,
                    f"{label}.subject[{n}]",
                    maximum=MAX_COMPLETION_SUBJECT_CHARS,
                )


def _evidence_problems(problems: _Problems, label: str, item: dict) -> None:
    kind = item.get("kind")
    if "kind" in item and (
        not isinstance(kind, str) or kind not in COMPLETION_EVIDENCE_KINDS
    ):
        problems.add(f"{label}.kind", f"must be {_one_of(COMPLETION_EVIDENCE_KINDS)}")
    if "scope" in item and (
        not isinstance(item["scope"], str)
        or item["scope"] not in COMPLETION_EVIDENCE_SCOPES
    ):
        problems.add(f"{label}.scope", f"must be {_one_of(COMPLETION_EVIDENCE_SCOPES)}")
    if "source" in item:
        problems.text(
            item["source"], f"{label}.source", maximum=MAX_COMPLETION_SOURCE_CHARS
        )
    if "ref" in item:
        ref = item["ref"]
        ref_problem = _text_problem(ref, maximum=MAX_HANDOFF_EVIDENCE_ITEM_CHARS)
        if ref_problem is not None:
            problems.add(f"{label}.ref", ref_problem)
        elif kind in COMPLETION_ID_REF_KINDS and not _ROW_ID.fullmatch(str(ref)):
            problems.add(f"{label}.ref", f"must be the numeric id of the {kind} row")
    if "digest" in item and (
        not isinstance(item["digest"], str) or not _SHA256_HEX.fullmatch(item["digest"])
    ):
        problems.add(f"{label}.digest", "must be 64 lowercase hex characters (SHA-256)")
    if "bytes" in item:
        size = item["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            problems.add(f"{label}.bytes", "must be a non-negative integer")
    if "locator" in item:
        problems.text(
            item["locator"], f"{label}.locator", maximum=MAX_COMPLETION_LOCATOR_CHARS
        )


def _normalize_completion_report(
    *,
    results: object = None,
    unfinished: object = None,
    uncertainty: object = None,
    evidence: object = None,
    verdict: object = None,
    findings: object = None,
    usage: object = None,
) -> dict | None:
    """Bound advisory data without lookup, execution, or verification.

    ``verdict`` and ``findings`` are optional reporter assertions in closed vocabularies.
    Every problem is collected and refused together, each by field path, in the
    error's ``violations``; a message never repeats the rejected value.

    This grammar is reused on reads: credential refusal belongs only to the
    accepting write commands, so existing stored history remains readable.
    """
    problems = _Problems()
    if usage is not None:
        for path, rule in reported_usage.usage_problems(usage):
            problems.add(path, rule)
    if verdict is not None and (
        not isinstance(verdict, str) or verdict not in COMPLETION_VERDICTS
    ):
        problems.add("verdict", f"must be one of {', '.join(COMPLETION_VERDICTS)}")
    if findings is not None:
        severities = ", ".join(COMPLETION_FINDING_SEVERITIES)
        if not isinstance(findings, dict) or not findings:
            problems.add(
                "findings", f"must be an object with at least one of {severities}"
            )
        else:
            if any(key not in COMPLETION_FINDING_SEVERITIES for key in findings):
                problems.add("findings", f"has a key outside {severities}")
            for key in COMPLETION_FINDING_SEVERITIES:
                count = findings.get(key, 0)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    problems.add(f"findings.{key}", "must be a non-negative integer")
    lists = (
        ("results", results, MAX_COMPLETION_RESULTS),
        ("unfinished", unfinished, MAX_COMPLETION_TEXT_ITEMS),
        ("uncertainty", uncertainty, MAX_COMPLETION_TEXT_ITEMS),
        ("evidence", evidence, MAX_HANDOFF_EVIDENCE_ITEMS),
    )
    for name, raw, maximum in lists:
        if raw is None:
            continue
        if not isinstance(raw, list) or len(raw) > maximum:
            problems.add(name, f"must be an array of at most {maximum} items")
            continue
        for index, item in enumerate(raw):
            label = f"{name}[{index}]"
            if name in {"unfinished", "uncertainty"}:
                problems.text(item, label, maximum=MAX_COMPLETION_TEXT_CHARS)
                continue
            keys, optional = (
                (COMPLETION_RESULT_KEYS, COMPLETION_RESULT_OPTIONAL)
                if name == "results"
                else (COMPLETION_EVIDENCE_KEYS, COMPLETION_EVIDENCE_OPTIONAL)
            )
            if not isinstance(item, dict):
                problems.add(label, _keys_rule(keys, optional))
                continue
            if not (set(keys) <= set(item) <= set(keys) | set(optional)):
                problems.add(label, _keys_rule(keys, optional))
            if name == "results":
                _result_problems(problems, label, item)
            else:
                _evidence_problems(problems, label, item)
    problems.raise_any()
    if isinstance(evidence, list) and evidence:
        # The same item-count, text/control and encoded-ref envelope as yield.
        # Validation trims there; retain the original bytes in this report.
        try:
            _normalize_handoff_evidence([item["ref"] for item in evidence])
        except IssueCommandError:
            # Every item passed above, so only the encoded size can fail here.
            problems.add(
                "evidence",
                f"refs must encode to at most {MAX_HANDOFF_EVIDENCE_JSON_CHARS} "
                "characters together",
            )
            problems.raise_any()
    report: dict = {}
    if usage is not None:
        report["usage"] = reported_usage.normalize_usage(usage)
    if verdict is not None:
        report["verdict"] = verdict
    if findings is not None:
        assert isinstance(findings, dict)
        report["findings"] = dict(findings)
    for name, raw, _maximum in lists:
        if raw is None:
            continue
        assert isinstance(raw, list)
        if name in {"unfinished", "uncertainty"}:
            report[name] = list(raw)
            continue
        fields = (
            COMPLETION_RESULT_KEYS + COMPLETION_RESULT_OPTIONAL
            if name == "results"
            else COMPLETION_EVIDENCE_KEYS + COMPLETION_EVIDENCE_OPTIONAL
        )
        report[name] = [
            {
                field: list(item[field]) if field == "subject" else item[field]
                for field in fields
                if field in item
            }
            for item in raw
        ]
    if not report:
        return None
    if (
        len(
            json.dumps(
                report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        > MAX_COMPLETION_REPORT_JSON_CHARS
    ):
        problems.add(
            "$",
            "the encoded reported completion must be at most "
            f"{MAX_COMPLETION_REPORT_JSON_CHARS} characters",
        )
        problems.raise_any()
    return report


def reported_completion(event: dict) -> dict | None:
    """Read bounded native report data; imported text never becomes a native report."""
    if event["verb"] != "claim_completed" or event.get("imported_at") is not None:
        return None
    header, separator, payload = event["detail"].partition(
        issue_activity.COMPLETION_REPORT_MARKER
    )
    if (
        not separator
        or not header.startswith("generation ")
        or not leases.is_valid_generation(header.removeprefix("generation "))
    ):
        return None
    if len(payload) > MAX_COMPLETION_REPORT_JSON_CHARS:
        return None
    try:
        raw = json.loads(payload)
        if (
            not isinstance(raw, dict)
            or not raw
            or set(raw)
            - {
                "verdict",
                "findings",
                "usage",
                "results",
                "unfinished",
                "uncertainty",
                "evidence",
            }
        ):
            return None
        report = _normalize_completion_report(**raw)
        return report if report == raw else None
    except (ValueError, RecursionError, IssueCommandError):
        return None


def yield_claim(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    generation: object | None,
    reason: str,
    note: str | None = None,
    attempted_work: object,
    evidence: object,
    blocking_question: object,
    resume_instructions: object,
) -> dict:
    """Release the caller's active lease without pretending the work completed.

    Yield is deliberately holder-only, including for admins: recording another
    actor as voluntarily yielding would make the audit trail lie. It preserves
    assignment, contributors, status, and dependencies, and never auto-routes
    the issue. The lease deletion and run-stamped activity event are atomic.
    """
    actor = _require_issue_writer(actor)
    reason, note = _normalize_claim_yield(reason, note)
    normalized_attempted_work = _normalize_handoff_text(
        attempted_work,
        name="attempted_work",
        maximum=MAX_HANDOFF_ATTEMPTED_WORK_CHARS,
        required=True,
    )
    normalized_evidence = _normalize_handoff_evidence(evidence)
    for index, text in enumerate(normalized_evidence):
        kind = report_credentials.credential_kind(text)
        if kind is not None:
            raise IssueCommandError(
                "invalid",
                f"evidence[{index}] contains {kind}; remove the credential and retry",
            )
    normalized_blocking_question = _normalize_handoff_text(
        blocking_question,
        name="blocking_question",
        maximum=MAX_HANDOFF_BLOCKING_QUESTION_CHARS,
        required=True,
    )
    normalized_resume_instructions = _normalize_handoff_text(
        resume_instructions,
        name="resume_instructions",
        maximum=MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS,
        required=True,
    )
    assert normalized_attempted_work is not None
    assert normalized_blocking_question is not None
    assert normalized_resume_instructions is not None
    with db.transaction(conn, immediate=True):
        _visible_issue(conn, actor, issue_id)
        existing = leases.get_lease(conn, issue_id)
        if existing is None or not existing["active"]:
            raise IssueCommandError(
                "conflict", "no active claim to yield", code=CLAIM_REQUIRED_CODE
            )
        if existing["holder_id"] != actor["id"]:
            raise _claim_held(
                conn,
                actor,
                existing,
                f"issue is claimed by {existing['holder_name']}, not you",
            )
        current_generation = _matching_lease_generation(
            existing, generation, actor=actor
        )
        if claim_handoffs.get_open_handoff(conn, issue_id) is not None:
            raise IssueCommandError(
                "conflict",
                "an open claim handoff must be resumed before another can be created",
            )
        handoff_token = claim_handoffs.new_handoff_token()
        event = issue_activity.record_claim_yielded(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            generation=current_generation,
            handoff_token=handoff_token,
            reason=reason,
            blocking_question=normalized_blocking_question,
            commit=False,
        )
        handoff = claim_handoffs.create_handoff(
            conn,
            issue_id=issue_id,
            yield_event_id=event["id"],
            lease_generation=current_generation,
            reason=reason,
            note=note,
            attempted_work=normalized_attempted_work,
            evidence=normalized_evidence,
            blocking_question=normalized_blocking_question,
            resume_instructions=normalized_resume_instructions,
            handoff_token=handoff_token,
        )
        if not leases.delete_lease(conn, issue_id, current_generation, commit=False):
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation changed before release",
            )
        return handoff


def resume_claim_handoff(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    handoff_token: object,
    generation: object | None,
    resume_note: object | None = None,
) -> dict:
    """Acknowledge one open handoff as the exact current leaseholder.

    This transition means the context was received. It never asserts that the
    blocker was solved, work completed, approval granted, or instructions trusted.
    """
    actor = _require_issue_writer(actor)
    normalized_token = _normalize_handoff_token(handoff_token)
    normalized_note = _normalize_handoff_text(
        resume_note,
        name="resume_note",
        maximum=MAX_HANDOFF_RESUME_NOTE_CHARS,
        required=False,
    )
    with db.transaction(conn, immediate=True):
        _visible_issue(conn, actor, issue_id)
        existing = leases.get_lease(conn, issue_id)
        if existing is None or not existing["active"]:
            raise IssueCommandError(
                "conflict",
                "an active claim is required to resume a handoff",
                code=CLAIM_REQUIRED_CODE,
            )
        if existing["holder_id"] != actor["id"]:
            raise _claim_held(
                conn,
                actor,
                existing,
                f"issue is claimed by {existing['holder_name']}, not you",
            )
        current_generation = _matching_lease_generation(
            existing, generation, actor=actor
        )
        handoff = claim_handoffs.get_handoff(
            conn, issue_id=issue_id, handoff_token=normalized_token
        )
        if handoff is None:
            raise IssueCommandError("not_found", "claim handoff not found")
        if handoff["state"] != "awaiting_resume":
            raise IssueCommandError("conflict", "claim handoff was already resumed")
        event = issue_activity.record_claim_handoff_resumed(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            handoff_token=normalized_token,
            generation=current_generation,
            commit=False,
        )
        resumed = claim_handoffs.resume_handoff(
            conn,
            issue_id=issue_id,
            handoff_token=normalized_token,
            resume_event_id=event["id"],
            lease_generation=current_generation,
            resume_note=normalized_note,
        )
        if resumed is None:
            raise IssueCommandError("conflict", "claim handoff was already resumed")
        return resumed


def complete_claim(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    generation: object | None,
    results: object = None,
    unfinished: object = None,
    uncertainty: object = None,
    evidence: object = None,
    verdict: object = None,
    findings: object = None,
    usage: object = None,
    candidate_submission: object = None,
    review_candidate_id: object = None,
) -> dict:
    """Release the lease by completing the claimed work (complete) — the issue is freed for
    the next claimant. The actor must hold the lease: an ACTIVE one, or its OWN row the
    clock has already expired (clearing a lapsed row is the holder's to do — nothing else
    removes it, so it would otherwise sit on their desk forever). An admin may release
    anyone's ACTIVE lease, a moderation lever recorded as ``lease_released`` (not as a
    completion, and with no report). An admin may also settle another account's EXPIRED
    row without claiming it (C8), naming its generation: the lapse is recorded first
    (``lease_lapsed``, once per generation) and the settlement is ``lease_released``.
    Rejects with IssueCommandError('conflict') if there is no lease row, or a non-admin's
    row belongs to someone else; 404 for an unseeable issue. Completion releases the
    coordination lease only — it does not change the issue's status; the agent transitions
    status through the ordinary (audited) status command."""
    actor = _require_issue_writer(actor)
    submission, review_id = review_commands.normalize_extensions(
        candidate_submission, review_candidate_id
    )
    extended = submission is not None or review_id is not None
    if extended and conn.in_transaction:
        raise IssueCommandError(
            "invalid",
            "candidate completion requires a connection without an open transaction",
        )
    report = _normalize_completion_report(
        results=results,
        unfinished=unfinished,
        uncertainty=uncertainty,
        evidence=evidence,
        verdict=verdict,
        findings=findings,
        usage=usage,
    )
    if report and "usage" in report:
        try:
            reported_usage.refuse_credentials(report["usage"])
        except ValueError as exc:
            raise IssueCommandError("invalid", str(exc)) from exc
    for name, items in (report or {}).items():
        if name in {"verdict", "findings", "usage"}:
            continue  # closed words and integers: nothing free-form to scan
        for index, item in enumerate(items):
            fields = item.items() if isinstance(item, dict) else [(None, item)]
            for field, value in fields:
                if field in {"state", "kind", "scope", "digest", "bytes"}:
                    continue
                # ``subject`` is a list of text; everything else scanned is one string.
                texts = (
                    [(f".{field}[{n}]", t) for n, t in enumerate(value)]
                    if field == "subject"
                    else [(f".{field}" if field else "", value)]
                )
                for suffix, text in texts:
                    kind = report_credentials.credential_kind(text)
                    if kind is not None:
                        raise IssueCommandError(
                            "invalid",
                            f"{name}[{index}]{suffix} contains {kind}; "
                            "remove the credential and retry",
                        )
    with db.transaction(conn, immediate=True):
        # Only extensions strengthen possession authority. The clock is sampled
        # after the writer reservation, never before a competing writer's wait.
        extension_stamp = leases.clock_stamp(datetime.now(UTC)) if extended else None
        issue = _visible_issue(conn, actor, issue_id)
        _claimant_or_reject(conn, issue, actor)
        existing = leases.get_lease(conn, issue_id)
        if existing is not None and extension_stamp is not None:
            existing["active"] = existing["expires_at"] > extension_stamp
        mine = existing is not None and existing["holder_id"] == actor["id"]
        if extended and (not mine or existing is None or not existing["active"]):
            raise IssueCommandError(
                "conflict",
                "candidate completion requires your own active claim",
                code=CLAIM_REQUIRED_CODE,
            )
        # An expired row that is not yours is not a possession a non-admin may
        # complete. An admin may settle it (C8), with its generation, below.
        admin = identity.is_admin(actor)
        if existing is None or (not existing["active"] and not mine and not admin):
            raise IssueCommandError(
                "conflict", "no active claim to complete", code=CLAIM_REQUIRED_CODE
            )
        if not mine and not admin:
            raise _claim_held(
                conn,
                actor,
                existing,
                f"issue is claimed by {existing['holder_name']}, not you",
            )
        lapsed = not existing["active"]
        if lapsed and mine and generation is None:
            # The immediate writer lock makes the server-observed expired
            # generation an exact fence: no replacement can appear between this
            # read and the deletion. Same reasoning as decline's expired branch.
            current_generation = existing["generation"]
        else:
            current_generation = _matching_lease_generation(
                existing, generation, actor=actor
            )
        if (
            mine
            and not lapsed
            and claim_handoffs.get_open_handoff(conn, issue_id) is not None
        ):
            # Only an ACTIVE possession owes the handshake. resume_claim_handoff
            # itself requires an active claim, so demanding it of a lapsed row
            # would be a refusal whose remedy is unreachable. The handoff stays
            # open either way and still gates whoever claims next.
            raise IssueCommandError(
                "conflict",
                "resume the open claim handoff before completing this possession",
            )
        if submission is not None:
            review_commands.prepare_submission(
                conn, issue=issue, actor=actor, submission=submission
            )
        elif review_id is not None:
            review_commands.prepare_review(
                conn, issue=issue, actor=actor, candidate_id=review_id
            )
        if lapsed and not mine:
            # The clock ended this possession before the admin settled it (C8).
            # Record that first, through the lapse's one writer: once per
            # generation, so a lapse an observer already recorded is not
            # recorded again. The release below then says only that the admin
            # cleared the row, never that it ended live work.
            lease_lapse_commands.record_lease_lapse(
                conn, actor=actor, issue_id=issue_id, generation=current_generation
            )
        if not leases.delete_lease(conn, issue_id, current_generation, commit=False):
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation changed before release",
            )
        if mine:
            event = issue_activity.record_claim_completed(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                generation=current_generation,
                reported=report,
                commit=False,
            )
        elif report:
            raise IssueCommandError(
                "invalid",
                "releasing another account's lease carries no completion report",
            )
        else:
            # An admin ending another account's live lease, or settling its
            # lapsed row, is a release, not a completion of that account's work
            # (U3, C8): its own verb, naming the holder, told to the holder, and
            # read by nothing as finished work.
            event = issue_activity.record_lease_released(
                conn,
                actor_id=actor["id"],
                issue_id=issue_id,
                generation=current_generation,
                holder_id=int(existing["holder_id"]),
                holder_name=str(existing["holder_name"]),
                commit=False,
            )
        still_open = (
            statuses.category_of(conn, issue.get("project_id"), issue["status"])
            != "done"
        )
        result = {
            "released": True,
            "issue_id": issue_id,
            "issue_status": issue["status"],
            "issue_still_open": still_open,
            # Always present on both branches: an absent field would make
            # "the clock had already released this" a defaulted meaning.
            "lapsed": lapsed,
            "next": _completion_next(report),
        }
        if submission is not None:
            result["candidate_submission"] = review_commands.insert_submission(
                conn, issue_id=issue_id, event_id=event["id"], submission=submission
            )
        elif review_id is not None:
            result["candidate_review"] = review_commands.insert_review(
                conn, issue_id=issue_id, event_id=event["id"], candidate_id=review_id
            )
        return result


def decline_delegation(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    issue_id: int,
    generation: object | None = None,
) -> list[dict]:
    """Decline a delegation handed to you (decline): remove YOURSELF from the contributor
    set so the work is visibly refused, not silently dropped, and can be re-routed. Returns
    the remaining contributor list. Self-service — the actor removes only itself; rejects
    with IssueCommandError('not_found') if the actor was not a delegated contributor. Any
    lease the actor held on the issue is released with the same act (giving up the
    delegation gives up the claim)."""
    actor = _require_issue_writer(actor)
    with db.transaction(conn, immediate=True):
        _visible_issue(conn, actor, issue_id)
        if not contributors_data.is_contributor(conn, issue_id, actor["id"]):
            raise IssueCommandError(
                "not_found", "you are not a delegated contributor on this issue"
            )
        held = leases.get_lease(conn, issue_id)
        released_generation = None
        if held is not None and held["holder_id"] == actor["id"]:
            if held["active"]:
                released_generation = _matching_lease_generation(
                    held, generation, actor=actor
                )
            else:
                # An expired row is not a current possession. A generationless
                # decline releases our expired row using the server-observed
                # generation under this immediate writer lock, never unfenced.
                if generation is not None:
                    released_generation = _matching_lease_generation(
                        held, generation, actor=actor
                    )
                else:
                    # The immediate writer lock makes the server-observed expired
                    # generation an exact fence; no replacement can appear between
                    # this read and deletion.
                    released_generation = held["generation"]
        elif generation is not None:
            _normalize_lease_generation(generation, required=True)
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation is stale; fetch the current lease before retrying",
            )
        if not contributors_data.remove_contributor(
            conn, issue_id, actor["id"], commit=False
        ):
            raise RuntimeError("delegation disappeared inside its write transaction")
        if released_generation is not None and not leases.delete_lease(
            conn, issue_id, released_generation, commit=False
        ):
            raise IssueCommandError(
                "lease_generation_mismatch",
                "lease generation changed before release",
            )
        issue_activity.record_delegation_declined(
            conn,
            actor_id=actor["id"],
            issue_id=issue_id,
            released_generation=released_generation,
            commit=False,
        )
        return contributors_data.list_contributors(conn, issue_id)


def account_active_leases(
    conn: sqlite3.Connection, actor: dict, holder_id: int
) -> list[dict]:
    """The holder's active leases on issues ``actor`` may see, by issue id:
    what pausing, revoking or offboarding the holder names (C8). A lease on an
    issue the actor cannot see is left out, as a hidden issue reads as missing
    everywhere. Read inside the act's transaction; writes nothing."""
    listed = []
    for lease in leases.live_leases_at(conn):
        if lease["holder_id"] != holder_id:
            continue
        issue = issues.get_issue(conn, int(lease["issue_id"]))
        if issue is None or not access.can_see_issue(conn, actor, issue["id"]):
            continue
        listed.append(
            {
                "issue_id": issue["id"],
                "key": issue.get("key"),
                "title": issue["title"],
                "generation": lease["generation"],
                "expires_at": lease["expires_at"],
            }
        )
    return listed


def release_account_lease(conn: sqlite3.Connection, actor: dict, lease: dict) -> dict:
    """Release one lease :func:`account_active_leases` named, as ``actor``,
    through :func:`complete_claim`: the command an admin's release of one lease
    uses (U3), fenced by the generation the list read, and recorded as
    ``lease_released`` naming the holder. Composed inside the act's
    transaction, so a refusal rolls the whole act back."""
    return complete_claim(
        conn,
        actor=actor,
        issue_id=int(lease["issue_id"]),
        generation=lease["generation"],
    )
