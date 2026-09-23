"""Browser routes for Mentor spaces: list, edit, access, and daily notes.

Split out of web/mentor.py."""

from __future__ import annotations
import html
import sqlite3
from urllib.parse import quote
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from athena.core import (
    access,
    activity,
    identity,
    notifications,
    users,
)
from athena.core.deps import get_conn
from athena.mentor import (
    page_commands,
    page_templates,
    pages,
    space_commands,
    spaces,
)
from athena.web import html_export
from athena.web.csrf import verify_csrf
from athena.web.router import _readonly_response, get_templates

from athena.web.mentor import (
    _signin_required,
    _tree_rows,
    _write_required,
)

router = APIRouter()


@router.get("/mentor", response_class=HTMLResponse)
def spaces_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """List every space with a New Space form (the form is gated; reading is open).
    Each space links to its page tree. Mirrors /aegis/projects."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    # Only the spaces this viewer may see (public + their own private ones; admins
    # all). A private space never appears here to someone outside it.
    all_spaces = spaces.list_spaces(conn, access.visible_space_filter(conn, user))
    # One page-count per space — cheap on the small lists Mentor holds, and it
    # comes from the real data layer (no cached counter to drift).
    counts = {s["id"]: pages.count_pages_in_space(conn, s["id"]) for s in all_spaces}
    can_write = user is not None and identity.can_write(user)
    return templates.TemplateResponse(
        request=request,
        name="mentor/spaces.html",
        context={
            "spaces": all_spaces,
            "counts": counts,
            "can_write": can_write,
            # An admin may manage access on any space; the creator only on their own.
            "is_admin": user is not None and identity.is_admin(user),
        },
    )


@router.post("/mentor/spaces", dependencies=[Depends(verify_csrf)])
def create_space(
    request: Request,
    key: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create a space as the logged-in user. Mirrors the API's create_space rules:
    key normalized to UPPERCASE (so "eng" == "ENG"), key + name required, duplicate
    key → 409. Actor is the session, never a form field."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "create spaces")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    key = key.strip().upper()
    name = name.strip()
    if not key:
        return HTMLResponse(
            '<div class="error">Space key is required.</div>', status_code=400
        )
    if not name:
        return HTMLResponse(
            '<div class="error">Space name is required.</div>', status_code=400
        )
    if spaces.get_space_by_key(conn, key) is not None:
        return HTMLResponse(
            '<div class="error">A space with that key already exists.</div>',
            status_code=409,
        )

    # The command owns the atomic insert AND its 'space_created' event.
    space = space_commands.create_space(
        conn, actor_id=user["id"], key=key, name=name, description=description.strip()
    )
    return RedirectResponse(f"/mentor/spaces/{space['id']}", status_code=303)


@router.post("/mentor/spaces/{space_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_space(
    request: Request, space_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Delete a space from its detail page. Creator-only — unlike Mentor's open
    create/edit, removing a whole container is gated to its creator (401 logged-out,
    403 non-creator, 404 missing), mirroring the Aegis project delete. Refused with
    409 if the space still holds pages: we don't cascade, so the pages must be moved
    or deleted first. On success the space is gone, so we 303 back to /mentor."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "delete spaces")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    space = spaces.get_space(conn, space_id)
    if space is None:
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    # Shared visibility-first + creator-only rule (access.container_write_reason) — a
    # hidden space reads as "not found" (404), never "exists but not yours" (403).
    reason = access.container_write_reason(
        conn, user, kind="space", container_id=space_id, created_by=space["created_by"]
    )
    if reason == "not_visible":
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    if reason == "not_owner":
        return HTMLResponse(
            '<div class="blocked">Only the space creator may delete it.</div>',
            status_code=403,
        )
    if pages.count_pages_in_space(conn, space_id) > 0:
        return HTMLResponse(
            '<div class="error">Delete or move this space\'s pages first.</div>',
            status_code=409,
        )
    # The command owns the atomic delete AND its 'space_deleted' event.
    space_commands.delete_space(
        conn, actor_id=user["id"], space_id=space_id, name=space["name"]
    )
    return RedirectResponse("/mentor", status_code=303)


@router.get("/mentor/spaces/{space_id}/edit", response_class=HTMLResponse)
def edit_space_form(
    request: Request, space_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the space edit form prefilled with its current key/name/description.
    Editing is a write open to any signed-in actor (only delete is creator-locked),
    so a logged-out caller gets a sign-in prompt rather than a dead form."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit spaces")
    if err is not None:
        return err

    space = spaces.get_space(conn, space_id)
    # Can't prefill a form for a space you can't see — a private space reads as
    # "not found", no leak. Same gate as the POST twin below; without it the GET
    # form leaked a hidden space's key/name/description to any signed-in member.
    if space is None or not access.can_see_space(conn, user, space_id):
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    return templates.TemplateResponse(
        request=request, name="mentor/space_edit.html", context={"space": space}
    )


@router.post("/mentor/spaces/{space_id}/edit", dependencies=[Depends(verify_csrf)])
def edit_space(
    request: Request,
    space_id: int,
    key: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save edits to a space's key/name/description. Open to any session user (like
    create/edit a page); only delete is creator-locked. Mirrors the API: key
    uppercased, key + name required, a key clash with a DIFFERENT space → 409.
    303 back to the space detail on success."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit spaces")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    before = spaces.get_space(conn, space_id)
    # Can't edit a space you can't see — a private space reads as "not found", no leak.
    if before is None or not access.can_see_space(conn, user, space_id):
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    key = key.strip().upper()
    name = name.strip()
    if not key:
        return HTMLResponse(
            '<div class="error">Space key is required.</div>', status_code=400
        )
    if not name:
        return HTMLResponse(
            '<div class="error">Space name is required.</div>', status_code=400
        )
    clash = spaces.get_space_by_key(conn, key)
    if clash is not None and clash["id"] != space_id:
        return HTMLResponse(
            '<div class="error">A space with that key already exists.</div>',
            status_code=409,
        )

    # The command owns the atomic update AND its 'space_edited' event (a no-op change
    # records nothing). A space that vanished in a race 404s rather than 500s.
    try:
        space_commands.edit_space(
            conn,
            actor_id=user["id"],
            space_id=space_id,
            key=key,
            name=name,
            description=description.strip(),
        )
    except space_commands.SpaceCommandError:
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/spaces/{space_id}", status_code=303)


def _authorize_space_manage(conn, space_id: int, user: dict):
    """Resolve a space whose ACCESS (privacy + roster) the user may manage, or an error
    response. Returns (space, None) or (None, HTMLResponse). Creator-OR-admin: a private
    space the user can't see is 404 (no existence leak); a visible one they may see but
    not manage is 403. The 401 (logged-out) check stays at each call site."""
    space = spaces.get_space(conn, space_id)
    if space is None or not access.can_see_space(conn, user, space_id):
        return None, HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    if not identity.can_write(user):
        return None, _readonly_response()
    if space["created_by"] != user["id"] and not identity.is_admin(user):
        return None, HTMLResponse(
            '<div class="blocked">Only the space creator or an admin may manage access.</div>',
            status_code=403,
        )
    return space, None


@router.get("/mentor/spaces/{space_id}/access", response_class=HTMLResponse)
def space_access(
    request: Request, space_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """The access page for a space: its visibility with a public/private toggle, and —
    when private — the member roster with add/remove. Creator-or-admin (401/403/404)."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage access")
    space, err = _authorize_space_manage(conn, space_id, user)
    if err is not None:
        return err
    members = access.list_space_members(conn, space_id)
    member_ids = {m["user_id"] for m in members}
    addable = [u for u in users.list_users(conn) if u["id"] not in member_ids]
    return templates.TemplateResponse(
        request=request,
        name="mentor/space_access.html",
        context={"space": space, "members": members, "addable": addable},
    )


@router.post(
    "/mentor/spaces/{space_id}/visibility", dependencies=[Depends(verify_csrf)]
)
def space_set_visibility(
    request: Request,
    space_id: int,
    visibility: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Flip a space public ↔ private from its access page. Creator-or-admin. Going
    private auto-adds the creator to the roster. 303 back to the access page."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage access")
    space, err = _authorize_space_manage(conn, space_id, user)
    if err is not None:
        return err
    visibility = visibility.strip().lower()
    if visibility not in ("public", "private"):
        return HTMLResponse(
            '<div class="error">Visibility must be public or private.</div>',
            status_code=400,
        )
    if visibility != space["visibility"]:
        # The command owns the atomic flip, the creator-as-member add (going private),
        # and the visibility event.
        space_commands.set_space_visibility(
            conn, actor_id=user["id"], space_id=space_id, visibility=visibility
        )
    return RedirectResponse(f"/mentor/spaces/{space_id}/access", status_code=303)


@router.post("/mentor/spaces/{space_id}/members", dependencies=[Depends(verify_csrf)])
def space_add_member(
    request: Request,
    space_id: int,
    user_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Grant a user access to a private space from its access page. Creator-or-admin.
    400 on a missing/blank user; a re-add is idempotent. 303 back to the access page."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage access")
    _, err = _authorize_space_manage(conn, space_id, user)
    if err is not None:
        return err
    member = users.get_user(conn, int(user_id)) if user_id.strip().isdigit() else None
    if member is None:
        return HTMLResponse('<div class="error">No such user.</div>', status_code=400)
    # The command owns the atomic grant AND its 'space_member_added' event (idempotent).
    space_commands.add_space_member(
        conn,
        actor_id=user["id"],
        space_id=space_id,
        user_id=member["id"],
        member_name=member["name"],
    )
    return RedirectResponse(f"/mentor/spaces/{space_id}/access", status_code=303)


@router.post(
    "/mentor/spaces/{space_id}/members/{member_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def space_remove_member(
    request: Request,
    space_id: int,
    member_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Revoke a user's space membership from its access page. Creator-or-admin. A no-op
    (they weren't a member) still 303s back — the roster reflects reality."""
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("manage access")
    _, err = _authorize_space_manage(conn, space_id, user)
    if err is not None:
        return err
    member = users.get_user(conn, member_id)
    # The command owns the atomic revoke AND its 'space_member_removed' event; a no-op
    # (they weren't a member) records nothing and still 303s back.
    space_commands.remove_space_member(
        conn,
        actor_id=user["id"],
        space_id=space_id,
        user_id=member_id,
        member_name=member["name"] if member else str(member_id),
    )
    return RedirectResponse(f"/mentor/spaces/{space_id}/access", status_code=303)


@router.get("/mentor/spaces/{space_id}", response_class=HTMLResponse)
def space_detail(
    request: Request, space_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """A space's page tree plus a New Page form (gated). 404-ish empty render if the
    space doesn't exist (consistent with the Aegis not-found handling)."""
    templates = get_templates()

    user = getattr(request.state, "user", None)
    space = spaces.get_space(conn, space_id)
    # A private space the viewer can't see is treated exactly like a missing one — same
    # 404, and the fallback space list is itself gated so it never leaks private names.
    if space is None or not access.can_see_space(conn, user, space_id):
        return templates.TemplateResponse(
            request=request,
            name="mentor/spaces.html",
            context={
                "spaces": spaces.list_spaces(
                    conn, access.visible_space_filter(conn, user)
                ),
                "counts": {},
                "can_write": False,
                "error": f"Space #{space_id} not found",
            },
            status_code=404,
        )

    # The "Show archived" toggle submits a truthy value to include soft-deleted pages
    # in the tree (so they can be found and restored); by default they're hidden.
    archived_raw = (request.query_params.get("archived") or "").strip()
    include_archived = archived_raw.lower() in ("1", "true", "on", "yes")
    page_rows = pages.list_pages_in_space(
        conn, space_id, include_archived=include_archived
    )
    can_write = user is not None and identity.can_write(user)
    return templates.TemplateResponse(
        request=request,
        name="mentor/space_detail.html",
        context={
            "space": space,
            "tree": _tree_rows(page_rows),
            # Flat list (alpha) for the optional "nest under" parent select.
            "all_pages": page_rows,
            # The space's template pages, driving the "new page from template"
            # picker. Empty is the normal case for a space that has not set any
            # up, and the picker simply does not render.
            "templates": page_templates.list_templates(conn, space_id),
            # Drives the danger zone: only the creator sees Delete (creator-only,
            # tighter than Mentor's open write model), and it's disabled while the
            # space still holds pages (the API would refuse that delete with 409).
            "can_write": can_write,
            "can_delete": user is not None
            and can_write
            and user["id"] == space["created_by"],
            "page_count": len(page_rows),
            "is_watching": user is not None
            and notifications.is_watching(conn, user["id"], "space", space_id),
            "include_archived": include_archived,
            "activity": activity.list_activity(
                conn, target_kind="space", target_id=space_id
            ),
        },
    )


@router.post("/mentor/spaces/{space_id}/daily", dependencies=[Depends(verify_csrf)])
def open_daily_note(
    request: Request, space_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Open today's daily note in this space, creating it on the first visit.

    The operator's morning page: one button, always the same page for a given day.
    Idempotency lives in the command (find-or-create in one transaction), not here,
    so a double-click cannot produce two notes.
    """
    user = getattr(request.state, "user", None)
    err = _write_required(user, "open the daily note")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    if spaces.get_space(conn, space_id) is None or not access.can_see_space(
        conn, user, space_id
    ):
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    try:
        page, _created = page_commands.ensure_daily_page(
            conn, actor=user, space_id=space_id
        )
    except page_commands.PageCommandError:
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/pages/{page['id']}", status_code=303)


@router.post(
    "/mentor/spaces/{space_id}/pages/from-template", dependencies=[Depends(verify_csrf)]
)
def create_page_from_template(
    request: Request,
    space_id: int,
    template_id: int = Form(...),
    title: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create a page whose body starts as a template's. The command re-checks that
    the chosen page is still a template under the write lock."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "create pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    if spaces.get_space(conn, space_id) is None or not access.can_see_space(
        conn, user, space_id
    ):
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    title = title.strip()
    if not title:
        return HTMLResponse(
            '<div class="error">Page title is required.</div>', status_code=400
        )
    try:
        page = page_commands.create_page_from_template(
            conn,
            actor=user,
            space_id=space_id,
            template_id=template_id,
            title=title,
        )
    except page_commands.PageCommandError as exc:
        return HTMLResponse(
            f'<div class="error">{html.escape(exc.detail)}</div>',
            status_code=404 if exc.kind == "not_found" else 422,
        )
    return RedirectResponse(f"/mentor/pages/{page['id']}", status_code=303)


@router.post("/mentor/spaces/{space_id}/pages", dependencies=[Depends(verify_csrf)])
def create_page(
    request: Request,
    space_id: int,
    title: str = Form(""),
    body: str = Form(""),
    parent_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Create a page in a space as the logged-in user. Mirrors the API: 404 if the
    space is missing, title required, and an optional parent must be a real page IN
    THIS SAME SPACE (the cross-space tree rule the FK can't express). 303 to the new
    page on success."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "create pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    # Can't add a page to a space you can't see — a private space reads as "not found".
    if spaces.get_space(conn, space_id) is None or not access.can_see_space(
        conn, user, space_id
    ):
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )

    title = title.strip()
    if not title:
        return HTMLResponse(
            '<div class="error">Page title is required.</div>', status_code=400
        )

    parent_id = parent_id.strip()
    if parent_id == "":
        parent: int | None = None
    else:
        if not parent_id.isdigit():
            return HTMLResponse(
                '<div class="error">Invalid parent page.</div>', status_code=400
            )
        parent_page = pages.get_page(conn, int(parent_id))
        if parent_page is None or parent_page["space_id"] != space_id:
            return HTMLResponse(
                '<div class="error">Parent must be a page in this space.</div>',
                status_code=400,
            )
        parent = int(parent_id)

    # The command owns the atomic insert AND its 'page_created' event (auto-watch +
    # mentions).
    try:
        page = page_commands.create_page(
            conn,
            actor=user,
            space_id=space_id,
            title=title,
            body=body.strip() or "",
            parent_id=parent,
        )
    except page_commands.PageCommandError:
        return HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/pages/{page['id']}", status_code=303)


@router.get("/mentor/spaces/{space_id}/export.html")
def export_space_html(
    request: Request,
    space_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Download this space as one standalone HTML file — the human-readable exit.

    A read, gated exactly like the space itself: a space you cannot see is the
    same 404 a missing one gives. The file contains only what YOU could see when
    you asked for it, and says so in its own footer.
    """
    user = getattr(request.state, "user", None)
    document = html_export.build_space_html(conn, space_id, actor=user)
    if document is None:
        return HTMLResponse('<div class="error">No such space.</div>', status_code=404)
    space = spaces.get_space(conn, space_id)
    assert space is not None  # build_space_html already refused a missing space
    name = f"athena-{space['key'].lower()}.html"
    encoded = quote(name)
    disposition = (
        f'attachment; filename="{name}"'
        if encoded == name
        else f"attachment; filename*=utf-8''{encoded}"
    )
    return Response(
        document,
        media_type="text/html; charset=utf-8",
        headers={"content-disposition": disposition},
    )


def _space_visible_or_response(conn, space_id: int, user):
    """(space, None) when this user may read the space, (None, 404 response) otherwise —
    the space twin of _page_visible_or_response, so "private" and "missing" stay
    indistinguishable to someone who may not see it.

    The message is a fixed string, like the page twin's: echoing the requested id back
    into HTML puts a request-derived value in a response body for no benefit, and the
    reply is more honest without it — naming the id would confirm which id was asked
    about, which is the one thing a not-found is supposed to stay quiet about."""
    space = spaces.get_space(conn, space_id)
    if space is None or not access.can_see_space(conn, user, space_id):
        return None, HTMLResponse(
            '<div class="error">Space not found.</div>', status_code=404
        )
    return space, None


@router.post("/mentor/spaces/{space_id}/watch", dependencies=[Depends(verify_csrf)])
def watch_space(request: Request, space_id: int, conn=Depends(get_conn)):
    """Subscribe to a space: every page event inside it lands in your inbox (any
    signed-in user — a personal subscription, like watching a page)."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to watch.</div>',
            status_code=401,
        )
    # You can't watch what you can't see — a hidden space is "not found", exactly as
    # for pages, so a subscription can never become a side channel for its existence.
    space, err = _space_visible_or_response(conn, space_id, user)
    if err is not None:
        return err
    assert space is not None
    # Redirect to the id off the SPACE ROW, not the path parameter — the same shape
    # the create route uses. The value is identical; the provenance is not, and a
    # redirect target that came out of the database cannot carry anything a request
    # put into it.
    notifications.watch(conn, user["id"], "space", space["id"])
    return RedirectResponse(f"/mentor/spaces/{space['id']}", status_code=303)


@router.post("/mentor/spaces/{space_id}/unwatch", dependencies=[Depends(verify_csrf)])
def unwatch_space(request: Request, space_id: int, conn=Depends(get_conn)):
    """Stop watching a space."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    space, err = _space_visible_or_response(conn, space_id, user)
    if err is not None:
        return err
    assert space is not None
    notifications.unwatch(conn, user["id"], "space", space["id"])
    return RedirectResponse(f"/mentor/spaces/{space['id']}", status_code=303)
