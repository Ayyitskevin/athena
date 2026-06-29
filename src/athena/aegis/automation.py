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

import json
import sqlite3
from collections.abc import Callable

from athena.aegis import issues
from athena.core import activity

# The actions a rule may take. The engine dispatches on action_type; the boundary
# validates a rule's action_type is one of these. (The real implementations land with
# the live executor in a later slice; this set is the contract both sides agree on.)
ACTION_TYPES = ("assign", "add_label", "set_status", "comment", "add_contributor")

# The issue fields a CONDITION may match against, resolved from the target issue at match
# time. Keeping the set explicit means a typo'd condition key is a boundary error, never
# a silent match-everything.
CONDITION_FIELDS = ("project_id", "status", "priority", "assignee_id", "sprint_id")

_COLS = (
    "id, name, enabled, trigger_verb, target_kind, conditions, action_type, "
    "action_params, created_by, created_at"
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
    conn: sqlite3.Connection, *, enabled_only: bool = False
) -> list[dict]:
    """Every rule (or just the enabled ones), in id order — the order they fire."""
    sql = f"SELECT {_COLS} FROM automation_rules"
    if enabled_only:
        sql += " WHERE enabled = 1"
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


def matching_rules(
    conn: sqlite3.Connection, event: dict, rules: list[dict] | None = None
) -> list[dict]:
    """The enabled rules that fire for this event, in id order. `rules` may be passed to
    avoid a re-query when scanning many events in one pass."""
    rules = rules if rules is not None else list_rules(conn, enabled_only=True)
    return [r for r in rules if r["enabled"] and _matches(conn, r, event)]


# --- cursor + engine --------------------------------------------------------


def get_cursor(conn: sqlite3.Connection) -> int:
    """The id of the last activity row the engine has processed."""
    return conn.execute(
        "SELECT cursor FROM automation_state WHERE id = 1"
    ).fetchone()["cursor"]


def _set_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute("UPDATE automation_state SET cursor = ? WHERE id = 1", (value,))
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
                except Exception:  # noqa: BLE001 — a bad action must not strand the cursor
                    pass
    _set_cursor(conn, last_id)
    return fired
