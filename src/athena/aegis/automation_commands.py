"""Application commands for audited automation-rule lifecycle changes.

An automation rule is the most privileged construct an operator can create: it fires on
any matching activity event and performs writes (assign, label, set-status, comment) as
the system Automation actor, across every issue, with no human in the loop. Yet creating,
arming/disarming, or deleting a rule were bare writes with NO activity event — so who
stood up (or tore down) an instance-wide automated writer left zero trace in the
append-only log. These commands own that write: the row change and its audit event run
in one db.transaction, so a rule can never appear, flip, or vanish without its record.

The direct twin of core/webhook_commands.py (automation's own set_enabled docstring calls
it "the pause twin of a webhook's active flag"). Authorization stays at the transport
boundary; the command owns normalization, rule-spec validation, persistence, and audit
emission.
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
    """A transport-neutral rejection carrying an error KIND, never a status
    code. Adapters map kinds through their own ``STATUS_BY_KIND`` ("not_found" for
    an unknown rule) without the command having to know about HTTP."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _detail(rule: dict) -> str:
    """A human-readable summary of what the rule does — its name and its when->do shape —
    so the trail shows WHICH automated writer changed, not just an opaque id."""
    if rule["trigger_type"] == "schedule":
        cadence = (
            f", every {rule['schedule_every_seconds']} seconds"
            if rule["schedule_every_seconds"] is not None
            else ""
        )
        return (
            f"{rule['name']} (at {rule['schedule_at']}{cadence} "
            f"{rule['target_kind']} → {rule['action_type']})"
        )
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
    trigger_type: str = "event",
    schedule_at: str | None = None,
    schedule_every_seconds: int | None = None,
) -> dict:
    """Validate and create a rule with its lifecycle activity in one transaction.
    The detail records the rule's normalized name and when-to-action shape.
    ``sqlite3.IntegrityError`` for an unreal creator propagates unchanged.
    """
    normalized_name = name.strip()
    if not normalized_name:
        raise AutomationCommandError("invalid", "rule name is required")
    if conditions is not None and not isinstance(conditions, dict):
        raise AutomationCommandError("invalid", "conditions must be an object")
    if action_params is not None and not isinstance(action_params, dict):
        raise AutomationCommandError("invalid", "action_params must be an object")
    normalized_conditions = conditions or {}
    normalized_action_params = action_params or {}
    error = automation.validate_rule(
        trigger_verb=trigger_verb,
        action_type=action_type,
        conditions=normalized_conditions,
        action_params=normalized_action_params,
        target_kind=target_kind,
        trigger_type=trigger_type,
        schedule_at=schedule_at,
        schedule_every_seconds=schedule_every_seconds,
    )
    if error is not None:
        raise AutomationCommandError("invalid", error)

    with db.transaction(conn, immediate=True):
        rule = automation.create_rule(
            conn,
            name=normalized_name,
            trigger_verb=trigger_verb,
            action_type=action_type,
            created_by=actor_id,
            conditions=normalized_conditions,
            action_params=normalized_action_params,
            target_kind=target_kind,
            trigger_type=trigger_type,
            schedule_at=schedule_at,
            schedule_every_seconds=schedule_every_seconds,
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
    Raises AutomationCommandError("not_found") for an unknown rule."""
    with db.transaction(conn, immediate=True):
        before = automation.get_rule(conn, rule_id)
        if before is None:
            raise AutomationCommandError("not_found", "no such rule")
        updated = automation.set_enabled(conn, rule_id, enabled, commit=False)
        if updated is None:
            raise AutomationCommandError("not_found", "no such rule")
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
