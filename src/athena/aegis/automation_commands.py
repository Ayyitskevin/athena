"""Application commands for audited automation-rule lifecycle changes.

An automation rule is the most privileged construct an operator can create: it fires on
any matching activity event and performs writes (assign, label, set-status, comment) as
the system Automation actor, across every issue, with no human in the loop. Yet creating,
arming/disarming, or deleting a rule were bare writes with NO activity event — so who
stood up (or tore down) an instance-wide automated writer left zero trace in the
append-only log. These commands own that write: the row change and its audit event run
in one db.transaction, so a rule can never appear, flip, or vanish without its record.

The direct twin of core/webhook_commands.py (automation's own set_enabled docstring calls
it "the pause twin of a webhook's active flag"). Authorization (admin-only) and rule-spec
validation stay at the transport boundary, as they do for webhooks; the command owns the
write and the audit emission.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import automation
from athena.core import activity, db

# Free-form activity verbs (activity.verb is plain TEXT; see migrations/0017).
VERB_CREATED = "created_automation_rule"
VERB_ENABLED = "enabled_automation_rule"
VERB_DISABLED = "disabled_automation_rule"
VERB_DELETED = "deleted_automation_rule"


class AutomationCommandError(Exception):
    """A transport-neutral rejection. ``status_code`` lets each adapter map it (404 for
    an unknown rule) without the command having to know about HTTP."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _detail(rule: dict) -> str:
    """A human-readable summary of what the rule does — its name and its when->do shape —
    so the trail shows WHICH automated writer changed, not just an opaque id."""
    return (
        f"{rule['name']} (on {rule['trigger_verb']} {rule['target_kind']} "
        f"→ {rule['action_type']})"
    )


def create_rule(
    conn: sqlite3.Connection,
    *,
    actor_id: int,
    name: str,
    trigger_verb: str,
    action_type: str,
    conditions: dict | None = None,
    action_params: dict | None = None,
    target_kind: str = "issue",
) -> dict:
    """Create an automation rule and record a 'created_automation_rule' event atomically.
    The detail records the rule's name and its when->do shape. Rule-spec validity is the
    caller's guard, applied before this runs; sqlite3.IntegrityError (an unreal created_by)
    propagates unchanged."""
    with db.transaction(conn, immediate=True):
        rule = automation.create_rule(
            conn,
            name=name,
            trigger_verb=trigger_verb,
            action_type=action_type,
            created_by=actor_id,
            conditions=conditions,
            action_params=action_params,
            target_kind=target_kind,
            commit=False,
        )
        activity.record(
            conn,
            actor_id=actor_id,
            verb=VERB_CREATED,
            target_kind="automation_rule",
            target_id=rule["id"],
            detail=_detail(rule),
            commit=False,
        )
        return rule


def set_rule_enabled(
    conn: sqlite3.Connection, *, actor_id: int, rule_id: int, enabled: bool
) -> dict:
    """Arm or disarm a rule and record the change atomically. Records an
    'enabled_automation_rule' / 'disabled_automation_rule' event only when the enabled
    state actually flips (re-arming an already-armed rule is not a lifecycle moment).
    Raises AutomationCommandError(404) for an unknown rule."""
    with db.transaction(conn, immediate=True):
        before = automation.get_rule(conn, rule_id)
        if before is None:
            raise AutomationCommandError("no such rule", status_code=404)
        updated = automation.set_enabled(conn, rule_id, enabled, commit=False)
        if updated is None:
            raise AutomationCommandError("no such rule", status_code=404)
        if bool(before["enabled"]) != bool(updated["enabled"]):
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_ENABLED if updated["enabled"] else VERB_DISABLED,
                target_kind="automation_rule",
                target_id=rule_id,
                detail=_detail(updated),
                commit=False,
            )
        return updated


def delete_rule(conn: sqlite3.Connection, *, actor_id: int, rule_id: int) -> bool:
    """Delete a rule and record a 'deleted_automation_rule' event atomically. Returns True
    if one was removed, False if there was no such rule (so the caller can 404). No event
    is recorded when nothing was deleted. The rule is read first so the audit detail can
    name the automated writer that is going away."""
    with db.transaction(conn, immediate=True):
        target = automation.get_rule(conn, rule_id)
        removed = automation.delete_rule(conn, rule_id, commit=False)
        if removed and target is not None:
            activity.record(
                conn,
                actor_id=actor_id,
                verb=VERB_DELETED,
                target_kind="automation_rule",
                target_id=rule_id,
                detail=_detail(target),
                commit=False,
            )
        return removed
