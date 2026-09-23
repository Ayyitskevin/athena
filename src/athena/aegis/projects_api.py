"""REST routes for Aegis projects, membership, and per-project statuses.

Split out of aegis/api.py. Attaches to the projects router defined there."""

from __future__ import annotations
import sqlite3
from fastapi import (
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool
from athena.aegis import (
    issues,
    project_commands,
    project_etags,
    projects,
    sprints,
    status_commands,
    statuses,
    timeline,
)
from athena.core import (
    access,
    users,
)
from athena.core.ids import RowIdPath
from athena.core.deps import get_conn
from athena.core.identity import is_admin, issue_write_actor, optional_actor

from athena.aegis.api import (
    STATUS_BY_KIND,
    projects_router,
)
from athena.aegis.rest_support import (
    PROJECT_POLICY_PRECONDITION_HTTP,
    PROJECT_POLICY_STATUS,
    if_match_values,
)


class ProjectCreate(BaseModel):
    name: str
    # The issue-key prefix (e.g. "ATH" -> ATH-1, ATH-2). Required on create and
    # the canonical short identity; validated for shape and uniqueness below.
    key: str
    description: str = ""


class ProjectEdit(BaseModel):
    # A partial edit of the project itself (not an issue's link to it — that is
    # ProjectUpdate above). Send any subset; unset fields are left unchanged.
    name: str | None = None
    key: str | None = None
    description: str | None = None


class ProjectPolicyUpdate(BaseModel):
    block_agent_closes_when_blocked: StrictBool


class ProjectOut(BaseModel):
    id: int
    name: str
    key: str
    description: str
    created_by: int
    created_at: str
    # 'public' (anyone may read) or 'private' (creator, admins, and members only).
    # Defaults 'public' for every project until explicitly flipped.
    visibility: str = "public"

    # Disabled by default: projects preserve today's close behavior until a human
    # creator/admin explicitly opts in.
    block_agent_closes_when_blocked: bool = False


class VisibilityUpdate(BaseModel):
    # The privacy flag for a project/space: 'public' | 'private'. A dedicated body
    # (not folded into the project edit) because flipping privacy is creator-OR-admin,
    # while editing name/key/description stays creator-only — different gates.
    visibility: str


class MemberAdd(BaseModel):
    user_id: int


class MemberOut(BaseModel):
    # One membership row on a private project/space: who, plus who granted it and when.
    # Excludes the creator/admins, who get in implicitly (see access.list_*_members).
    user_id: int
    name: str
    is_agent: bool
    added_by: int | None = None
    added_at: str


class StatusCreate(BaseModel):
    name: str
    category: str  # 'todo' | 'doing' | 'done'


class StatusOut(BaseModel):
    name: str
    category: str
    position: int


def _validate_key(key: str) -> str:
    """Normalize and validate a project key, or raise 422. Returns the uppercased
    key the boundary should pass on to the data layer / dup check. The shape rule
    itself lives in projects.normalize_key so the web form enforces it identically."""
    normalized = projects.normalize_key(key)
    if normalized is None:
        raise HTTPException(
            status_code=422,
            detail="key must start with a letter and be 1–10 letters/digits",
        )
    return normalized


def _tagged_project(project: dict, response: Response) -> dict:
    """Return the public project representation and matching strong ETag."""
    public, current = project_etags.resource_and_etag(project)
    response.headers["ETag"] = current
    return public


def _project_policy_error_response(
    exc: project_commands.ProjectPolicyCommandError,
) -> JSONResponse:
    """Map guarded policy failures without weakening project visibility."""
    spec = PROJECT_POLICY_PRECONDITION_HTTP.get(exc.kind)
    if spec is not None:
        status_code, code = spec
        headers = {}
        if exc.current_etag is not None:
            headers["ETag"] = exc.current_etag
        if exc.kind == "precondition_required":
            headers["Cache-Control"] = "no-store"
        return JSONResponse(
            status_code=status_code,
            content={"detail": exc.detail, "code": code},
            headers=headers,
        )
    raise HTTPException(
        status_code=PROJECT_POLICY_STATUS[exc.kind], detail=exc.detail
    ) from exc


@projects_router.get("", response_model=list[ProjectOut])
def list_all_projects(
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading the project list is open (optional_actor → None for anonymous), but it
    # only lists projects the caller may see — public ones plus their own private
    # ones; admins see all. A private project never shows to someone outside it.
    return projects.list_projects(conn, access.visible_project_filter(conn, actor))


@projects_router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may create a project (like creating a label).
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="project name is required")
    key = _validate_key(payload.key)
    if projects.get_project_by_name(conn, name) is not None:
        raise HTTPException(status_code=409, detail="project already exists")
    if projects.get_project_by_key(conn, key) is not None:
        raise HTTPException(status_code=409, detail="project key already in use")
    # The command owns the insert, its default statuses, AND the atomic
    # 'created_project' audit event — a workspace container never appears silently.
    return project_commands.create_project(
        conn,
        actor_id=actor["id"],
        name=name,
        key=key,
        description=payload.description,
    )


@projects_router.get("/{project_id}", response_model=ProjectOut)
def show_project(
    project_id: RowIdPath,
    response: Response,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    project = projects.get_project(conn, project_id)
    # A private project the caller can't see is a 404, indistinguishable from a missing
    # one, so visibility never leaks through existence — the gate its Mentor twin
    # show_space already applied.
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    return _tagged_project(project, response)


@projects_router.get("/{project_id}/floor")
def project_floor(
    project_id: RowIdPath,
    room: str | None = None,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """One project as a floor of chairs. 404 if missing or hidden."""
    from athena.aegis import office

    floor = office.build_floor(conn, project_id=project_id, actor=actor, room_slug=room)
    if floor is None:
        raise HTTPException(status_code=404, detail="no such project")
    return floor


@projects_router.put("/{project_id}/policy", response_model=ProjectOut)
def set_project_policy(
    project_id: RowIdPath,
    payload: ProjectPolicyUpdate,
    request: Request,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict | JSONResponse:
    """Configure blocked-close governance from an exact reviewed project state."""
    try:
        updated = project_commands.set_blocked_close_policy(
            conn,
            actor=actor,
            project_id=project_id,
            enabled=payload.block_agent_closes_when_blocked,
            if_match=if_match_values(request),
        )
    except project_commands.ProjectPolicyCommandError as exc:
        return _project_policy_error_response(exc)
    return _tagged_project(updated, response)


def _project_for_write(conn: sqlite3.Connection, project_id: int, actor: dict) -> dict:
    """Fetch a project the actor may MODIFY, or raise: 404 if no such project, 403
    if the actor isn't its creator. Edit/delete is creator-only — projects have no
    assignee, so unlike issues there is no second eligible writer. Reading the
    project (and creating one) stay open; only changing or removing an existing
    one is gated here.

    Visibility first, THEN the creator check: a hidden private project must read as
    "no such project" (404), never as "exists but not yours" (403) — otherwise
    PATCH/DELETE is an existence oracle for names the read path deliberately hides.
    Same order as _project_for_privacy and the web's _authorize_project_write."""
    project = projects.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    reason = access.container_write_reason(
        conn,
        actor,
        kind="project",
        container_id=project_id,
        created_by=project["created_by"],
    )
    if reason == "not_visible":
        raise HTTPException(status_code=404, detail="no such project")
    if reason == "not_owner":
        raise HTTPException(
            status_code=403, detail="only the project creator may modify it"
        )
    return project


@projects_router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: RowIdPath,
    payload: ProjectEdit,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator only (404 if missing, 403 if not permitted).
    _project_for_write(conn, project_id, actor)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    name = payload.name.strip() if payload.name is not None else None
    if name is not None:
        if not name:
            raise HTTPException(status_code=422, detail="project name cannot be empty")
        # A rename onto another project's name is the same collision create guards
        # against (409). Renaming to your own current name is fine (NOCASE match
        # on yourself), so only block when the match is a DIFFERENT project.
        clash = projects.get_project_by_name(conn, name)
        if clash is not None and clash["id"] != project_id:
            raise HTTPException(status_code=409, detail="project already exists")
    key = _validate_key(payload.key) if payload.key is not None else None
    if key is not None:
        # Same collision logic as name: a key already held by ANOTHER project is a
        # 409; re-saving your own current key (NOCASE self-match) is fine.
        clash = projects.get_project_by_key(conn, key)
        if clash is not None and clash["id"] != project_id:
            raise HTTPException(status_code=409, detail="project key already in use")
    # The command owns the edit AND its atomic 'edited_project' audit event.
    updated = project_commands.update_project(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        name=name,
        key=key,
        description=payload.description,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such project")
    return updated


@projects_router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    # Creator only (404 if missing, 403 if not permitted).
    _project_for_write(conn, project_id, actor)
    # Refuse rather than cascade/detach: a project that still owns issues must be
    # emptied first (reassign or delete those issues). 409, mirroring the Mentor
    # page-delete-on-children rule — a delete must not silently move data.
    if issues.count_issues_in_project(conn, project_id) > 0:
        raise HTTPException(
            status_code=409, detail="reassign or delete its issues first"
        )
    # sprints.project_id is NOT NULL with no ON DELETE, so a project that owns any
    # sprint would fail the bare DELETE at the FK and surface as a 500 — permanently
    # undeletable. Refuse cleanly (409), same block-don't-cascade rule as issues.
    if sprints.list_sprints(conn, project_id=project_id):
        raise HTTPException(status_code=409, detail="delete its sprints first")
    # The command owns the delete AND its atomic 'deleted_project' event, which
    # outlives the vanished container so the trail keeps who removed it.
    project_commands.delete_project(conn, actor_id=actor["id"], project_id=project_id)


def _project_for_privacy(
    conn: sqlite3.Connection, project_id: int, actor: dict
) -> dict:
    """Fetch a project whose privacy/membership the actor may MANAGE, or raise: 404 if
    no such project OR one the actor can't see, 403 if it's visible but the actor is
    neither its creator nor an admin. The wider twin of _project_for_write (creator-
    only): access administration is creator-OR-admin per the access model.

    Visibility is checked first and collapses to a 404, so a private project the actor
    can't see is indistinguishable from a missing one — its existence never leaks
    through a 403, matching the web twin _authorize_project_manage. A member who CAN see
    it but isn't the creator/admin still gets the honest 403."""
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    if project["created_by"] != actor["id"] and not is_admin(actor):
        raise HTTPException(
            status_code=403,
            detail="only the project creator or an admin may manage access",
        )
    return project


@projects_router.put("/{project_id}/visibility", response_model=ProjectOut)
def set_project_visibility(
    project_id: RowIdPath,
    payload: VisibilityUpdate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator or admin only (404 if missing, 403 otherwise).
    project = _project_for_privacy(conn, project_id, actor)
    visibility = payload.visibility.strip().lower()
    if visibility not in ("public", "private"):
        raise HTTPException(
            status_code=422, detail="visibility must be 'public' or 'private'"
        )
    # Setting it to what it already is is a no-op — no write, no audit event.
    if visibility == project["visibility"]:
        return project
    # The command owns the flip, the creator's roster row when going private, and the
    # atomic visibility event — so an access-control change can never half-land.
    try:
        return project_commands.set_project_visibility(
            conn, actor_id=actor["id"], project_id=project_id, visibility=visibility
        )
    except project_commands.ProjectAccessCommandError as exc:
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=str(exc)
        ) from exc


@projects_router.get("/{project_id}/members", response_model=list[MemberOut])
def list_project_members(
    project_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading the roster is gated by plain visibility: anyone who can see the project
    # sees its members. A private project the caller can't see is a 404 — the roster
    # never reveals that the project (or its members) exist.
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    return access.list_project_members(conn, project_id)


@projects_router.post(
    "/{project_id}/members", response_model=list[MemberOut], status_code=201
)
def add_project_member(
    project_id: RowIdPath,
    payload: MemberAdd,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Creator or admin only. 422 if the user id isn't real (rather than letting the FK
    # surface a 500). Idempotent: re-adding an existing member records no event.
    _project_for_privacy(conn, project_id, actor)
    member = users.get_user(conn, payload.user_id)
    if member is None:
        raise HTTPException(status_code=422, detail="no such user")
    # The command owns the grant and its atomic audit event.
    project_commands.add_project_member(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        user_id=payload.user_id,
        member_name=member["name"],
    )
    return access.list_project_members(conn, project_id)


@projects_router.delete(
    "/{project_id}/members/{user_id}", response_model=list[MemberOut]
)
def remove_project_member(
    project_id: RowIdPath,
    user_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Creator or admin only. 404 if the user wasn't a member (so a no-op delete is an
    # honest miss, not a silent success). The member keeps no access they had via
    # created_by/admin — this only removes the explicit grant.
    _project_for_privacy(conn, project_id, actor)
    member = users.get_user(conn, user_id)
    # The command owns the revoke and its atomic audit event; a non-member is an
    # honest 404 rather than a silent success.
    if not project_commands.remove_project_member(
        conn,
        actor_id=actor["id"],
        project_id=project_id,
        user_id=user_id,
        member_name=member["name"] if member else str(user_id),
    ):
        raise HTTPException(status_code=404, detail="user is not a member")
    return access.list_project_members(conn, project_id)


@projects_router.get("/{project_id}/timeline")
def project_timeline(
    project_id: RowIdPath,
    max_per_lane: int = timeline.DEFAULT_MAX_PER_LANE,
    max_items: int = timeline.DEFAULT_MAX_ITEMS,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The project's roadmap as positioned data rather than markup — the same
    # structure the browser draws, so an agent reads the plan the operator sees.
    # A project the caller cannot see is the same 404 a missing one gives.
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    return timeline.project_timeline(
        conn,
        project_id=project_id,
        visible_project_ids=access.visible_project_filter(conn, actor),
        max_per_lane=max_per_lane,
        max_items=max_items,
    )


@projects_router.get("/{project_id}/statuses", response_model=list[StatusOut])
def list_project_statuses(
    project_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading a project's statuses is open, like listing its issues — but gated by the
    # same visibility: a private project the caller can't see is a 404, so its status
    # vocabulary doesn't leak.
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        raise HTTPException(status_code=404, detail="no such project")
    return statuses.list_statuses(conn, project_id)


@projects_router.post(
    "/{project_id}/statuses", response_model=list[StatusOut], status_code=201
)
def add_project_status(
    project_id: RowIdPath,
    payload: StatusCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Configuring statuses is project config — creator-only, like editing the
    # project itself. The command owns that gate now (from rows read under its own
    # write lock) along with the validation, the write, and the audit event.
    try:
        return status_commands.add_status(
            conn,
            actor=actor,
            project_id=project_id,
            name=payload.name,
            category=payload.category,
        )
    except status_commands.StatusCommandError as exc:
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=str(exc)
        ) from exc


@projects_router.delete("/{project_id}/statuses/{name}", response_model=list[StatusOut])
def remove_project_status(
    project_id: RowIdPath,
    name: str,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    try:
        return status_commands.remove_status(
            conn, actor=actor, project_id=project_id, name=name
        )
    except status_commands.StatusCommandError as exc:
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=str(exc)
        ) from exc
