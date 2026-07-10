"""Web routes for projects, project access, and sprints — the Aegis container surface.

Split out of web/router.py (which had grown past 2,700 lines) to keep each web
surface navigable, following the same one-module-per-area pattern as web/mentor.py,
web/admin.py, and web/labels.py. Its own APIRouter, mounted by main.py. A thin client
over the projects/sprints data layers, gated on the browser session — it owns no data.
The template and read-only-response helpers are the shared ones from web.router
(get_templates reads the Jinja instance main.py injects at startup).
"""
from __future__ import annotations

import html
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import issues, project_activity, projects, sprints, statuses
from athena.core import access, identity, users
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.router import _readonly_response, get_templates

router = APIRouter()


@router.get("/aegis/projects", response_class=HTMLResponse)
def projects_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """List all projects, each with a count of its issues and a link to the issue
    list filtered to it. Reading is open; the create form below is gated."""
    if get_templates() is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    # Only the projects this viewer may see (public + their own private ones; admins
    # all). A private project never appears here to someone outside it.
    all_projects = projects.list_projects(conn, access.visible_project_filter(conn, user))
    # One count per project, cheap on the small lists we have. NULL-project issues
    # (the backlog) are simply not counted under any project. Every listed project is
    # visible to the viewer, so its issue count is theirs to see in full.
    counts = {
        p["id"]: len(issues.list_issues(conn, project_id=p["id"]))
        for p in all_projects
    }
    can_write = user is not None and identity.can_write(user)
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/projects.html",
        context={
            "projects": all_projects,
            "counts": counts,
            "can_write": can_write,
            # An admin may manage access on any project, the creator only on their own —
            # the template uses this to show the "Manage access" link beyond the creator.
            "is_admin": user is not None and identity.is_admin(user),
        },
    )


@router.post("/aegis/projects", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def create_project(
    request: Request,
    name: str = Form(""),
    key: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create a project as the logged-in user (the actor is the session, never a
    form field — same rule as the REST API). Empty name → 400, bad key → 400,
    duplicate name or key → 409; otherwise 303 back to the projects list."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to create projects.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return _readonly_response()
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Project name is required.</div>', status_code=400)
    normalized_key = projects.normalize_key(key)
    if normalized_key is None:
        return HTMLResponse(
            '<div class="error">Key must start with a letter and be 1–10 letters/digits.</div>',
            status_code=400,
        )
    if projects.get_project_by_name(conn, name) is not None:
        return HTMLResponse('<div class="error">A project with that name already exists.</div>', status_code=409)
    if projects.get_project_by_key(conn, normalized_key) is not None:
        return HTMLResponse('<div class="error">That project key is already in use.</div>', status_code=409)
    projects.create_project(
        conn,
        name=name,
        key=normalized_key,
        description=description.strip(),
        created_by=user["id"],
    )
    return RedirectResponse("/aegis/projects", status_code=303)


def _authorize_project_write(conn, project_id: int, user: dict):
    """Resolve a project the logged-in user may edit/delete, or an error response.
    Returns (project, None) on success, or (None, HTMLResponse) with the right
    status: 404 if no such project, 403 if the user isn't its creator. Edit/delete
    is creator-only, the same rule the REST API enforces. The 401 (logged-out)
    check stays at each call site, before this."""
    project = projects.get_project(conn, project_id)
    # Visibility first: a private project the user can't see is "no such project" (404),
    # so a non-member never learns it exists via the 403. A visible-but-not-creator user
    # still gets the honest 403.
    if project is None or not access.can_see_project(conn, user, project_id):
        return None, HTMLResponse(
            '<div class="error">No such project.</div>', status_code=404
        )
    if not identity.can_write(user):
        return None, _readonly_response()
    if project["created_by"] != user["id"]:
        return None, HTMLResponse(
            '<div class="blocked">Only the project creator may edit it.</div>',
            status_code=403,
        )
    return project, None


@router.get("/aegis/projects/{project_id}/edit", response_class=HTMLResponse)
def project_edit_form(
    request: Request, project_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """The prefilled edit form for a project. Creator-only, like the save below:
    a logged-out user gets 401, a non-creator 403, a missing project 404. The
    Delete control lives here too, disabled with an explanation while the project
    still owns issues (the API would refuse that delete with a 409)."""
    if get_templates() is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit projects.</div>',
            status_code=401,
        )
    project, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    proj_statuses = statuses.list_statuses(conn, project_id)
    # How many issues use each status, so the template can disable removing one
    # that's still in use (the API/data layer refuses it too).
    status_usage = {
        s["name"]: len(
            issues.list_issues(conn, project_id=project_id, status=s["name"])
        )
        for s in proj_statuses
    }
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/project_edit.html",
        context={
            "project": project,
            "issue_count": issues.count_issues_in_project(conn, project_id),
            "statuses": proj_statuses,
            "status_usage": status_usage,
            "status_categories": statuses.CATEGORIES,
        },
    )


@router.post("/aegis/projects/{project_id}/edit", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def project_edit_save(
    request: Request,
    project_id: int,
    name: str = Form(""),
    key: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save an edit to a project's name/key/description. Creator-only (401/403/404),
    empty name → 400, bad key → 400, a name OR key that collides with ANOTHER
    project → 409; otherwise 303 back to the projects list."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit projects.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Project name is required.</div>', status_code=400)
    normalized_key = projects.normalize_key(key)
    if normalized_key is None:
        return HTMLResponse(
            '<div class="error">Key must start with a letter and be 1–10 letters/digits.</div>',
            status_code=400,
        )
    clash = projects.get_project_by_name(conn, name)
    if clash is not None and clash["id"] != project_id:
        return HTMLResponse('<div class="error">A project with that name already exists.</div>', status_code=409)
    key_clash = projects.get_project_by_key(conn, normalized_key)
    if key_clash is not None and key_clash["id"] != project_id:
        return HTMLResponse('<div class="error">That project key is already in use.</div>', status_code=409)
    projects.update_project(
        conn, project_id, name=name, key=normalized_key, description=description.strip()
    )
    return RedirectResponse("/aegis/projects", status_code=303)


@router.post("/aegis/projects/{project_id}/delete", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def project_delete(
    request: Request, project_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Delete a project from the edit page. Creator-only (401/403/404). Refused
    with 409 if the project still owns issues OR sprints — we don't cascade or
    detach, so those must be emptied first (the same block-don't-cascade rule the
    REST API's delete enforces at aegis/api.py:delete_project)."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to delete projects.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    if issues.count_issues_in_project(conn, project_id) > 0:
        return HTMLResponse(
            '<div class="error">Reassign or delete this project\'s issues first.</div>',
            status_code=409,
        )
    # sprints.project_id is NOT NULL with no ON DELETE, so a project owning any sprint
    # would trip the FK on the bare DELETE and 500 (undeletable from the UI). Refuse
    # cleanly, matching the REST twin — the web delete had drifted from it.
    if sprints.list_sprints(conn, project_id=project_id):
        return HTMLResponse(
            '<div class="error">Delete this project\'s sprints first.</div>',
            status_code=409,
        )
    projects.delete_project(conn, project_id)
    return RedirectResponse("/aegis/projects", status_code=303)


# --- Project access: privacy toggle + member management (web) -------------
#
# The browser surface for the access control that already lives in the REST API.
# Managing access is creator-OR-admin (wider than edit/delete, creator-only), so this
# uses its own gate, _authorize_project_manage, rather than _authorize_project_write.


def _authorize_project_manage(conn, project_id: int, user: dict):
    """Resolve a project whose ACCESS (privacy + roster) the user may manage, or an
    error response. Returns (project, None) or (None, HTMLResponse). Creator-OR-admin:
    a private project the user can't even see is 404 (no existence leak); a visible one
    they may see but not manage is 403. The 401 (logged-out) check stays at each call
    site, before this."""
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, user, project_id):
        return None, HTMLResponse(
            '<div class="error">No such project.</div>', status_code=404
        )
    if not identity.can_write(user):
        return None, _readonly_response()
    if project["created_by"] != user["id"] and not identity.is_admin(user):
        return None, HTMLResponse(
            '<div class="blocked">Only the project creator or an admin may manage access.</div>',
            status_code=403,
        )
    return project, None


@router.get("/aegis/projects/{project_id}/access", response_class=HTMLResponse)
def project_access(
    request: Request, project_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """The access page for a project: its visibility with a public/private toggle, and
    — when private — the member roster with add/remove. Creator-or-admin (401/403/404)."""
    if get_templates() is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to manage access.</div>',
            status_code=401,
        )
    project, err = _authorize_project_manage(conn, project_id, user)
    if err is not None:
        return err
    members = access.list_project_members(conn, project_id)
    member_ids = {m["user_id"] for m in members}
    # Everyone who could be added (not already a member). The creator gets in implicitly
    # and is auto-added on going private, so they're naturally excluded once private.
    addable = [u for u in users.list_users(conn) if u["id"] not in member_ids]
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/project_access.html",
        context={"project": project, "members": members, "addable": addable},
    )


@router.post("/aegis/projects/{project_id}/visibility", dependencies=[Depends(verify_csrf)])
def project_set_visibility(
    request: Request,
    project_id: int,
    visibility: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Flip a project public ↔ private from its access page. Creator-or-admin. Going
    private auto-adds the creator to the roster (they keep access via created_by
    regardless). 303 back to the access page."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    project, err = _authorize_project_manage(conn, project_id, user)
    if err is not None:
        return err
    visibility = visibility.strip().lower()
    if visibility not in ("public", "private"):
        return HTMLResponse(
            '<div class="error">Visibility must be public or private.</div>',
            status_code=400,
        )
    if visibility != project["visibility"]:
        projects.set_visibility(conn, project_id, visibility)
        if visibility == "private":
            access.add_project_member(
                conn, project_id, project["created_by"], added_by=user["id"]
            )
        project_activity.record_project_visibility_changed(
            conn, actor_id=user["id"], project_id=project_id,
            name=project["name"], visibility=visibility,
        )
    return RedirectResponse(f"/aegis/projects/{project_id}/access", status_code=303)


@router.post("/aegis/projects/{project_id}/members", dependencies=[Depends(verify_csrf)])
def project_add_member(
    request: Request,
    project_id: int,
    user_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Grant a user access to a private project from its access page. Creator-or-admin.
    400 on a missing/blank user; a re-add is idempotent. 303 back to the access page."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    _, err = _authorize_project_manage(conn, project_id, user)
    if err is not None:
        return err
    member = users.get_user(conn, int(user_id)) if user_id.strip().isdigit() else None
    if member is None:
        return HTMLResponse('<div class="error">No such user.</div>', status_code=400)
    if access.add_project_member(conn, project_id, member["id"], added_by=user["id"]):
        project_activity.record_project_member_added(
            conn, actor_id=user["id"], project_id=project_id, member_name=member["name"]
        )
    return RedirectResponse(f"/aegis/projects/{project_id}/access", status_code=303)


@router.post(
    "/aegis/projects/{project_id}/members/{member_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def project_remove_member(
    request: Request,
    project_id: int,
    member_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Revoke a user's project membership from its access page. Creator-or-admin. A
    no-op (they weren't a member) still 303s back — the roster simply reflects reality."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    _, err = _authorize_project_manage(conn, project_id, user)
    if err is not None:
        return err
    member = users.get_user(conn, member_id)
    if access.remove_project_member(conn, project_id, member_id):
        project_activity.record_project_member_removed(
            conn, actor_id=user["id"], project_id=project_id,
            member_name=member["name"] if member else str(member_id),
        )
    return RedirectResponse(f"/aegis/projects/{project_id}/access", status_code=303)


# --- Sprints: a project's iterations --------------------------------------


def _sprints_signin(verb: str = "manage sprints") -> HTMLResponse:
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


def _authorize_sprint_write(conn, sprint_id: int, user: dict):
    """Resolve a sprint the user may manage, or an error response. Returns
    (sprint, None) on success, or (None, HTMLResponse). Managing a sprint is the
    project creator's call — it reuses the project gate."""
    sprint = sprints.get_sprint(conn, sprint_id)
    if sprint is None:
        return None, HTMLResponse(
            '<div class="error">No such sprint.</div>', status_code=404
        )
    _, err = _authorize_project_write(conn, sprint["project_id"], user)
    if err is not None:
        return None, err
    return sprint, None


@router.get("/aegis/projects/{project_id}/sprints", response_class=HTMLResponse)
def project_sprints(
    request: Request, project_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """A project's sprints — open read, like the issue list. The create form and the
    start/complete/delete controls render only for the project creator."""
    if get_templates() is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    project = projects.get_project(conn, project_id)
    # A private project the viewer can't see is a 404, like a missing one.
    if project is None or not access.can_see_project(conn, user, project_id):
        return HTMLResponse('<div class="error">No such project.</div>', status_code=404)
    sprint_list = sprints.list_sprints(conn, project_id=project_id)
    counts = {s["id"]: sprints.count_issues_in_sprint(conn, s["id"]) for s in sprint_list}
    can_manage = (
        user is not None
        and identity.can_write(user)
        and project["created_by"] == user["id"]
    )
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/sprints.html",
        context={
            "project": project,
            "sprints": sprint_list,
            "counts": counts,
            "can_manage": can_manage,
        },
    )


@router.post(
    "/aegis/projects/{project_id}/sprints", dependencies=[Depends(verify_csrf)]
)
def create_sprint_web(
    request: Request,
    project_id: int,
    name: str = Form(""),
    goal: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    user = getattr(request.state, "user", None)
    if user is None:
        return _sprints_signin()
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    name = name.strip()
    if not name:
        return HTMLResponse(
            '<div class="error">Sprint name is required.</div>', status_code=400
        )
    sprints.create_sprint(
        conn,
        project_id=project_id,
        name=name,
        goal=goal.strip(),
        start_date=start_date.strip() or None,
        end_date=end_date.strip() or None,
    )
    return RedirectResponse(f"/aegis/projects/{project_id}/sprints", status_code=303)


@router.post("/aegis/sprints/{sprint_id}/start", dependencies=[Depends(verify_csrf)])
def start_sprint_web(
    request: Request, sprint_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    if user is None:
        return _sprints_signin()
    sprint, err = _authorize_sprint_write(conn, sprint_id, user)
    if err is not None:
        return err
    try:
        sprints.start_sprint(conn, sprint_id)
    except sprints.SprintStateError as exc:
        return HTMLResponse(f'<div class="error">{exc}</div>', status_code=409)
    return RedirectResponse(
        f"/aegis/projects/{sprint['project_id']}/sprints", status_code=303
    )


@router.post("/aegis/sprints/{sprint_id}/complete", dependencies=[Depends(verify_csrf)])
def complete_sprint_web(
    request: Request, sprint_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    if user is None:
        return _sprints_signin()
    sprint, err = _authorize_sprint_write(conn, sprint_id, user)
    if err is not None:
        return err
    try:
        sprints.complete_sprint(conn, sprint_id)
    except sprints.SprintStateError as exc:
        return HTMLResponse(f'<div class="error">{exc}</div>', status_code=409)
    return RedirectResponse(
        f"/aegis/projects/{sprint['project_id']}/sprints", status_code=303
    )


@router.post("/aegis/sprints/{sprint_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_sprint_web(
    request: Request, sprint_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    user = getattr(request.state, "user", None)
    if user is None:
        return _sprints_signin()
    sprint, err = _authorize_sprint_write(conn, sprint_id, user)
    if err is not None:
        return err
    if sprints.count_issues_in_sprint(conn, sprint_id) > 0:
        return HTMLResponse(
            '<div class="error">Move its issues out of the sprint first.</div>',
            status_code=409,
        )
    sprints.delete_sprint(conn, sprint_id)
    return RedirectResponse(
        f"/aegis/projects/{sprint['project_id']}/sprints", status_code=303
    )


@router.post("/aegis/projects/{project_id}/statuses", dependencies=[Depends(verify_csrf)])
def add_project_status_web(
    request: Request,
    project_id: int,
    name: str = Form(""),
    category: str = Form("todo"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Add a status to a project from its edit page. Creator-only, like editing the
    project. The data layer rejects duplicates/bad categories; we surface the reason."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    reason = statuses.add_status(conn, project_id, name, category)
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}</div>', status_code=400)
    return RedirectResponse(f"/aegis/projects/{project_id}/edit", status_code=303)


@router.post(
    "/aegis/projects/{project_id}/statuses/{name}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_project_status_web(
    request: Request,
    project_id: int,
    name: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Remove a status from a project. Creator-only. Refused (with a reason) if it's
    the last status or still in use by issues."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    _, err = _authorize_project_write(conn, project_id, user)
    if err is not None:
        return err
    reason = statuses.remove_status(conn, project_id, name)
    if reason is not None:
        return HTMLResponse(f'<div class="error">{html.escape(reason)}</div>', status_code=400)
    return RedirectResponse(f"/aegis/projects/{project_id}/edit", status_code=303)
