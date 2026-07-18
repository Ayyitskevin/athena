"""The automation-rules REST API — manage "when event X, do Y" rules.

A rule reacts to any activity event across the instance (it isn't scoped to one project:
a rule with no project_id condition fires on every issue), and its action writes to the
data layer as a privileged system actor. That makes rule management an OPERATOR action,
gated like webhooks and user administration: every route requires an admin actor.

The boundary validates the rule spec (automation.validate_rule) before persisting, so a
typo'd verb/condition key or a malformed action is a 422 here, never a row that silently
can't fire. The engine that consumes these rows (the background loop) lives in
aegis/automation.py; this is only the management surface.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from athena.aegis import automation, automation_commands
from athena.core.deps import get_conn
from athena.core.identity import admin_actor

router = APIRouter(prefix="/automation", tags=["aegis"])


class RuleOut(BaseModel):
    id: int
    name: str
    enabled: bool
    trigger_verb: str
    target_kind: str
    conditions: dict
    action_type: str
    action_params: dict
    created_by: int
    created_at: str
    # Failure surface (parity with a webhook's health): how many times this rule's
    # action has raised, the most recent error text, and when. failure_count is 0 and
    # last_error/last_error_at are null for a rule that has never failed.
    failure_count: int
    last_error: str | None
    last_error_at: str | None


class RuleCreate(BaseModel):
    name: str
    trigger_verb: str
    action_type: str
    # JSON objects; defaulted empty. conditions narrow the trigger by issue fields,
    # action_params carry the action's arguments. Both are validated against the rule
    # contract (automation.validate_rule) before the row is written.
    conditions: dict = Field(default_factory=dict)
    action_params: dict = Field(default_factory=dict)
    target_kind: str = "issue"


class RuleUpdate(BaseModel):
    # Pause (false) or resume (true) a rule WITHOUT deleting it — the pause twin of a
    # webhook's active flag. The trigger/action are fixed at creation: to change the
    # logic, delete and recreate (so an edit can never half-rewrite a live rule).
    enabled: bool


@router.get("/rules", response_model=list[RuleOut])
def list_all(
    failing_only: bool = Query(
        False, description="return only rules whose action has failed"
    ),
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    return automation.list_rules(conn, failing_only=failing_only)


@router.post("/rules", response_model=RuleOut, status_code=201)
def create(
    payload: RuleCreate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="rule name is required")
    error = automation.validate_rule(
        trigger_verb=payload.trigger_verb,
        action_type=payload.action_type,
        conditions=payload.conditions,
        action_params=payload.action_params,
        target_kind=payload.target_kind,
    )
    if error is not None:
        raise HTTPException(status_code=422, detail=error)
    # The command owns the insert AND its atomic 'created_automation_rule' audit event,
    # so standing up an instance-wide automated writer is never a silent act.
    return automation_commands.create_rule(
        conn,
        actor_id=actor["id"],
        name=name,
        trigger_verb=payload.trigger_verb,
        action_type=payload.action_type,
        conditions=payload.conditions,
        action_params=payload.action_params,
        target_kind=payload.target_kind,
    )


@router.get("/rules/{rule_id}", response_model=RuleOut)
def show(
    rule_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    rule = automation.get_rule(conn, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="no such rule")
    return rule


@router.patch("/rules/{rule_id}", response_model=RuleOut)
def update(
    rule_id: int,
    payload: RuleUpdate,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The command records the arm/disarm flip atomically.
    try:
        return automation_commands.set_rule_enabled(
            conn, actor_id=actor["id"], rule_id=rule_id, enabled=payload.enabled
        )
    except automation_commands.AutomationCommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.delete("/rules/{rule_id}", status_code=204)
def remove(
    rule_id: int,
    actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    # The command records the deletion atomically (naming the rule going away).
    if not automation_commands.delete_rule(
        conn, actor_id=actor["id"], rule_id=rule_id
    ):
        raise HTTPException(status_code=404, detail="no such rule")
