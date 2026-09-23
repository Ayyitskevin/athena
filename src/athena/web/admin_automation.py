"""Browser routes for automation rules.

Split out of web/admin.py."""

from __future__ import annotations
import sqlite3
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from athena import config
from athena.aegis import (
    automation,
    automation_commands,
    projects,
    sprints,
    statuses,
)
from athena.core import (
    users,
)
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates

from athena.web.admin import (
    _admin_required,
)

router = APIRouter()


def _int_or_none(raw: str) -> int | None:
    """A select's value as an int, or None for the empty ('any'/unset) option. Selects
    only ever submit a known id or '', so a non-numeric value is treated as unset."""
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _optional_form_int(raw: str, *, field: str) -> tuple[int | None, str | None]:
    """Parse optional operator-entered integer text without treating errors as unset.

    Project/user ids come from closed selects, where ``_int_or_none`` is appropriate.
    Schedule values are free text: silently treating ``"hourly"`` as absent could turn
    a requested recurring rule into a one-shot rule, so malformed text fails closed.
    """
    value = (raw or "").strip()
    if not value:
        return None, None
    try:
        return int(value), None
    except ValueError:
        return None, f"{field} must be an integer"


def _automation_context(conn: sqlite3.Connection, *, error: str | None = None) -> dict:
    """Everything the rule-builder page renders: the existing rules plus the option
    sets the create form offers (the SAME closed sets the validator enforces, so the
    form can't suggest a value the boundary would reject). Project/user id→name maps let
    the rules table show "in <Project>" / "assign <Name>" instead of bare ids."""
    project_rows = projects.list_projects(conn)  # admin: every project
    sprint_rows = sprints.list_sprints(conn)
    user_rows = users.list_users(conn)
    return {
        "rules": automation.list_rules(conn),
        "trigger_verbs": automation.TRIGGER_VERBS,
        "action_types": automation.ACTION_TYPES,
        "projects": project_rows,
        "sprints": sprint_rows,
        "users": user_rows,
        # The default workflow's statuses, offered as suggestions for set_status — a rule
        # may still target a project with custom statuses (validated at fire time).
        "status_suggestions": statuses.status_names(conn, None),
        "project_names": {p["id"]: p["name"] for p in project_rows},
        "sprint_names": {s["id"]: s["name"] for s in sprint_rows},
        "user_names": {u["id"]: u["name"] for u in user_rows},
        # Shown as the buzz_message channel placeholder so the operator can see
        # what "blank" means on this deployment.
        "default_assign_channel": config.buzz_assign_channel(),
        "error": error,
    }


def _action_params_from_form(
    action_type: str,
    *,
    user_id: int | None,
    status: str,
    label: str,
    body: str,
    buzz_channel: str = "",
    buzz_mention: str = "",
    buzz_note: str = "",
) -> dict:
    """Fold the per-action form fields down to the one action_params dict the chosen
    action_type needs. Unrelated fields are ignored, so switching the action select
    doesn't smuggle a stale value into the rule. An empty field yields {}, which
    validate_rule then rejects with the right 'requires …' message (buzz_message
    accepts {} — every param is optional there)."""
    if action_type in ("assign", "add_contributor"):
        return {"user_id": user_id} if user_id is not None else {}
    if action_type == "set_status":
        return {"status": status.strip()} if status.strip() else {}
    if action_type == "add_label":
        return {"label": label.strip()} if label.strip() else {}
    if action_type == "comment":
        return {"body": body.strip()} if body.strip() else {}
    if action_type == "buzz_message":
        params: dict = {}
        if buzz_channel.strip():
            params["channel"] = buzz_channel.strip()
        if buzz_mention.strip():
            params["mention"] = buzz_mention.strip()
        if buzz_note.strip():
            params["note"] = buzz_note.strip()
        return params
    return {}


@router.get("/admin/automation", response_class=HTMLResponse)
def automation_admin(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _admin_required(user)
    if err is not None:
        return err
    return templates.TemplateResponse(
        request=request, name="admin/automation.html", context=_automation_context(conn)
    )


@router.post(
    "/admin/automation",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def create_rule(
    request: Request,
    name: str = Form(""),
    trigger_type: str = Form("event"),
    trigger_verb: str = Form(""),
    schedule_at: str = Form(""),
    schedule_every_seconds: str = Form(""),
    condition_project: str = Form(""),
    condition_sprint: str = Form(""),
    condition_inactive_for_seconds: str = Form(""),
    action_type: str = Form(""),
    action_user_id: str = Form(""),
    action_status: str = Form(""),
    action_label: str = Form(""),
    action_body: str = Form(""),
    action_buzz_channel: str = Form(""),
    action_buzz_mention: str = Form(""),
    action_buzz_note: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    templates = get_templates()
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"

    name = name.strip()
    trigger_type = trigger_type.strip()
    if trigger_type == "schedule":
        trigger_verb = automation.SCHEDULE_TRIGGER_VERB
        schedule_at_value = schedule_at.strip() or None
        schedule_every, schedule_every_error = _optional_form_int(
            schedule_every_seconds, field="schedule_every_seconds"
        )
        inactive_for, inactive_for_error = _optional_form_int(
            condition_inactive_for_seconds, field="inactive_for_seconds"
        )
    else:
        # Ignore fields from the unselected trigger type, just as action-specific
        # fields below are ignored. Stale schedule text cannot alter an event rule.
        schedule_at_value = None
        schedule_every = None
        schedule_every_error = None
        inactive_for = None
        inactive_for_error = None

    conditions: dict = {}
    project_id = _int_or_none(condition_project)
    if project_id is not None:
        conditions["project_id"] = project_id
    sprint_id = _int_or_none(condition_sprint)
    if sprint_id is not None:
        conditions["sprint_id"] = sprint_id
    if inactive_for is not None:
        conditions["inactive_for_seconds"] = inactive_for
    action_params = _action_params_from_form(
        action_type,
        user_id=_int_or_none(action_user_id),
        status=action_status,
        label=action_label,
        body=action_body,
        buzz_channel=action_buzz_channel,
        buzz_mention=action_buzz_mention,
        buzz_note=action_buzz_note,
    )

    def _reject(message: str):
        return templates.TemplateResponse(
            request=request,
            name="admin/automation.html",
            context=_automation_context(conn, error=message),
            status_code=400,
        )

    if schedule_every_error is not None:
        return _reject(schedule_every_error)
    if inactive_for_error is not None:
        return _reject(inactive_for_error)
    try:
        automation_commands.create_rule(
            conn,
            actor_id=actor["id"],
            name=name,
            trigger_verb=trigger_verb,
            action_type=action_type,
            conditions=conditions,
            action_params=action_params,
            trigger_type=trigger_type,
            schedule_at=schedule_at_value,
            schedule_every_seconds=schedule_every,
        )
    except automation_commands.AutomationCommandError as exc:
        return _reject(str(exc))
    return RedirectResponse("/admin/automation", status_code=303)


@router.post(
    "/admin/automation/{rule_id}/enabled",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def toggle_rule(
    request: Request,
    rule_id: int,
    enabled: str = Form("0"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
    # The form posts the DESIRED next state ("1" enable, anything else pause) — a
    # deterministic toggle that keeps the rule (and its place in fire order). The
    # command records the flip atomically.
    try:
        automation_commands.set_rule_enabled(
            conn, actor_id=actor["id"], rule_id=rule_id, enabled=enabled == "1"
        )
    except automation_commands.AutomationCommandError:
        return HTMLResponse('<div class="error">No such rule.</div>', status_code=404)
    return RedirectResponse("/admin/automation", status_code=303)


@router.post(
    "/admin/automation/{rule_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
def delete_rule(
    request: Request,
    rule_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    actor = getattr(request.state, "user", None)
    err = _admin_required(actor)
    if err is not None:
        return err
    assert actor is not None, "_admin_required accepted a missing user"
    if not automation_commands.delete_rule(conn, actor_id=actor["id"], rule_id=rule_id):
        return HTMLResponse('<div class="error">No such rule.</div>', status_code=404)
    return RedirectResponse("/admin/automation", status_code=303)
