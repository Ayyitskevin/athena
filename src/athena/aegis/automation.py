"""The automation rules engine — "when event X, do Y" over the activity log.

A rule reacts to an activity event (a TRIGGER: a verb + target kind, optionally narrowed
by CONDITIONS on the target issue) and performs an ACTION. This is the internal, in-app
twin of webhooks: webhooks PUSH events to external URLs; a rule turns an event into an
in-app write (assign / label / set status / comment). It consumes the same append-only
`activity` log via a single cursor, exactly like webhooks.deliver_pending.

Concerns, separable for testing (mirroring core/webhooks.py):
  * data access — CRUD on automation_rules + the single cursor in automation_state;
  * _matches — whether one event fires one rule (verb/kind + JSON conditions);
  * process_pending — one pass: drain events after the cursor, run matching enabled
    rules through an INJECTED executor, advance the cursor.
The live background loop (a later slice) is a thin wrapper that injects the real action
executor; here the executor is a parameter so the matching/dispatch logic is unit-
testable without performing real writes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import re
import sqlite3

from athena import config
from athena.aegis import (
    comment_commands,
    contributors,
    issue_activity,
    issue_commands,
    issues,
)
from athena.core import activity, db, labels, run_context, users

logger = logging.getLogger(__name__)

# The actions a rule may take (dispatched on in execute_action). The boundary validates
# a rule's action_type is one of these; the expected action_params per type are:
#   assign / add_contributor -> {"user_id": int}
#   set_status               -> {"status": str}   (must be valid for the issue's project)
#   add_label                -> {"label": str}    (find-or-created by name)
#   comment                  -> {"body": str}
ACTION_TYPES = ("assign", "add_label", "set_status", "comment", "add_contributor")

# The issue fields a CONDITION may match against, resolved from the target issue at match
# time. Keeping the set explicit means a typo'd condition key is a boundary error, never
# a silent match-everything.
CONDITION_FIELDS = ("project_id", "status", "priority", "assignee_id", "sprint_id")
SCHEDULE_CONDITION_FIELDS = (*CONDITION_FIELDS, "inactive_for_seconds")

# The activity verbs a rule may TRIGGER on, for target_kind="issue" — every verb the
# issue lifecycle emits (see aegis/issue_activity.py), plus "*" for "any issue event".
# Validating against this closed set at the boundary means a typo'd verb is a 422, never
# a rule that silently never fires. The only target_kind rules act on yet is "issue".
TRIGGER_VERBS = (
    "created",
    "issue_edited",
    "changed_status",
    "changed_priority",
    "assigned",
    "unassigned",
    "changed_project",
    "removed_from_project",
    "moved_to_sprint",
    "removed_from_sprint",
    "set_parent",
    "removed_parent",
    "added_contributor",
    "removed_contributor",
    "labeled",
    "unlabeled",
    "commented",
    "comment_deleted",
    "archived",
    "unarchived",
)
TARGET_KINDS = ("issue",)
TRIGGER_TYPES = ("event", "schedule")
SCHEDULE_TRIGGER_VERB = "scheduled"

# Schedules intentionally stay small and single-process. Each pass claims only a
# bounded number of rules and executes only a bounded number of durable target
# occurrences. A target overflow fails the whole slot closed rather than performing a
# surprising partial sweep.
MIN_SCHEDULE_INTERVAL_SECONDS = 60
MAX_SCHEDULE_INTERVAL_SECONDS = 31_536_000
MAX_SCHEDULE_RULES_PER_PASS = 10
MAX_SCHEDULE_RULE_SCAN = 100
MAX_SCHEDULE_TARGETS_PER_FIRING = 50
MAX_SCHEDULE_ACTIONS_PER_PASS = 50
MAX_SCHEDULE_ATTEMPTS = 3
ONE_SHOT_CATCH_UP_SECONDS = 86_400

_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse only Athena's canonical, second-precision UTC representation.

    Local, naive, offset, and fractional timestamps are deliberately rejected. Fixed
    UTC instants keep interval grids deterministic and avoid DST-dependent behavior.
    """
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, _UTC_TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime(_UTC_TIMESTAMP_FORMAT)


def _validate_action_params(action_type: str, params: dict) -> str | None:
    """The shape an action_type's params must have, or an error string. A user_id/label/
    status/body that's missing or the wrong type would make the action a silent no-op at
    fire time (execute_action fails soft), so we reject it at creation instead."""
    if action_type in ("assign", "add_contributor"):
        uid = params.get("user_id")
        if not isinstance(uid, int) or isinstance(uid, bool):
            return f"{action_type} requires action_params.user_id (an integer)"
    elif action_type == "set_status":
        status = params.get("status")
        if not isinstance(status, str) or not status.strip():
            return "set_status requires action_params.status (a non-empty string)"
    elif action_type == "add_label":
        label = params.get("label")
        if not isinstance(label, str) or not label.strip():
            return "add_label requires action_params.label (a non-empty string)"
    elif action_type == "comment":
        body = params.get("body")
        if not isinstance(body, str) or not body.strip():
            return "comment requires action_params.body (a non-empty string)"
    return None


def validate_rule(
    *,
    trigger_verb: str,
    action_type: str,
    conditions: dict,
    action_params: dict,
    target_kind: str = "issue",
    trigger_type: str = "event",
    schedule_at: str | None = None,
    schedule_every_seconds: int | None = None,
) -> str | None:
    """Whether a rule spec is well-formed: return a human error string, or None if valid.
    The ONE validator both write surfaces share — the REST API (slice 3) and the web form
    (slice 4) — so a malformed rule is rejected the same way on both, never persisted as a
    row that can't ever fire (typo'd verb/condition key) or whose action no-ops every time
    (missing param). The data layer itself only persists; this is the boundary contract."""
    if target_kind not in TARGET_KINDS:
        return f"target_kind must be one of: {', '.join(TARGET_KINDS)}"
    if trigger_type not in TRIGGER_TYPES:
        return f"trigger_type must be one of: {', '.join(TRIGGER_TYPES)}"
    allowed_conditions: tuple[str, ...]
    if trigger_type == "event":
        if trigger_verb != "*" and trigger_verb not in TRIGGER_VERBS:
            return f"trigger_verb must be '*' or one of: {', '.join(TRIGGER_VERBS)}"
        if schedule_at is not None or schedule_every_seconds is not None:
            return "event rules cannot include schedule fields"
        allowed_conditions = CONDITION_FIELDS
    else:
        if trigger_verb != SCHEDULE_TRIGGER_VERB:
            return f"schedule rules must use trigger_verb '{SCHEDULE_TRIGGER_VERB}'"
        if _parse_utc_timestamp(schedule_at) is None:
            return "schedule_at must be canonical UTC (YYYY-MM-DDTHH:MM:SSZ)"
        if schedule_every_seconds is not None and (
            not isinstance(schedule_every_seconds, int)
            or isinstance(schedule_every_seconds, bool)
            or not (
                MIN_SCHEDULE_INTERVAL_SECONDS
                <= schedule_every_seconds
                <= MAX_SCHEDULE_INTERVAL_SECONDS
            )
        ):
            return (
                "schedule_every_seconds must be null or an integer between "
                f"{MIN_SCHEDULE_INTERVAL_SECONDS} and "
                f"{MAX_SCHEDULE_INTERVAL_SECONDS}"
            )
        inactive_for = conditions.get("inactive_for_seconds")
        if inactive_for is not None and (
            not isinstance(inactive_for, int)
            or isinstance(inactive_for, bool)
            or not (
                MIN_SCHEDULE_INTERVAL_SECONDS
                <= inactive_for
                <= MAX_SCHEDULE_INTERVAL_SECONDS
            )
        ):
            return (
                "inactive_for_seconds must be an integer between "
                f"{MIN_SCHEDULE_INTERVAL_SECONDS} and "
                f"{MAX_SCHEDULE_INTERVAL_SECONDS}"
            )
        for field, value in conditions.items():
            if field in ("project_id", "assignee_id", "sprint_id") and (
                value is not None
                and (not isinstance(value, int) or isinstance(value, bool))
            ):
                return f"{field} must be an integer or null"
            if field in ("status", "priority") and (
                value is not None and not isinstance(value, str)
            ):
                return f"{field} must be a string or null"

        allowed_conditions = SCHEDULE_CONDITION_FIELDS
    if action_type not in ACTION_TYPES:
        return f"action_type must be one of: {', '.join(ACTION_TYPES)}"
    for key in conditions:
        if key not in allowed_conditions:
            return (
                f"unknown condition field '{key}'; "
                f"allowed: {', '.join(allowed_conditions)}"
            )
    return _validate_action_params(action_type, action_params)


_COLS = (
    "id, name, enabled, trigger_verb, target_kind, conditions, action_type, "
    "action_params, created_by, created_at, failure_count, last_error, last_error_at, "
    "trigger_type, schedule_at, schedule_every_seconds, next_scheduled_at, "
    "last_scheduled_for, schedule_missed_count, last_schedule_target_count, "
    "last_schedule_overflow_count"
)


def _row(row: sqlite3.Row) -> dict:
    """A rule row with its JSON blobs parsed and enabled coerced to bool, so callers
    work with a plain dict (the saved_filters criteria pattern)."""
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    json_errors: list[str] = []
    for field in ("conditions", "action_params"):
        try:
            parsed = json.loads(d[field])
        except (TypeError, json.JSONDecodeError):
            parsed = {}
            json_errors.append(f"{field} is not valid JSON")
        if not isinstance(parsed, dict):
            parsed = {}
            json_errors.append(f"{field} must be a JSON object")
        d[field] = parsed
    d["_json_errors"] = json_errors
    return d


def _configuration_error(rule: dict) -> str | None:
    errors = list(rule.get("_json_errors", []))
    if rule["trigger_type"] == "schedule":
        error = validate_rule(
            trigger_verb=rule["trigger_verb"],
            action_type=rule["action_type"],
            conditions=rule["conditions"],
            action_params=rule["action_params"],
            target_kind=rule["target_kind"],
            trigger_type=rule["trigger_type"],
            schedule_at=rule["schedule_at"],
            schedule_every_seconds=rule["schedule_every_seconds"],
        )
        if error is not None:
            errors.append(error)
        if rule["next_scheduled_at"] is not None and (
            _parse_utc_timestamp(rule["next_scheduled_at"]) is None
        ):
            errors.append(
                "next_scheduled_at must be canonical UTC (YYYY-MM-DDTHH:MM:SSZ)"
            )
    return "; ".join(errors) if errors else None


def _decorate_rule(
    conn: sqlite3.Connection, rule: dict, *, now: datetime | None = None
) -> dict:
    """Add bounded schedule progress/health without letting malformed rows crash reads."""
    rule["configuration_error"] = _configuration_error(rule)
    rule.pop("_json_errors", None)
    rule["schedule_pending_count"] = 0
    rule["schedule_failed_count"] = 0
    rule["last_schedule_state"] = None
    if rule["trigger_type"] != "schedule":
        rule["schedule_status"] = "event"
        return rule

    progress = conn.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN o.state = 'pending' THEN 1 ELSE 0 END), 0) "
        "AS pending_count, "
        "COALESCE(SUM(CASE WHEN o.state = 'failed' THEN 1 ELSE 0 END), 0) "
        "AS failed_count "
        "FROM automation_schedule_occurrences o "
        "JOIN automation_schedule_firings f ON f.id = o.firing_id "
        "WHERE f.rule_id = ?",
        (rule["id"],),
    ).fetchone()
    latest = conn.execute(
        "SELECT state FROM automation_schedule_firings WHERE rule_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (rule["id"],),
    ).fetchone()
    rule["schedule_pending_count"] = int(progress["pending_count"])
    rule["schedule_failed_count"] = int(progress["failed_count"])
    rule["last_schedule_state"] = latest["state"] if latest else None

    if rule["configuration_error"] is not None:
        rule["schedule_status"] = "malformed"
    elif not rule["enabled"]:
        rule["schedule_status"] = "disabled"
    elif (
        rule["schedule_failed_count"] > 0
        or rule["last_schedule_state"] == "failed"
        or (rule["schedule_pending_count"] > 0 and rule["failure_count"] > 0)
    ):
        rule["schedule_status"] = "failing"
    elif rule["schedule_pending_count"] > 0:
        rule["schedule_status"] = "processing"
    elif rule["next_scheduled_at"] is None:
        rule["schedule_status"] = (
            "completed" if rule["last_scheduled_for"] is not None else "malformed"
        )
    else:
        current = now or _utc_now()
        due = _parse_utc_timestamp(rule["next_scheduled_at"])
        assert due is not None
        if due > current:
            rule["schedule_status"] = "scheduled"
        elif due == current:
            rule["schedule_status"] = "due"
        else:
            rule["schedule_status"] = "overdue"
    return rule


# --- data access ------------------------------------------------------------


def create_rule(
    conn: sqlite3.Connection,
    *,
    name: str,
    trigger_verb: str,
    action_type: str,
    created_by: int,
    conditions: dict | None = None,
    action_params: dict | None = None,
    target_kind: str = "issue",
    trigger_type: str = "event",
    schedule_at: str | None = None,
    schedule_every_seconds: int | None = None,
    commit: bool = True,
) -> dict:
    """Insert a rule and return it. conditions/action_params are stored as JSON text.
    The boundary validates trigger_verb/action_type/condition keys; this layer persists.
    Raises sqlite3.IntegrityError if created_by isn't a real user (the FK). ``commit=False``
    lets an audited command fold the insert and its activity event into one transaction."""
    cur = conn.execute(
        "INSERT INTO automation_rules "
        "(name, trigger_verb, target_kind, conditions, action_type, action_params, "
        "created_by, trigger_type, schedule_at, schedule_every_seconds, "
        "next_scheduled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            trigger_verb,
            target_kind,
            json.dumps(conditions or {}),
            action_type,
            json.dumps(action_params or {}),
            created_by,
            trigger_type,
            schedule_at,
            schedule_every_seconds,
            schedule_at if trigger_type == "schedule" else None,
        ),
    )
    rule_id = cur.lastrowid
    assert rule_id is not None
    if commit:
        conn.commit()
    rule = get_rule(conn, rule_id)
    assert rule is not None
    return rule


def get_rule(conn: sqlite3.Connection, rule_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM automation_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    return _decorate_rule(conn, _row(row)) if row else None


def list_rules(
    conn: sqlite3.Connection,
    *,
    enabled_only: bool = False,
    failing_only: bool = False,
) -> list[dict]:
    """Every rule (or just the enabled ones), in id order — the order they fire."""
    sql = f"SELECT {_COLS} FROM automation_rules"
    clauses: list[str] = []
    if enabled_only:
        clauses.append("enabled = 1")
    if failing_only:
        clauses.append("failure_count > 0")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    return [_decorate_rule(conn, _row(r)) for r in conn.execute(sql).fetchall()]


def set_enabled(
    conn: sqlite3.Connection, rule_id: int, enabled: bool, *, commit: bool = True
) -> dict | None:
    """Switch a rule on/off WITHOUT deleting it (the pause twin of a webhook's active
    flag). Returns the updated rule, or None if there is no such rule. ``commit=False``
    lets an audited command fold the flip and its activity event into one transaction."""
    cur = conn.execute(
        "UPDATE automation_rules SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, rule_id),
    )
    if commit:
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_rule(conn, rule_id)


def delete_rule(conn: sqlite3.Connection, rule_id: int, *, commit: bool = True) -> bool:
    """Delete a rule. Returns True if one was removed, False if there was no such row.
    ``commit=False`` lets an audited command fold the delete and its activity event
    into one transaction."""
    conn.execute(
        "UPDATE automation_schedule_occurrences SET state = 'cancelled' "
        "WHERE state = 'pending' AND firing_id IN "
        "(SELECT id FROM automation_schedule_firings WHERE rule_id = ?)",
        (rule_id,),
    )
    conn.execute(
        "UPDATE automation_schedule_firings "
        "SET state = 'cancelled', completed_at = datetime('now') "
        "WHERE rule_id = ? AND state = 'pending'",
        (rule_id,),
    )
    cur = conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
    if commit:
        conn.commit()
    return cur.rowcount > 0


# --- matching ---------------------------------------------------------------


def _matches(conn: sqlite3.Connection, rule: dict, event: dict) -> bool:
    """Whether `event` fires `rule`. Three gates: the target kind, the trigger verb
    ('*' = any), and EVERY condition (an issue-field equality resolved from the current
    target issue). A condition on a vanished target fails closed (no match)."""
    if event["target_kind"] != rule["target_kind"]:
        return False
    if rule["trigger_verb"] != "*" and event["verb"] != rule["trigger_verb"]:
        return False
    conditions = rule["conditions"]
    if not conditions:
        return True
    issue = issues.get_issue(conn, event["target_id"])
    if issue is None:
        return False
    return all(issue.get(key) == want for key, want in conditions.items())


# --- cursor + engine --------------------------------------------------------


def get_cursor(conn: sqlite3.Connection) -> int:
    """The id of the last activity row the engine has processed."""
    return conn.execute("SELECT cursor FROM automation_state WHERE id = 1").fetchone()[
        "cursor"
    ]


def _set_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute("UPDATE automation_state SET cursor = ? WHERE id = 1", (value,))
    conn.commit()


def record_rule_failure(
    conn: sqlite3.Connection, rule_id: int, error: str, *, commit: bool = True
) -> None:
    """Note that a rule's action raised: bump its failure_count and stamp the error text
    and time. The engine still advances its cursor past the event (at-most-once, by
    design — see process_pending), so a bad rule never wedges the loop; this just makes
    the failure visible on /admin and the API instead of vanishing. Mirrors how
    core/webhooks records a failed delivery per subscriber row."""
    conn.execute(
        "UPDATE automation_rules "
        "SET failure_count = failure_count + 1, last_error = ?, "
        "last_error_at = datetime('now') WHERE id = ?",
        (error, rule_id),
    )
    if commit:
        conn.commit()


# An Executor performs one rule's action for one event. Injected so process_pending is
# testable without real writes; the live loop passes the real action executor.
Executor = Callable[[sqlite3.Connection, dict, dict], None]


def process_pending(
    conn: sqlite3.Connection,
    *,
    executor: Executor,
    max_batch: int = 50,
    skip_actor_id: int | None = None,
) -> int:
    """Run ONE engine pass: read activity events after the cursor (oldest-first), fire
    every matching enabled rule through `executor`, and advance the cursor past the
    batch. Returns the number of (rule, event) actions fired.

    Loop guard: events authored by `skip_actor_id` are NOT matched — the live engine
    attributes its own actions to a system automation actor and skips that actor's
    events, so a rule whose action emits a new event can't re-trigger itself forever.

    The cursor advances past every event read (matched or not), so an event is processed
    at most once (best-effort, fire-and-forget — unlike webhooks' at-least-once retry).
    An executor that raises is caught per (rule, event) so one bad action neither strands
    the cursor nor blocks the rest of the batch."""
    cursor = get_cursor(conn)
    events = activity.list_events(conn, after_id=cursor, limit=max_batch)
    if not events:
        return 0
    enabled = [
        rule
        for rule in list_rules(conn, enabled_only=True)
        if rule["trigger_type"] == "event"
    ]
    fired = 0
    last_id = cursor
    for event in events:
        last_id = event["id"]
        if skip_actor_id is not None and event["actor_id"] == skip_actor_id:
            continue
        for rule in enabled:
            if _matches(conn, rule, event):
                try:
                    executor(conn, rule, event)
                    fired += 1
                except Exception as exc:  # noqa: BLE001 — a bad action must not strand the cursor
                    # At-most-once by design: the cursor still advances past this event
                    # (below) so one bad rule never wedges the loop. But no longer
                    # SILENTLY — log it and stamp the rule so the operator can see a
                    # misbehaving rule on /admin and via the API.
                    logger.warning(
                        "automation rule %s failed on event %s: %s",
                        rule["id"],
                        event["id"],
                        exc,
                        exc_info=True,
                    )
                    record_rule_failure(
                        conn, rule["id"], f"{type(exc).__name__}: {exc}"
                    )
    _set_cursor(conn, last_id)
    return fired


# --- the live executor: turn a matched event into an in-app write -----------

# The dedicated actor every rule action is attributed to, so the audit trail reads
# "Automation assigned …" and the engine can skip its OWN events (the loop guard).
SYSTEM_ACTOR_EMAIL = issue_commands.AUTOMATION_ACTOR_EMAIL


def system_actor_id(conn: sqlite3.Connection) -> int:
    """The id of the 'Automation' actor, get-or-created on first use. An is_agent user
    with NO password (it never logs in) and the viewer role (it writes through the data
    layer, not the gated API). Single-process loop, so the get-or-create needs no lock."""
    existing = users.get_user_by_email(conn, SYSTEM_ACTOR_EMAIL)
    if existing is not None:
        return existing["id"]
    return users.create_user(
        conn,
        email=SYSTEM_ACTOR_EMAIL,
        name="Automation",
        is_agent=True,
        role=users.VIEWER_ROLE,
    )["id"]


def automation_run_id(rule_id: int, event_id: int) -> str:
    """The lineage run id stamped on every write a rule makes for one trigger event.
    Unique per (rule, triggering event) firing, so it both groups the action in
    replay/lineage AND doubles as the idempotency key below — re-running the same
    firing after a crash can recognize it already happened."""
    return f"automation:rule-{rule_id}:event-{event_id}"


def _already_fired(conn: sqlite3.Connection, firing_run_id: str) -> bool:
    """Whether this exact (rule, event) firing already recorded its action. The
    engine advances its cursor only at end-of-pass, so a crash after an action
    committed but before the cursor moved re-reads the event on restart. The
    assign/status/label/contributor actions are naturally idempotent (they no-op
    when already in the desired state), but a comment always appends — so without
    this guard a crashed pass would double-post it. Keyed on the lineage run id we
    are about to stamp, so the trail is its own dedup ledger (no extra table)."""
    return (
        conn.execute(
            "SELECT 1 FROM activity WHERE run_id = ? LIMIT 1", (firing_run_id,)
        ).fetchone()
        is not None
    )


def execute_action(
    conn: sqlite3.Connection,
    rule: dict,
    event: dict,
    *,
    actor_id: int,
    fail_closed: bool = False,
) -> bool:
    """Perform one rule's ACTION for one matched event, attributed to `actor_id` (the
    system automation actor), and record the matching activity event so the trail shows
    "Automation <did X>". Returns True if a change was actually made. Loads the target
    issue fresh; a vanished issue, a missing/invalid param, or a no-op (already in the
    desired state) returns False rather than raising — a bad rule fails soft instead of
    stranding the engine. The actions mirror the issue write endpoints' data-layer +
    activity-recorder pair, so an automated change is indistinguishable in the log from a
    human one except for the actor. Migrated issue changes go through the same atomic
    application command as REST/web, under the explicit Automation system policy.

    LINEAGE: every write here is stamped (via run_context) with a per-firing run id
    (automation:rule-<id>:event-<id>), the triggering event as its fork point, and
    the triggering event's own run as parent — so an automated change on the trail
    answers "which rule, fired by which event, in which run" without a schema change.
    That same run id makes the firing idempotent across a crashed pass.

    Event rules preserve their historical fail-soft command behavior. Scheduled target
    receipts pass ``fail_closed=True`` so a semantic command rejection is retried and
    remains visible instead of being mistaken for a completed no-op."""
    issue = issues.get_issue(conn, event["target_id"])
    if issue is None:
        return False
    firing_run_id = automation_run_id(rule["id"], event["id"])
    if _already_fired(conn, firing_run_id):
        return False
    run_token = run_context.set_run_id(firing_run_id)
    parent_token = run_context.set_parent_run_id(event.get("run_id"))
    fork_token = run_context.set_forked_from_event_id(event["id"])
    try:
        return _perform_action(
            conn,
            rule,
            event,
            issue,
            actor_id=actor_id,
            fail_closed=fail_closed,
        )
    finally:
        run_context.reset_forked_from_event_id(fork_token)
        run_context.reset_parent_run_id(parent_token)
        run_context.reset_run_id(run_token)


def _perform_action(
    conn: sqlite3.Connection,
    rule: dict,
    event: dict,
    issue: dict,
    *,
    actor_id: int,
    fail_closed: bool,
) -> bool:
    """The action if-chain, run inside execute_action's lineage context."""
    params = rule["action_params"]

    if rule["action_type"] == "assign":
        user_id = params.get("user_id")
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return False
        if issue["assignee_id"] == user_id:
            return False
        try:
            issue_commands.update_issue_as_automation(
                conn,
                actor_id=actor_id,
                issue_id=issue["id"],
                assignee_id=user_id,
            )
        except issue_commands.IssueCommandError:
            if fail_closed:
                raise
            return False
        return True

    if rule["action_type"] == "set_status":
        status = params.get("status")
        if not isinstance(status, str) or not status:
            return False
        if issue["status"] == status:
            return False
        try:
            issue_commands.update_issue_as_automation(
                conn,
                actor_id=actor_id,
                issue_id=issue["id"],
                status=status,
            )
        except issue_commands.IssueCommandError:
            if fail_closed:
                raise
            return False
        return True

    if rule["action_type"] == "add_label":
        name = (params.get("label") or "").strip()
        if not name:
            return False
        label = labels.get_or_create_label(conn, name=name)
        with db.transaction(conn, immediate=True):
            if not labels.add_label_to_issue(
                conn,
                issue["id"],
                label["id"],
                commit=False,
            ):
                return False
            issue_activity.record_label_added(
                conn,
                actor_id=actor_id,
                issue_id=issue["id"],
                label_id=label["id"],
                commit=False,
            )
            return True

    if rule["action_type"] == "comment":
        body = (params.get("body") or "").strip()
        if not body:
            return False
        # Atomic add + audit (one transaction), so the firing's activity event and
        # its comment row land together — a crash can't leave a comment with no
        # trail entry, which is what makes the run-id idempotency guard reliable.
        comment_commands.create_comment(
            conn, actor_id=actor_id, issue_id=issue["id"], body=body
        )
        return True

    if rule["action_type"] == "add_contributor":
        user_id = params.get("user_id")
        if not isinstance(user_id, int):
            return False
        if users.get_user(conn, user_id) is None:
            if fail_closed:
                raise ValueError("configured contributor user does not exist")
            return False
        with db.transaction(conn, immediate=True):
            if not contributors.add_contributor(
                conn,
                issue["id"],
                user_id,
                added_by=actor_id,
                commit=False,
            ):
                return False
            issue_activity.record_contributor_added(
                conn,
                actor_id=actor_id,
                issue_id=issue["id"],
                user_id=user_id,
                commit=False,
            )
            return True

    return False


# --- bounded UTC schedule triggers ------------------------------------------


def schedule_trigger_run_id(rule_id: int, scheduled_for: str, issue_id: int) -> str:
    """The deterministic root run for one scheduled target occurrence."""
    return f"automation:schedule:rule-{rule_id}:slot-{scheduled_for}:issue-{issue_id}"


def _schedule_target_query(rule: dict, scheduled_for: datetime) -> tuple[str, list]:
    clauses = ["i.archived_at IS NULL"]
    params: list = []
    for field, value in rule["conditions"].items():
        if field == "inactive_for_seconds":
            continue
        if field not in CONDITION_FIELDS:
            raise ValueError(f"unsupported schedule condition: {field}")
        clauses.append(f"i.{field} IS ?")
        params.append(value)
    inactive_for = rule["conditions"].get("inactive_for_seconds")
    if inactive_for is not None:
        cutoff = (scheduled_for - timedelta(seconds=inactive_for)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        clauses.append(
            "COALESCE((SELECT MAX(a.created_at) FROM activity a "
            "WHERE a.target_kind = 'issue' AND a.target_id = i.id), i.created_at) "
            "<= ?"
        )
        params.append(cutoff)
    return " AND ".join(clauses), params


def _schedule_targets(
    conn: sqlite3.Connection,
    rule: dict,
    scheduled_for: datetime,
    *,
    max_targets: int,
) -> tuple[list[int], int]:
    """Return a deterministic bounded issue snapshot and the exact match count."""
    where, params = _schedule_target_query(rule, scheduled_for)
    rows = conn.execute(
        f"SELECT i.id FROM issues i WHERE {where} ORDER BY i.id LIMIT ?",
        (*params, max_targets + 1),
    ).fetchall()
    if len(rows) <= max_targets:
        ids = [int(row["id"]) for row in rows]
        return ids, len(ids)
    total = conn.execute(
        f"SELECT COUNT(*) AS count FROM issues i WHERE {where}", params
    ).fetchone()["count"]
    return [], int(total)


def _get_schedule_scan_cursor(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT scan_cursor FROM automation_schedule_state WHERE id = 1"
        ).fetchone()["scan_cursor"]
    )


def _set_schedule_scan_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        "UPDATE automation_schedule_state SET scan_cursor = ? WHERE id = 1",
        (value,),
    )
    conn.commit()


def _due_schedule_rule_ids(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    scan_limit: int,
    after_id: int,
) -> list[int]:
    """A bounded cyclic rule-id scan over canonical, non-expired due rows."""
    now_text = _format_utc_timestamp(now)
    one_shot_cutoff = _format_utc_timestamp(
        now - timedelta(seconds=ONE_SHOT_CATCH_UP_SECONDS)
    )
    rows = conn.execute(
        "SELECT id FROM automation_rules "
        "WHERE enabled = 1 AND trigger_type = 'schedule' "
        "AND next_scheduled_at IS NOT NULL "
        "AND strftime('%Y-%m-%dT%H:%M:%SZ', next_scheduled_at) = "
        "next_scheduled_at "
        "AND next_scheduled_at <= ? "
        "AND (schedule_every_seconds IS NOT NULL OR next_scheduled_at >= ?) "
        "ORDER BY CASE WHEN id > ? THEN 0 ELSE 1 END, id LIMIT ?",
        (now_text, one_shot_cutoff, after_id, scan_limit),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _claim_schedule(
    conn: sqlite3.Connection,
    rule: dict,
    *,
    now: datetime,
    actor_id: int,
    max_targets: int,
) -> int | None:
    """Atomically claim one due slot and persist its complete target snapshot.

    Returns the target count, zero for an empty/overflow slot, or None if another
    pass changed the rule before this claim. The runner is intentionally single-
    process, but the immediate transaction and unique slot key make the receipt safe
    if an operator accidentally overlaps two ticks.
    """
    due = _parse_utc_timestamp(rule["next_scheduled_at"])
    if due is None or due > now:
        return None
    repeat = rule["schedule_every_seconds"]
    if repeat is None:
        if (now - due).total_seconds() > ONE_SHOT_CATCH_UP_SECONDS:
            return None
        scheduled_for = due
        missed_slots = 0
        next_scheduled_at = None
    else:
        missed_slots = int((now - due).total_seconds()) // repeat
        scheduled_for = due + timedelta(seconds=repeat * missed_slots)
        next_scheduled_at = _format_utc_timestamp(
            scheduled_for + timedelta(seconds=repeat)
        )
    scheduled_text = _format_utc_timestamp(scheduled_for)

    with db.transaction(conn, immediate=True):
        fresh = get_rule(conn, rule["id"])
        if (
            fresh is None
            or not fresh["enabled"]
            or fresh["trigger_type"] != "schedule"
            or fresh["configuration_error"] is not None
            or fresh["next_scheduled_at"] != rule["next_scheduled_at"]
        ):
            return None
        target_ids, total_targets = _schedule_targets(
            conn, fresh, scheduled_for, max_targets=max_targets
        )
        overflow_count = max(0, total_targets - max_targets)
        if overflow_count:
            error = (
                f"schedule target limit exceeded: {total_targets} matches "
                f"(limit {max_targets})"
            )
            conn.execute(
                "INSERT INTO automation_schedule_firings "
                "(rule_id, scheduled_for, trigger_count, overflow_count, "
                "missed_slots, state, last_error, completed_at) "
                "VALUES (?, ?, 0, ?, ?, 'failed', ?, datetime('now'))",
                (
                    fresh["id"],
                    scheduled_text,
                    overflow_count,
                    missed_slots,
                    error,
                ),
            )
            conn.execute(
                "UPDATE automation_rules SET next_scheduled_at = ?, "
                "last_scheduled_for = ?, "
                "schedule_missed_count = schedule_missed_count + ?, "
                "last_schedule_target_count = ?, last_schedule_overflow_count = ? "
                "WHERE id = ?",
                (
                    next_scheduled_at,
                    scheduled_text,
                    missed_slots,
                    total_targets,
                    overflow_count,
                    fresh["id"],
                ),
            )
            record_rule_failure(conn, fresh["id"], error, commit=False)
            return 0

        state = "pending" if target_ids else "completed"
        completed_at_sql = "NULL" if target_ids else "datetime('now')"
        cur = conn.execute(
            "INSERT INTO automation_schedule_firings "
            "(rule_id, scheduled_for, trigger_count, overflow_count, missed_slots, "
            f"state, completed_at) VALUES (?, ?, ?, 0, ?, ?, {completed_at_sql})",
            (fresh["id"], scheduled_text, len(target_ids), missed_slots, state),
        )
        firing_id = cur.lastrowid
        assert firing_id is not None
        conn.execute(
            "UPDATE automation_rules SET next_scheduled_at = ?, "
            "last_scheduled_for = ?, "
            "schedule_missed_count = schedule_missed_count + ?, "
            "last_schedule_target_count = ?, last_schedule_overflow_count = 0 "
            "WHERE id = ?",
            (
                next_scheduled_at,
                scheduled_text,
                missed_slots,
                len(target_ids),
                fresh["id"],
            ),
        )
        for issue_id in target_ids:
            trigger_run = schedule_trigger_run_id(fresh["id"], scheduled_text, issue_id)
            run_token = run_context.set_run_id(trigger_run)
            parent_token = run_context.set_parent_run_id(None)
            fork_token = run_context.set_forked_from_event_id(None)
            try:
                trigger = activity.record(
                    conn,
                    actor_id=actor_id,
                    verb=SCHEDULE_TRIGGER_VERB,
                    target_kind="issue",
                    target_id=issue_id,
                    detail=f"automation rule {fresh['id']} scheduled for {scheduled_text}",
                    commit=False,
                )
            finally:
                run_context.reset_forked_from_event_id(fork_token)
                run_context.reset_parent_run_id(parent_token)
                run_context.reset_run_id(run_token)
            conn.execute(
                "INSERT INTO automation_schedule_occurrences "
                "(firing_id, issue_id, trigger_event_id) VALUES (?, ?, ?)",
                (firing_id, issue_id, trigger["id"]),
            )
        return len(target_ids)


def _claim_due_schedules(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    actor_id: int,
    max_rules: int,
    max_targets: int,
) -> int:
    claimed = 0
    scan_cursor = _get_schedule_scan_cursor(conn)
    last_scanned_id: int | None = None
    rule_ids = _due_schedule_rule_ids(
        conn,
        now,
        scan_limit=MAX_SCHEDULE_RULE_SCAN,
        after_id=scan_cursor,
    )
    for rule_id in rule_ids:
        if claimed >= max_rules:
            break
        last_scanned_id = rule_id
        rule = get_rule(conn, rule_id)
        if rule is None or rule["configuration_error"] is not None:
            continue
        result = _claim_schedule(
            conn, rule, now=now, actor_id=actor_id, max_targets=max_targets
        )
        if result is not None:
            claimed += 1
    if last_scanned_id is not None:
        _set_schedule_scan_cursor(conn, last_scanned_id)
    return claimed


def _finalize_schedule_firing(conn: sqlite3.Connection, firing_id: int) -> None:
    summary = conn.execute(
        "SELECT "
        "SUM(CASE WHEN state = 'pending' THEN 1 ELSE 0 END) AS pending_count, "
        "SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failed_count "
        "FROM automation_schedule_occurrences WHERE firing_id = ?",
        (firing_id,),
    ).fetchone()
    if int(summary["pending_count"] or 0) > 0:
        return
    failed = int(summary["failed_count"] or 0) > 0
    latest_error = None
    if failed:
        row = conn.execute(
            "SELECT last_error FROM automation_schedule_occurrences "
            "WHERE firing_id = ? AND state = 'failed' ORDER BY issue_id LIMIT 1",
            (firing_id,),
        ).fetchone()
        latest_error = row["last_error"] if row else "scheduled action failed"
    conn.execute(
        "UPDATE automation_schedule_firings "
        "SET state = ?, last_error = ?, completed_at = datetime('now') "
        "WHERE id = ? AND state = 'pending'",
        ("failed" if failed else "completed", latest_error, firing_id),
    )


def _record_schedule_attempt(
    conn: sqlite3.Connection,
    *,
    firing_id: int,
    issue_id: int,
    rule_id: int,
    error: str | None,
) -> None:
    with db.transaction(conn, immediate=True):
        occurrence = conn.execute(
            "SELECT attempt_count, state FROM automation_schedule_occurrences "
            "WHERE firing_id = ? AND issue_id = ?",
            (firing_id, issue_id),
        ).fetchone()
        if occurrence is None or occurrence["state"] != "pending":
            return
        attempts = int(occurrence["attempt_count"]) + 1
        if error is None:
            state = "completed"
            completed_at_sql = "datetime('now')"
        else:
            state = "failed" if attempts >= MAX_SCHEDULE_ATTEMPTS else "pending"
            completed_at_sql = "datetime('now')" if state == "failed" else "NULL"
        conn.execute(
            "UPDATE automation_schedule_occurrences "
            "SET state = ?, attempt_count = ?, last_error = ?, "
            "last_attempt_at = datetime('now'), "
            f"completed_at = {completed_at_sql} "
            "WHERE firing_id = ? AND issue_id = ?",
            (state, attempts, error, firing_id, issue_id),
        )
        if error is not None:
            record_rule_failure(conn, rule_id, error, commit=False)
        _finalize_schedule_firing(conn, firing_id)


def _process_schedule_occurrences(
    conn: sqlite3.Connection, *, executor: Executor, max_actions: int
) -> int:
    rows = conn.execute(
        "SELECT o.firing_id, o.issue_id, o.trigger_event_id, f.rule_id "
        "FROM automation_schedule_occurrences o "
        "JOIN automation_schedule_firings f ON f.id = o.firing_id "
        "JOIN automation_rules r ON r.id = f.rule_id "
        "WHERE o.state = 'pending' AND f.state = 'pending' "
        "AND r.enabled = 1 AND r.trigger_type = 'schedule' "
        "ORDER BY o.firing_id, o.issue_id LIMIT ?",
        (max_actions,),
    ).fetchall()
    processed = 0
    for occurrence in rows:
        rule = get_rule(conn, occurrence["rule_id"])
        event = activity.get_activity(conn, occurrence["trigger_event_id"])
        error: str | None = None
        if rule is None:
            continue
        if rule["configuration_error"] is not None:
            error = f"malformed schedule: {rule['configuration_error']}"
        elif event is None:
            error = "scheduled trigger activity is missing"
        else:
            try:
                executor(conn, rule, event)
            except Exception as exc:  # noqa: BLE001 - retry one target without wedging others
                error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "scheduled automation rule %s failed for issue %s: %s",
                    rule["id"],
                    occurrence["issue_id"],
                    exc,
                    exc_info=True,
                )
            else:
                processed += 1
        _record_schedule_attempt(
            conn,
            firing_id=occurrence["firing_id"],
            issue_id=occurrence["issue_id"],
            rule_id=rule["id"],
            error=error,
        )
    return processed


def process_schedules(
    conn: sqlite3.Connection,
    *,
    executor: Executor,
    actor_id: int,
    now: datetime | None = None,
    max_rules: int = MAX_SCHEDULE_RULES_PER_PASS,
    max_targets: int = MAX_SCHEDULE_TARGETS_PER_FIRING,
    max_actions: int = MAX_SCHEDULE_ACTIONS_PER_PASS,
) -> int:
    """Claim due UTC slots, then resume a bounded batch of durable target receipts."""
    current = now or _utc_now()
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("schedule runner now must be timezone-aware UTC")
    current = current.astimezone(UTC).replace(microsecond=0)
    _claim_due_schedules(
        conn,
        now=current,
        actor_id=actor_id,
        max_rules=max_rules,
        max_targets=max_targets,
    )
    return _process_schedule_occurrences(
        conn, executor=executor, max_actions=max_actions
    )


def _has_schedule_work(conn: sqlite3.Connection, now: datetime) -> bool:
    now_text = _format_utc_timestamp(now)
    one_shot_cutoff = _format_utc_timestamp(
        now - timedelta(seconds=ONE_SHOT_CATCH_UP_SECONDS)
    )
    return (
        conn.execute(
            "SELECT 1 FROM automation_rules r "
            "WHERE r.enabled = 1 AND r.trigger_type = 'schedule' AND ("
            "EXISTS (SELECT 1 FROM automation_schedule_firings f "
            "JOIN automation_schedule_occurrences o ON o.firing_id = f.id "
            "WHERE f.rule_id = r.id AND f.state = 'pending' "
            "AND o.state = 'pending') OR ("
            "r.next_scheduled_at IS NOT NULL "
            "AND strftime('%Y-%m-%dT%H:%M:%SZ', r.next_scheduled_at) = "
            "r.next_scheduled_at AND r.next_scheduled_at <= ? "
            "AND (r.schedule_every_seconds IS NOT NULL "
            "OR r.next_scheduled_at >= ?))) LIMIT 1",
            (now_text, one_shot_cutoff),
        ).fetchone()
        is not None
    )


# --- background loop (wired into main.lifespan) -----------------------------


def run_pass(db_path: str | Path) -> int:
    """Open a short-lived connection and run one engine pass with the LIVE executor.
    Synchronous (the loop calls it in a worker thread). The automation actor is both the
    attribution for its actions and the loop guard (skip_actor_id), so a rule whose
    action emits a new event can't re-trigger itself.

    The actor is resolved LAZILY — created only the first time a rule actually fires, not
    on every idle tick. This matters on a fresh install: automation defaults ON, so the
    loop ticks immediately; eagerly creating the 'Automation' user before any human signs
    up would make count_users() > 0 and consume the first-user-is-admin bootstrap, locking
    the deploy out. A rule can't exist before a human admin does (created_by FK), so by the
    time the executor fires, bootstrap has already happened. Until the actor exists there
    are no events it authored, so skip_actor_id is None for that pass (any event it emits
    mid-pass lands after this batch and is skipped on the next pass, when the id is known)."""
    conn = db.connect(db_path)
    try:
        existing = users.get_user_by_email(conn, SYSTEM_ACTOR_EMAIL)
        actor: dict[str, int | None] = {"id": existing["id"] if existing else None}

        def executor(c: sqlite3.Connection, rule: dict, event: dict) -> None:
            if actor["id"] is None:
                actor["id"] = system_actor_id(c)
            actor_id = actor["id"]
            assert actor_id is not None
            execute_action(
                c,
                rule,
                event,
                actor_id=actor_id,
                fail_closed=rule["trigger_type"] == "schedule",
            )

        event_actions = process_pending(
            conn, executor=executor, skip_actor_id=actor["id"]
        )
        schedule_actions = 0
        now = _utc_now()
        if _has_schedule_work(conn, now):
            if actor["id"] is None:
                actor["id"] = system_actor_id(conn)
            actor_id = actor["id"]
            assert actor_id is not None
            schedule_actions = process_schedules(
                conn, executor=executor, actor_id=actor_id, now=now
            )
        return event_actions + schedule_actions
    finally:
        conn.close()


async def process_loop(db_path: str | Path) -> None:
    """Forever: run one pass in a worker thread, then sleep the interval. Started/
    cancelled by main.lifespan. A pass that raises is swallowed so one bad tick never
    kills the loop; cancellation (shutdown) propagates out cleanly. Mirrors
    webhooks.delivery_loop."""
    while True:
        try:
            await asyncio.to_thread(run_pass, db_path)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed pass must not stop the loop
            logger.warning("automation pass failed; loop continues", exc_info=True)
        await asyncio.sleep(config.AUTOMATION_INTERVAL_SECONDS)
