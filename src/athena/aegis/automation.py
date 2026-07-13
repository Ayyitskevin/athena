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
import json
import logging
from pathlib import Path
import sqlite3
from collections.abc import Callable

from athena import config
from athena.aegis import comments, contributors, issue_activity, issue_commands, issues
from athena.core import activity, db, labels, users

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

# The activity verbs a rule may TRIGGER on, for target_kind="issue" — every verb the
# issue lifecycle emits (see aegis/issue_activity.py), plus "*" for "any issue event".
# Validating against this closed set at the boundary means a typo'd verb is a 422, never
# a rule that silently never fires. The only target_kind rules act on yet is "issue".
TRIGGER_VERBS = (
    "created", "issue_edited", "changed_status", "changed_priority",
    "assigned", "unassigned", "changed_project", "removed_from_project",
    "moved_to_sprint", "removed_from_sprint", "set_parent", "removed_parent",
    "added_contributor", "removed_contributor", "labeled", "unlabeled",
    "commented", "comment_deleted", "archived", "unarchived",
)
TARGET_KINDS = ("issue",)


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
) -> str | None:
    """Whether a rule spec is well-formed: return a human error string, or None if valid.
    The ONE validator both write surfaces share — the REST API (slice 3) and the web form
    (slice 4) — so a malformed rule is rejected the same way on both, never persisted as a
    row that can't ever fire (typo'd verb/condition key) or whose action no-ops every time
    (missing param). The data layer itself only persists; this is the boundary contract."""
    if target_kind not in TARGET_KINDS:
        return f"target_kind must be one of: {', '.join(TARGET_KINDS)}"
    if trigger_verb != "*" and trigger_verb not in TRIGGER_VERBS:
        return f"trigger_verb must be '*' or one of: {', '.join(TRIGGER_VERBS)}"
    if action_type not in ACTION_TYPES:
        return f"action_type must be one of: {', '.join(ACTION_TYPES)}"
    for key in conditions:
        if key not in CONDITION_FIELDS:
            return (
                f"unknown condition field '{key}'; "
                f"allowed: {', '.join(CONDITION_FIELDS)}"
            )
    return _validate_action_params(action_type, action_params)


_COLS = (
    "id, name, enabled, trigger_verb, target_kind, conditions, action_type, "
    "action_params, created_by, created_at, failure_count, last_error, last_error_at"
)


def _row(row: sqlite3.Row) -> dict:
    """A rule row with its JSON blobs parsed and enabled coerced to bool, so callers
    work with a plain dict (the saved_filters criteria pattern)."""
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["conditions"] = json.loads(d["conditions"])
    d["action_params"] = json.loads(d["action_params"])
    return d


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
) -> dict:
    """Insert a rule and return it. conditions/action_params are stored as JSON text.
    The boundary validates trigger_verb/action_type/condition keys; this layer persists.
    Raises sqlite3.IntegrityError if created_by isn't a real user (the FK)."""
    cur = conn.execute(
        "INSERT INTO automation_rules "
        "(name, trigger_verb, target_kind, conditions, action_type, action_params, "
        "created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            trigger_verb,
            target_kind,
            json.dumps(conditions or {}),
            action_type,
            json.dumps(action_params or {}),
            created_by,
        ),
    )
    conn.commit()
    return get_rule(conn, cur.lastrowid)


def get_rule(conn: sqlite3.Connection, rule_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM automation_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    return _row(row) if row else None


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
    return [_row(r) for r in conn.execute(sql).fetchall()]


def set_enabled(
    conn: sqlite3.Connection, rule_id: int, enabled: bool
) -> dict | None:
    """Switch a rule on/off WITHOUT deleting it (the pause twin of a webhook's active
    flag). Returns the updated rule, or None if there is no such rule."""
    cur = conn.execute(
        "UPDATE automation_rules SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, rule_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_rule(conn, rule_id)


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
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
    return conn.execute(
        "SELECT cursor FROM automation_state WHERE id = 1"
    ).fetchone()["cursor"]


def _set_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute("UPDATE automation_state SET cursor = ? WHERE id = 1", (value,))
    conn.commit()


def record_rule_failure(conn: sqlite3.Connection, rule_id: int, error: str) -> None:
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
    enabled = list_rules(conn, enabled_only=True)
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
                        rule["id"], event["id"], exc, exc_info=True,
                    )
                    record_rule_failure(conn, rule["id"], f"{type(exc).__name__}: {exc}")
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
        conn, email=SYSTEM_ACTOR_EMAIL, name="Automation",
        is_agent=True, role=users.VIEWER_ROLE,
    )["id"]


def execute_action(
    conn: sqlite3.Connection, rule: dict, event: dict, *, actor_id: int
) -> bool:
    """Perform one rule's ACTION for one matched event, attributed to `actor_id` (the
    system automation actor), and record the matching activity event so the trail shows
    "Automation <did X>". Returns True if a change was actually made. Loads the target
    issue fresh; a vanished issue, a missing/invalid param, or a no-op (already in the
    desired state) returns False rather than raising — a bad rule fails soft instead of
    stranding the engine. The actions mirror the issue write endpoints' data-layer +
    activity-recorder pair, so an automated change is indistinguishable in the log from a
    human one except for the actor. Migrated issue changes go through the same atomic
    application command as REST/web, under the explicit Automation system policy."""
    issue = issues.get_issue(conn, event["target_id"])
    if issue is None:
        return False
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
            return False
        return True

    if rule["action_type"] == "add_label":
        name = (params.get("label") or "").strip()
        if not name:
            return False
        label = labels.get_or_create_label(conn, name=name)
        if labels.add_label_to_issue(conn, issue["id"], label["id"]):
            issue_activity.record_label_added(
                conn, actor_id=actor_id, issue_id=issue["id"], label_id=label["id"]
            )
            return True
        return False

    if rule["action_type"] == "comment":
        body = (params.get("body") or "").strip()
        if not body:
            return False
        comments.add_comment(conn, issue_id=issue["id"], author_id=actor_id, body=body)
        issue_activity.record_commented(
            conn, actor_id=actor_id, issue_id=issue["id"], body=body
        )
        return True

    if rule["action_type"] == "add_contributor":
        user_id = params.get("user_id")
        if not isinstance(user_id, int) or users.get_user(conn, user_id) is None:
            return False
        if contributors.add_contributor(conn, issue["id"], user_id, added_by=actor_id):
            issue_activity.record_contributor_added(
                conn, actor_id=actor_id, issue_id=issue["id"], user_id=user_id
            )
            return True
        return False

    return False


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
            execute_action(c, rule, event, actor_id=actor["id"])

        return process_pending(conn, executor=executor, skip_actor_id=actor["id"])
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
