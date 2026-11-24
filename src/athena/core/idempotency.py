"""Durable ownership and replay records for mutating API requests.

The ASGI middleware owns authentication, request buffering, and response replay.
This module owns the SQLite state machine. Transactions here are intentionally
short: an executing row is committed before the route runs, because the route
uses its own connection and must not wait behind an idempotency write lock.

That separation has one unavoidable consequence. If a worker disappears after a
domain commit but before it stores the response, Athena cannot know whether the
mutation happened. Such a key is therefore indeterminate and is never taken over
automatically. Failing closed is less convenient than guessing, but it cannot
silently apply the same mutation twice.

The authorization fence
-----------------------

What the fence is for: a replayed key should not return a stored response its
caller could not obtain by making the request now. A replay is always stale (the
issue may have been edited since); what it must not be is unauthorized: the same
request, made now by the same caller, should still be allowed, and nothing the
stored response exposes should have become unreadable to that caller. Database
triggers move counters on the acts listed below (0043, 0061, 0090); a receipt
whose captured counters differ is purged and answers
``idempotency_authorization_changed``. The fence fails closed: an act that might
narrow is fenced even when it widens. It holds for the acts listed here and no
others; the exceptions are listed last.

The counters. ``revision`` (0043) moves on EVERY act below. A process still
running the old code compares only ``revision``, so it fails closed on all of
them, whatever order processes restart in. ``issue_authority_revision`` moves,
with ``revision``, on every per-issue act, so ``revision - issue_authority_revision``
(``access`` in ``_counters``) moves on the workspace-wide acts alone. Each issue's
own counter (``idempotency_issue_authority``) moves on that issue's per-issue acts.
A receipt compares ``revision`` (scope ``workspace``), ``access`` and its issue's
counter (scope ``issue``) or ``access`` alone (scope ``none``).

Which stored responses each act can invalidate (F6, decision D17):

* **Every receipt** (workspace-wide acts). A user's id, role, pause state or
  ``is_agent``, or the user's deletion (role makes an admin, who reads and writes
  everything; a viewer writes nothing; pause refuses every action; an agent may
  not close a blocked issue under the blocked-close policy, even with the
  override). A token's principal, hash or scopes, or its deletion. A project's or
  space's visibility or owner, or its deletion; a project's blocked-close policy;
  a project or space membership removed or moved (these decide who reads every
  issue and page inside, and any response may embed one: a parent, a link
  target, a blocker, a packet, a search hit, the ``similar_open`` of a create).
  An approval gate added, removed or rewritten for an account (a gated close
  answers 202 ``approval_required``). An action budget set, or its limit or
  window changed. A page's space, a sprint's project, a comment's issue or
  author, a page comment's page or author, an attachment's target or uploader, a
  filter's, notification's or watch's owner or target. And an issue's
  **placement**: its ``project_id`` decides who can read it, and it is embedded
  in other issues' answers (as a parent, a link, a blocker, a child, a similar
  issue), so moving one issue can make a receipt for a write on a *different*
  issue expose what its caller may no longer read. An issue's ``id`` is a
  workspace-wide act too, because the per-issue scope is keyed by it.

* **The receipts of one issue** (per-issue acts): an issue's ``assignee_id`` or
  ``created_by`` changing, or one of its contributors removed or moved (a
  ``DELETE /contributors`` or a decline). The argument has two halves, and both
  are checked by ``tests/test_idempotency_issue_fence.py``:

  1. *Reads.* No visibility rule reads an issue's assignee, creator or
     contributors. ``core/access.py`` decides visibility from a project's or
     space's visibility, owner and members and from an issue's ``project_id``;
     the "mine" selections (the activity feed's, the desk's, the ready queue's)
     choose among issues the caller can already read and never admit one.
     Assignment grants no visibility, not in a private project either (an
     assignee who is not a member still reads nothing there), and an issue's
     creator is not its project's owner. So no per-issue act can make any stored
     response, whatever issues it embeds, expose something its caller can no
     longer read. A response that embeds another issue's assignee name shows a
     fact the caller can still read on that issue's trail.
  2. *Writes.* An issue's assignee, creator and contributors are read only by
     the write gates of that same issue: ``issues.can_act_on`` (creator,
     assignee, delegated contributor or admin), the claimant gate in
     ``lease_commands`` (assignee, contributor or admin) and the reviewer gate in
     ``review_request_commands`` (the review issue's assignee or a contributor).
     Every route in ``ISSUE_SCOPED_ROUTES`` gates on the issue its path names and
     checks every other issue it touches (a parent, a link target, a mention
     target, a review candidate) for visibility only. So for those receipts the
     only issue whose assignee, creator or contributors decided "allowed" is the
     path's issue.

  A receipt can be scoped only where its route names, before it runs, every
  issue whose assignee, creator or contributors its authorization read.
  ``POST /issues`` (``ISSUE_INDEPENDENT_ROUTES``) reads only the issue it
  creates, whose creator is the caller by construction: a fresh create makes a
  new issue with the caller as its creator, so no later change to the first one
  can refuse it, and its receipt depends on no issue's. Everything else is scope
  ``workspace``, compared on ``revision``, so a per-issue act anywhere fences it
  as before: ``POST /issues/bulk`` (many issues), ``POST /issues/claim-next``
  (its issue is chosen by the ready rule, which reads assignees, and is unknown
  from the path), every route outside ``/issues``, and any route added under
  ``/issues/{id}`` that nobody has reviewed: the lists below are allow-lists,
  not patterns.

  A write that is itself a per-issue act fences its own receipt: its fresh
  answer is sent once and its key then refuses. A keyed ``PUT /assignee`` that
  hands the issue away always did this; a keyed decline now does too.

* **None** (the exceptions; these move no counter). An issue's parent, sprint,
  status, text, blockers or lease; a contributor added; a membership, gate or
  budget removed only widens. And spending an action budget: every metered write
  spends, and a replay applies nothing and is not metered, so a receipt still
  replays after its caller's budget is spent while a fresh request answers 429
  ``agent_budget_exhausted``. A replay is also stale about state by design: a
  fresh request may meet a 409 or 422 about the target as it is now (a lease
  someone else holds, a close the policy now refuses because a blocker was
  added), which the replay does not re-evaluate.

A fenced marker expires when its receipt would have (``expires_at`` carried
over from the receipt, or the receipt's TTL from the moment it is fenced while
still executing). After that the key is a key never seen: the same request
under it runs again. That is the retention a completed receipt already had; a
fenced key gains no window for a duplicate that its receipt did not have.
Markers fenced before 0090 keep no expiry, because their receipt's expiry was
discarded with the response.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import sqlite3
import time

from athena.core import db, tokens


LEGACY_FINGERPRINT = "legacy-fingerprint-unavailable"

#: Keyed routes whose receipt depends on the assignee and creator of the issue the
#: path names, and of no other issue (see the module docstring). Each was read
#: one by one; ``tests/test_idempotency_issue_fence.py`` fails when a listed route
#: no longer exists. A route missing from this list keeps the workspace fence.
ISSUE_SCOPED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("PATCH", "/issues/{issue_id}"),
        ("POST", "/issues/{issue_id}/archive"),
        ("POST", "/issues/{issue_id}/unarchive"),
        ("PUT", "/issues/{issue_id}/assignee"),
        ("PUT", "/issues/{issue_id}/project"),
        ("PUT", "/issues/{issue_id}/sprint"),
        ("PUT", "/issues/{issue_id}/parent"),
        ("POST", "/issues/{issue_id}/attachments"),
        ("POST", "/issues/{issue_id}/comments"),
        ("PATCH", "/issues/{issue_id}/comments/{comment_id}"),
        ("DELETE", "/issues/{issue_id}/comments/{comment_id}"),
        ("POST", "/issues/{issue_id}/labels"),
        ("DELETE", "/issues/{issue_id}/labels/{label_id}"),
        ("POST", "/issues/{issue_id}/links"),
        ("DELETE", "/issues/{issue_id}/links/{relation}/{target_id}"),
        ("POST", "/issues/{issue_id}/link-mention"),
        ("POST", "/issues/{issue_id}/contributors"),
        ("DELETE", "/issues/{issue_id}/contributors/{user_id}"),
        ("POST", "/issues/{issue_id}/delegate"),
        ("POST", "/issues/{issue_id}/decline"),
        ("POST", "/issues/{issue_id}/claim"),
        ("POST", "/issues/{issue_id}/complete"),
        ("POST", "/issues/{issue_id}/yield"),
        ("POST", "/issues/{issue_id}/claim-handoffs/{handoff_token}/resume"),
        ("POST", "/issues/{issue_id}/dispatch"),
        ("POST", "/issues/{issue_id}/learnings"),
        ("POST", "/issues/{review_issue_id}/review-requests"),
    }
)

#: Keyed routes whose receipt depends on no issue's assignee or creator.
ISSUE_INDEPENDENT_ROUTES: frozenset[tuple[str, str]] = frozenset({("POST", "/issues")})

# A canonical row id: what the scope is keyed by. Any other spelling of the path's
# issue (a leading zero, a sign, a non-ASCII digit) keeps the workspace fence.
_ROW_ID = "([1-9][0-9]{0,17})"
_PARAM = re.compile(r"\{[a-z_]+\}")


def _route_pattern(template: str) -> re.Pattern[str]:
    """The template's first parameter captures the issue; the rest match a segment."""
    parts = _PARAM.split(template)
    params = _PARAM.findall(template)
    pattern = re.escape(parts[0])
    for index, literal in enumerate(parts[1:]):
        pattern += (_ROW_ID if index == 0 else "[^/]+") + re.escape(literal)
    assert len(params) == len(parts) - 1
    return re.compile(pattern)


_SCOPED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, _route_pattern(template))
    for method, template in sorted(ISSUE_SCOPED_ROUTES)
)


@dataclass(frozen=True)
class ReceiptScope:
    """Which issue assignee/creator changes fence a receipt: any (``workspace``),
    one issue's (``issue``) or none (``none``)."""

    kind: str
    issue_id: int | None = None


WORKSPACE_SCOPE = ReceiptScope("workspace")


def receipt_scope(method: str, path: str) -> ReceiptScope:
    """The scope a keyed request's receipt is fenced under, from its method and
    route path alone (the claim is made before the route runs)."""
    if (method, path) in ISSUE_INDEPENDENT_ROUTES:
        return ReceiptScope("none")
    for scoped_method, pattern in _SCOPED_PATTERNS:
        if method == scoped_method:
            match = pattern.fullmatch(path)
            if match is not None:
                return ReceiptScope("issue", int(match.group(1)))
    return WORKSPACE_SCOPE


@dataclass(frozen=True)
class ClaimResult:
    """The durable decision for one claim/read attempt."""

    kind: str
    record: dict | None = None


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else int(value)


def _record(conn: sqlite3.Connection, *, key: str, identity: str) -> dict | None:
    row = conn.execute(
        "SELECT request_fingerprint, authorization_revision, authority_scope, "
        "scope_issue_id, access_revision, issue_authority_revision, method, path, "
        "state, owner_token, lease_expires_at, status_code, content_type, "
        "response_headers, "
        "response_body, failure_code, created_at, updated_at, expires_at "
        "FROM idempotency_keys WHERE idempotency_key = ? AND identity = ?",
        (key, identity),
    ).fetchone()
    return dict(row) if row is not None else None


def _legacy_actor_alias_record(
    conn: sqlite3.Connection, *, key: str, identity: str
) -> dict | None:
    """Canonicalize an old actor identity using Python's original int semantics.

    V1 accepted spellings such as actor:01, actor:+1, surrounding whitespace, and
    underscores. SQLite CAST does not match Python int for all of them, so migration
    SQL alone cannot safely collapse aliases. This runs under the claim transaction.
    """
    if not identity.startswith("actor:"):
        return None
    try:
        wanted_id = int(identity.removeprefix("actor:"))
    except ValueError:
        return None

    aliases = []
    for row in conn.execute(
        "SELECT identity FROM idempotency_keys "
        "WHERE idempotency_key = ? AND substr(identity, 1, 6) = 'actor:'",
        (key,),
    ):
        try:
            if int(row["identity"].removeprefix("actor:")) == wanted_id:
                aliases.append(row["identity"])
        except ValueError:
            continue
    if not aliases:
        return None
    if identity in aliases:
        for alias in aliases:
            if alias != identity:
                conn.execute(
                    "DELETE FROM idempotency_keys "
                    "WHERE idempotency_key = ? AND identity = ?",
                    (key, alias),
                )
        return _record(conn, key=key, identity=identity)

    chosen = sorted(aliases)[0]
    conn.execute(
        "UPDATE idempotency_keys SET identity = ? "
        "WHERE idempotency_key = ? AND identity = ?",
        (identity, key, chosen),
    )
    for alias in aliases:
        if alias != chosen:
            conn.execute(
                "DELETE FROM idempotency_keys "
                "WHERE idempotency_key = ? AND identity = ?",
                (key, alias),
            )
    return _record(conn, key=key, identity=identity)


def _authorization_changed(
    conn: sqlite3.Connection, *, key: str, identity: str, expires_at: int
) -> None:
    """Purge a stale response and keep a fail-closed marker until ``expires_at``,
    the instant its receipt would have expired."""
    conn.execute(
        "UPDATE idempotency_keys SET "
        "state = 'indeterminate', owner_token = NULL, lease_expires_at = NULL, "
        "status_code = NULL, content_type = NULL, response_headers = NULL, "
        "response_body = NULL, failure_code = 'authorization_changed', "
        "updated_at = datetime('now'), expires_at = ? "
        "WHERE idempotency_key = ? AND identity = ?",
        (int(expires_at), key, identity),
    )


def _counters(conn: sqlite3.Connection, issue_id: int | None) -> dict[str, int]:
    """Every counter a receipt may be compared on, read in the caller's
    transaction (migrations 0043 and 0090):

    * ``revision`` moves on every act, workspace-wide or per-issue. Old code
      compares only this, and the new code compares it for scope ``workspace``.
    * ``access`` is ``revision`` minus the workspace-wide count of per-issue
      acts: it moves on the workspace-wide acts only.
    * ``issue`` is ``issue_id``'s own counter of per-issue acts (0 until its
      first one, or with no issue).
    """
    state = conn.execute(
        "SELECT revision, issue_authority_revision "
        "FROM idempotency_authorization_state WHERE singleton = 1"
    ).fetchone()
    issue = 0
    if issue_id is not None:
        row = conn.execute(
            "SELECT revision FROM idempotency_issue_authority WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        issue = row["revision"] if row is not None else 0
    return {
        "revision": state["revision"],
        "access": state["revision"] - state["issue_authority_revision"],
        "issue": issue,
    }


def _fenced(conn: sqlite3.Connection, record: dict) -> bool:
    """Whether authorization may have narrowed since ``record`` was claimed."""
    scope = record["authority_scope"]
    now = _counters(conn, record["scope_issue_id"])
    if scope == "workspace":
        return now["revision"] != record["authorization_revision"]
    if scope == "issue":
        return (now["access"], now["issue"]) != (
            record["access_revision"],
            record["issue_authority_revision"],
        )
    if scope == "none":
        return now["access"] != record["access_revision"]
    raise ValueError(f"unknown receipt scope: {scope!r}")


def _expired(record: dict, current: int) -> bool:
    """A completed receipt, or a marker fenced from one, past its expiry."""
    return record["expires_at"] is not None and int(record["expires_at"]) <= current


def claim_or_read(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    method: str,
    path: str,
    request_fingerprint: str,
    lease_seconds: int,
    now: int | None = None,
    earlier_fingerprints: tuple[str, ...] = (),
) -> ClaimResult:
    """Atomically become the owner, observe a result, or get a safe refusal.

    BEGIN IMMEDIATE serializes the read/insert decision across processes. No
    transaction survives this function, so the winning route can write using its
    normal connection. A live executing owner is observed, never replaced.

    A new receipt stores ``request_fingerprint``. A stored one matches the
    request when it holds that or one of ``earlier_fingerprints``: the same
    request as an earlier release fingerprinted it, so a retry still finds its
    receipt across an upgrade that changes what a key is matched on.
    """
    current = _now(now)
    with db.transaction(conn, immediate=True):
        record = _legacy_actor_alias_record(conn, key=key, identity=identity)
        if record is None:
            record = _record(conn, key=key, identity=identity)

        if record is not None:
            # V1 receipts cannot prove that the retry body is the original body.
            # Their key remains an explicit ambiguity marker after aliases collapse.
            if record["request_fingerprint"] == LEGACY_FINGERPRINT:
                if record["method"] != method or record["path"] != path:
                    return ClaimResult("mismatch", record)
                return ClaimResult("indeterminate", record)
            if _expired(record, current):
                # A completed receipt, or the marker fenced from one, past the
                # instant the receipt would have expired: the key is unused again.
                # The target key must expire even when more than the cleanup
                # batch's worth of older rows exist.
                conn.execute(
                    "DELETE FROM idempotency_keys "
                    "WHERE idempotency_key = ? AND identity = ?",
                    (key, identity),
                )
                record = None

        if record is not None:
            if record["state"] == "indeterminate":
                kind = (
                    "authorization_changed"
                    if record["failure_code"] == "authorization_changed"
                    else "indeterminate"
                )
                return ClaimResult(kind, record)
            if _fenced(conn, record):
                if record["state"] == "executing":
                    return ClaimResult("authorization_changed", record)
                # Authorization may have narrowed since the claim. Purge the stored
                # body and keep the key fail-closed until the receipt would have
                # expired; neither replay nor re-execution is safe before then.
                _authorization_changed(
                    conn, key=key, identity=identity, expires_at=record["expires_at"]
                )
                return ClaimResult(
                    "authorization_changed",
                    _record(conn, key=key, identity=identity),
                )

        # Lazy, bounded retention for other keys: completed receipts and the markers
        # fenced from them. Executing and other indeterminate rows carry no expiry
        # because their mutation outcome may be unknown.
        conn.execute(
            "DELETE FROM idempotency_keys WHERE rowid IN ("
            "SELECT rowid FROM idempotency_keys "
            "WHERE expires_at IS NOT NULL AND expires_at <= ? LIMIT 100)",
            (current,),
        )

        if record is None:
            scope = receipt_scope(method, path)
            counters = _counters(conn, scope.issue_id)
            owner_token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO idempotency_keys ("
                "idempotency_key, identity, request_fingerprint, "
                "authorization_revision, authority_scope, scope_issue_id, "
                "access_revision, issue_authority_revision, method, path, state, "
                "owner_token, lease_expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'executing', ?, ?)",
                (
                    key,
                    identity,
                    request_fingerprint,
                    counters["revision"],
                    scope.kind,
                    scope.issue_id,
                    counters["access"],
                    counters["issue"],
                    method,
                    path,
                    owner_token,
                    current + max(1, int(lease_seconds)),
                ),
            )
            return ClaimResult("owner", {"owner_token": owner_token})

        if record["request_fingerprint"] not in (
            request_fingerprint,
            *earlier_fingerprints,
        ):
            return ClaimResult("mismatch", record)
        if (
            record["state"] == "executing"
            and int(record["lease_expires_at"]) <= current
        ):
            # Do not rewrite or steal the row. A slow original owner may still
            # finish and publish its fenced result; a dead one remains fail-closed.
            return ClaimResult("indeterminate", record)
        if record["state"] == "completed":
            return ClaimResult("replay", record)
        return ClaimResult("in_progress", record)


def complete(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    owner_token: str,
    status_code: int,
    content_type: str | None,
    response_headers: str,
    response_body: bytes,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    """Publish a bounded 2xx response iff this caller still owns the claim.

    This receipt is a second transaction, separate from the domain write, on
    purpose. ``claim_or_read`` commits the executing row on the middleware's
    connection before the handler runs, so a concurrent retry can see the
    single-flight claim. Folding the receipt into the domain transaction would
    hide that claim until commit (both retries execute) or hold SQLite's writer
    lock for the whole request. A crash between those two commits leaves the
    key executing and fail-closed; this function never steals it.

    Returns completed, authorization_changed, or lost. An authorization change
    during route execution purges the replay body but still lets middleware send
    this fresh response; subsequent retries remain fail-closed until the receipt
    would have expired (``ttl_seconds`` from now).
    """
    current = _now(now)
    expires_at = current + max(1, int(ttl_seconds))
    with db.transaction(conn, immediate=True):
        row = conn.execute(
            "SELECT authorization_revision, authority_scope, scope_issue_id, "
            "access_revision, issue_authority_revision FROM idempotency_keys "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (key, identity, owner_token),
        ).fetchone()
        if row is None:
            return "lost"
        if _fenced(conn, dict(row)):
            _authorization_changed(
                conn, key=key, identity=identity, expires_at=expires_at
            )
            return "authorization_changed"

        cur = conn.execute(
            "UPDATE idempotency_keys SET "
            "state = 'completed', owner_token = NULL, lease_expires_at = NULL, "
            "status_code = ?, content_type = ?, response_headers = ?, "
            "response_body = ?, failure_code = NULL, updated_at = datetime('now'), "
            "expires_at = ? "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (
                status_code,
                content_type,
                response_headers,
                response_body,
                expires_at,
                key,
                identity,
                owner_token,
            ),
        )
        return "completed" if cur.rowcount == 1 else "lost"


def release(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    owner_token: str,
) -> bool:
    """Release a claim after an ordinary non-5xx response, fenced by owner."""
    with db.transaction(conn, immediate=True):
        cur = conn.execute(
            "DELETE FROM idempotency_keys "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (key, identity, owner_token),
        )
        return cur.rowcount == 1


def mark_indeterminate(
    conn: sqlite3.Connection,
    *,
    key: str,
    identity: str,
    owner_token: str,
    failure_code: str,
) -> bool:
    """Fence a key after an outcome that may have followed a committed write."""
    with db.transaction(conn, immediate=True):
        cur = conn.execute(
            "UPDATE idempotency_keys SET "
            "state = 'indeterminate', owner_token = NULL, lease_expires_at = NULL, "
            "failure_code = ?, updated_at = datetime('now') "
            "WHERE idempotency_key = ? AND identity = ? "
            "AND state = 'executing' AND owner_token = ?",
            (failure_code, key, identity, owner_token),
        )
        return cur.rowcount == 1


#: Keys are 1 to 255 visible ASCII characters, as the header is validated.
MAX_KEY_BYTES = 255

#: What a key's owner can read about it (A11), each with what a retry of the
#: same request would meet.
KEY_STATES = (
    "unused",
    "in_progress",
    "completed",
    "indeterminate",
    "authorization_changed",
)


def token_identity(raw_token: str) -> str:
    """The identity a bearer token's keys are stored under: the credential,
    not the account, so one account's tokens never read each other's keys."""
    return "tok:" + tokens._hash(raw_token)


def actor_identity(actor_id: int) -> str:
    """The identity of the trusted actor-header path."""
    return f"actor:{actor_id}"


def is_valid_key(key: str) -> bool:
    """Whether ``key`` could be an Idempotency-Key at all."""
    return (
        isinstance(key, str)
        and 0 < len(key) <= MAX_KEY_BYTES
        and all(0x21 <= ord(char) <= 0x7E for char in key)
    )


def key_state(
    conn: sqlite3.Connection, *, key: str, identity: str, now: int | None = None
) -> dict:
    """What a retry under ``key`` by ``identity`` would meet, read without
    changing anything (A11).

    The same decisions ``claim_or_read`` makes, in the same order, with none
    of its writes: an expired receipt reads ``unused`` and is not deleted, and
    a receipt whose authorization has changed reads ``authorization_changed``
    and keeps its stored answer until a real retry purges it. Whether a retry
    carries the same request cannot be known from the key alone, so a
    ``completed`` or ``in_progress`` key says what an identical retry would
    meet; a different request under it is refused ``idempotency_mismatch``.

    Only the key's own identity can ask: another credential's key under the
    same text is a different row and reads ``unused``.
    """
    current = _now(now)
    record = _record(conn, key=key, identity=identity)
    legacy = record is not None and record["request_fingerprint"] == LEGACY_FINGERPRINT
    if record is not None and not legacy and _expired(record, current):
        record = None
    if record is None:
        return {"state": "unused", "method": None, "path": None, "status_code": None}
    if legacy:
        state = "indeterminate"
    elif record["state"] == "indeterminate":
        state = (
            "authorization_changed"
            if record["failure_code"] == "authorization_changed"
            else "indeterminate"
        )
    elif _fenced(conn, record):
        state = "authorization_changed"
    elif record["state"] == "executing":
        state = (
            "in_progress"
            if int(record["lease_expires_at"]) > current
            else "indeterminate"
        )
    else:
        state = "completed"
    return {
        "state": state,
        "method": record["method"],
        "path": record["path"],
        "status_code": record["status_code"] if state == "completed" else None,
    }


def get_record(conn: sqlite3.Connection, *, key: str, identity: str) -> dict | None:
    """Inspection helper for diagnostics and state-machine tests."""
    return _record(conn, key=key, identity=identity)
