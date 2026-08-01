"""Mentor web surface: the browser-facing thin client over the spaces/pages API.

Mirrors web/auth.py's split-router shape (its own APIRouter, templates fetched
via web.router.get_templates) and web/router.py's Aegis conventions. It owns NO
data: every route reads through mentor.spaces / mentor.pages (the same data-access
the REST API uses) and every write is gated on the browser session
(request.state.user), never a form field — the cardinal AGENTS.md rule.

Mentor's authorization is deliberately simpler than Aegis's: reads are open and
writes are open to ANY authenticated actor (a page has no creator-only lock — it's
a shared wiki and every edit is snapshotted into history), so the only gate is
"are you signed in?" — there is no creator-or-assignee check like issues have.
"""

from __future__ import annotations

import html
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from athena import config
from athena.core import (
    access,
    activity,
    attachment_commands,
    attachments,
    graph,
    identity,
    labels,
    links,
    mentions,
    notifications,
    users,
)
from athena.core.deps import get_conn
from athena.mentor import (
    page_commands,
    page_drafts,
    page_etags,
    page_templates,
    page_comment_commands,
    page_comments,
    pages,
    space_commands,
    spaces,
)
from markupsafe import escape

from athena.web import html_export
from athena.web.csrf import verify_csrf
from athena.web.render import MAX_PREVIEW_CHARS, render_comment, render_page_body
from athena.web.router import _readonly_response, get_templates

router = APIRouter()


def _signin_required(verb: str) -> HTMLResponse:
    """The 401 body shown when a logged-out browser tries to write."""
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


def _write_required(user: dict | None, verb: str) -> HTMLResponse | None:
    if user is None:
        return _signin_required(verb)
    if not identity.can_write(user):
        return _readonly_response()
    return None


def _page_visible_or_response(conn, page_id, user):
    """Return (page, None) if the user may SEE this page (its space is visible to them),
    else (None, 404 response). The web write-side visibility gate — a page in a private
    space the user can't read is "not found", so its existence and content never leak
    through a write path. The browser twin of the API's _page_for_read; every page write
    funnels through here so visibility can't be forgotten on one."""
    page = pages.get_page(conn, page_id)
    if page is None or not access.can_see_space(conn, user, page["space_id"]):
        return None, HTMLResponse(
            '<div class="error">Page not found.</div>', status_code=404
        )
    return page, None


def _tree_rows(page_rows: list[dict]) -> list[dict]:
    """Flatten a space's pages into display order with a nesting depth on each.

    The data layer hands us a flat list (alphabetical by title, each row carrying
    its parent_id). Shaping that into a tree is a presentation concern, so it lives
    here, not in SQL. We do a depth-first walk: each parent is immediately followed
    by its children (already alphabetical because the source list is), and every
    row gets a `depth` the template indents by. A page whose parent isn't in this
    set is treated as a root so nothing can silently vanish from the tree.
    """
    children: dict[int | None, list[dict]] = {}
    ids = {p["id"] for p in page_rows}
    for p in page_rows:
        parent = p["parent_id"] if p["parent_id"] in ids else None
        children.setdefault(parent, []).append(p)

    ordered: list[dict] = []

    def walk(parent_id: int | None, depth: int) -> None:
        for child in children.get(parent_id, []):
            ordered.append({**child, "depth": depth})
            walk(child["id"], depth + 1)

    walk(None, 0)
    return ordered


# --- The knowledge graph ----------------------------------------------------


@router.get("/mentor/pages/{page_id}/graph", response_class=HTMLResponse)
def page_graph(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """A page's neighbourhood: the bounded link graph plus its unlinked mentions.

    This is a SEPARATE route rather than a panel on the page itself, and that is a
    cost decision worth stating. A mention scan is a full-text query plus a body
    read per candidate, and a graph is a breadth-first walk with a visibility check
    per node; putting either on every page view would tax reading — the thing
    people do most — to serve the thing they do occasionally. One click keeps page
    rendering flat.
    """
    templates = get_templates()
    user = getattr(request.state, "user", None)
    page, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    assert page is not None
    return templates.TemplateResponse(
        request=request,
        name="knowledge.html",
        context={
            "subject_title": page["title"],
            "back_url": f"/mentor/pages/{page_id}",
            "target_kind": "page",
            "target_id": page_id,
            # Both reads take the VIEWER, never the page's author: the graph and
            # the mention list are each capable of revealing private work through
            # a public page, so they are gated exactly like every other read.
            "graph": graph.ego_graph(conn, kind="page", node_id=page_id, actor=user),
            "mentions": mentions.unlinked_mentions(
                conn, kind="page", target_id=page_id, actor=user
            ),
            "can_write": user is not None and identity.can_write(user),
        },
    )


@router.post(
    "/mentor/pages/{page_id}/link-mention", dependencies=[Depends(verify_csrf)]
)
def link_page_mention(
    request: Request,
    page_id: int,
    target_kind: str = Form(...),
    target_id: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Take one proposed edge: rewrite THIS page's body so its first unlinked
    mention of the target becomes a real reference.

    The page in the path is the SOURCE — the document being edited — so this is an
    ordinary page edit and goes through the ordinary page command, with its event,
    its version snapshot, and its attribution. Nothing here writes to the target.
    """
    user = getattr(request.state, "user", None)
    err = _write_required(user, "link mentions")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    page, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    assert page is not None
    if target_kind not in ("issue", "page"):
        return HTMLResponse('<div class="error">Unknown target.</div>', status_code=422)
    needle = mentions.mention_text(conn, target_kind, target_id)
    if not needle:
        return HTMLResponse(
            '<div class="error">That link target no longer exists.</div>',
            status_code=404,
        )
    body = mentions.linkify_first(
        page["body"] or "", needle, mentions.link_token(conn, target_kind, target_id)
    )
    if body is None:
        # The body changed under the operator; the mention they clicked is gone.
        # Refusing is the honest answer — editing anyway would rewrite text they
        # never saw.
        return HTMLResponse(
            '<div class="error">That mention is no longer in this page.</div>',
            status_code=409,
        )
    try:
        page_commands.edit_page(conn, actor_id=user["id"], page_id=page_id, body=body)
    except page_commands.PageCommandError:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    back = (
        f"/mentor/pages/{target_id}/graph"
        if target_kind == "page"
        else (f"/aegis/issues/{target_id}/graph")
    )
    return RedirectResponse(back, status_code=303)


# --- Spaces -----------------------------------------------------------------


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


# --- Space access: privacy toggle + member management (web) ----------------
#
# The Mentor twin of the project access page. Managing access is creator-OR-admin
# (wider than delete, creator-only), so it uses its own gate rather than _write_required.


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
    page, _created = page_commands.ensure_daily_page(
        conn, actor_id=user["id"], space_id=space_id
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
            actor_id=user["id"],
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
    page = page_commands.create_page(
        conn,
        actor_id=user["id"],
        space_id=space_id,
        title=title,
        body=body.strip() or "",
        parent_id=parent,
    )
    return RedirectResponse(f"/mentor/pages/{page['id']}", status_code=303)


# --- Pages ------------------------------------------------------------------


@router.get("/mentor/pages/{page_id}", response_class=HTMLResponse)
def page_detail(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Show one page: its current title/body, the space it belongs to, an Edit link
    (logged-in only), and its version history (superseded revisions, newest first)."""
    templates = get_templates()

    user = getattr(request.state, "user", None)
    page = pages.get_page(conn, page_id)
    # A page in a private space the viewer can't see is a 404, gated by its space, so
    # privacy never leaks through the existence of a page id.
    if page is None or not access.can_see_space(conn, user, page["space_id"]):
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)

    # One read of the space's pages serves two needs: the navigation tree (so you can
    # jump to any page in the space without going back to its index — the Confluence
    # left-rail idea), and the "Move under" candidates (every OTHER page; self can't
    # be its own parent — descendants stay in and are rejected by validate_move).
    page_rows = pages.list_pages_in_space(conn, page["space_id"])
    tree = _tree_rows(page_rows)
    siblings = [p for p in page_rows if p["id"] != page_id]
    # Breadcrumb trail: walk up parent_id (using the in-memory page map, no extra
    # queries) to collect this page's ancestors, root-first. The seen-set guards
    # against any pre-existing cycle so the walk always terminates.
    by_id = {p["id"]: p for p in page_rows}
    ancestors: list[dict] = []
    seen: set[int] = set()
    cursor = page.get("parent_id")
    while cursor is not None and cursor in by_id and cursor not in seen:
        seen.add(cursor)
        ancestors.append(by_id[cursor])
        cursor = by_id[cursor].get("parent_id")
    ancestors.reverse()
    # `user` was resolved above (for the visibility gate); reuse it here.
    can_write = user is not None and identity.can_write(user)
    # The discussion thread, oldest first. Each body is rendered the same way an
    # issue comment is: escaped plain text with [[user:N]] mentions resolved to
    # @Name (render_comment). Comments deliberately do NOT resolve [[page:N]]/
    # [[issue:N]] cross-links — that richer pass is for page/issue bodies only.
    comment_rows = page_comments.list_comments(conn, page_id)
    for comment in comment_rows:
        comment["body_html"] = render_comment(conn, comment["body"])
    return templates.TemplateResponse(
        request=request,
        name="mentor/page_detail.html",
        context={
            "page": page,
            # Embeds resolve HERE, per request, against the VIEWER — never the
            # author. An admin's `q: is:open` renders for a member only the work
            # that member could already see, and nothing is cached between
            # viewers, because a cache keyed on the page would serve one reader's
            # visibility to another.
            "body_html": render_page_body(conn, page["body"], actor=user),
            "comments": comment_rows,
            "page_labels": labels.labels_for_page(conn, page_id),
            "all_labels": labels.list_labels(
                conn
            ),  # the shared vocabulary, for autocomplete
            "attachments": attachments.list_for(conn, "page", page_id),
            # The types the download route serves inline — the template offers a
            # thumbnail and an embed snippet for exactly these, so the affordance
            # and the actual behaviour come from one list.
            "inline_image_types": attachments.INLINE_CONTENT_TYPES,
            "is_watching": user is not None
            and notifications.is_watching(conn, user["id"], "page", page_id),
            "backlinks": links.backlinks(conn, "page", page_id, actor=user),
            "space": spaces.get_space(conn, page["space_id"]),
            "ancestors": ancestors,
            "tree": tree,
            "versions": pages.list_page_versions(conn, page_id),
            "activity": activity.list_activity(
                conn, target_kind="page", target_id=page_id
            ),
            "move_candidates": siblings,
            "can_write": can_write,
            # Admins may moderate (delete) any comment, not just their own — drives the
            # per-comment Delete control the same way the server-side override gates it.
            "is_admin": user is not None and identity.is_admin(user),
            # Drives the Delete button: a page with children can't be deleted.
            "child_count": pages.count_child_pages(conn, page_id),
        },
    )


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


@router.get("/mentor/pages/{page_id}/edit", response_class=HTMLResponse)
def edit_page_form(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the edit form prefilled with the page's current title/body. Editing is
    a write, so logged-out callers get a sign-in prompt rather than a dead form."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None  # _write_required refused a missing user above

    page = pages.get_page(conn, page_id)
    # Can't edit (or even see the form for) a page in a space you can't read — 404,
    # same as a missing page, so the form never leaks a hidden page's content.
    if page is None or not access.can_see_space(conn, user, page["space_id"]):
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    draft = page_drafts.get_draft(conn, page_id=page_id, owner_id=user["id"])
    if draft is not None and not page_drafts.differs_from(draft, page):
        # Identical to the saved page: not unsaved work, so offering to restore
        # it would just make an author wonder what they had forgotten.
        draft = None
    return templates.TemplateResponse(
        request=request,
        name="mentor/page_edit.html",
        context={
            "page": page,
            "space": spaces.get_space(conn, page["space_id"]),
            # The preview starts populated rather than blank: an author opening
            # an existing page sees it as readers do before touching a key.
            "body_html": render_page_body(conn, page["body"], actor=user),
            # An unsaved draft is OFFERED, never applied: the form still shows
            # the saved page, and restoring is a decision the author makes. A
            # draft that merely matches the page is not offered at all.
            "draft": draft,
            # Restoring is a read: the form renders the draft's text instead of
            # the page's. Nothing is written until the author presses Save.
            "restored": draft is not None
            and request.query_params.get("restore") == "1",
            "draft_is_stale": draft is not None
            and page_drafts.is_stale(draft, page_etags.current_etag(conn, page)),
            # The baseline this editing session starts FROM. The form carries it
            # through every autosave, so a draft records the page the author
            # actually saw — not whatever the page had become by the time the
            # autosave timer fired (which would defeat the stale-draft warning).
            "page_etag": page_etags.current_etag(conn, page),
            "notice": (request.query_params.get("notice") or "").strip(),
        },
    )


@router.post("/mentor/pages/preview", dependencies=[Depends(verify_csrf)])
def preview_page_body(
    request: Request,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Render unsaved page text exactly as the page view will render it.

    This calls ``render_page_body`` — the SAME function the page itself calls —
    so the preview cannot drift from the display. It renders against the signed-in
    viewer, which matters: cross-links and embeds resolve per reader, so a preview
    rendered as anyone else would be a preview of someone else's page.

    Nothing is written. A preview is a read of text the author has in hand.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return _signin_required("preview")
    if len(body) > MAX_PREVIEW_CHARS:
        return HTMLResponse(
            '<div class="error">Too long to preview.</div>', status_code=413
        )
    return HTMLResponse(str(render_page_body(conn, body, actor=user)))


@router.post("/mentor/pages/{page_id}/draft", dependencies=[Depends(verify_csrf)])
def autosave_page_draft(
    request: Request,
    page_id: int,
    title: str = Form(""),
    body: str = Form(""),
    based_on: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Record where this author has got to, without touching the page.

    Nothing here writes to ``pages``: no version is cut, no activity event is
    recorded, no watcher is notified. That is the entire point — a crashed
    browser should cost nothing, and the trail should still say nothing happened
    until a human decides something did.

    Answers a small fragment the editor swaps in, so the author can see their
    work is held without the page moving under them.
    """
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None
    page, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    assert page is not None
    try:
        saved = page_drafts.save_draft(
            conn,
            page_id=page_id,
            owner_id=user["id"],
            title=title,
            body=body,
            # The etag the EDITOR RENDERED WITH, carried by the form — never
            # re-read here. Stamping the current etag at autosave time would
            # mark a draft fresh the moment someone else saved, which is the
            # exact moment the stale warning exists for. Blank only for a
            # cached pre-upgrade form; falling back to the current etag there
            # restores the old (weaker) behavior instead of refusing the save.
            based_on=based_on.strip() or page_etags.current_etag(conn, page),
        )
    except page_drafts.DraftTooLarge as exc:
        return HTMLResponse(f'<div class="error">{escape(str(exc))}</div>', 413)
    return HTMLResponse(
        f'<span class="draft-saved">Draft held {escape(saved["updated_at"])}</span>'
    )


@router.post(
    "/mentor/pages/{page_id}/draft/discard", dependencies=[Depends(verify_csrf)]
)
def discard_page_draft(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Throw away this author's draft of this page. Affects nobody else."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None
    page_drafts.discard_draft(conn, page_id=page_id, owner_id=user["id"])
    return RedirectResponse(
        f"/mentor/pages/{page_id}/edit?notice=Draft+discarded.", status_code=303
    )


@router.post("/mentor/pages/{page_id}/edit", dependencies=[Depends(verify_csrf)])
def edit_page(
    request: Request,
    page_id: int,
    title: str = Form(""),
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save edits to a page's title/body. Gated on the session user; empty title is
    rejected. update_page snapshots the prior revision into history before
    overwriting (see mentor/pages.py), then we 303 back to the page."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    title = title.strip()
    if not title:
        return HTMLResponse(
            '<div class="error">Title is required.</div>', status_code=400
        )

    # The command owns the atomic snapshot+overwrite and its 'page_edited' event; the
    # browser form carries no If-Match, so this stays last-write-wins (the optimistic
    # lock is a REST/MCP concern for concurrent agents). A page that vanished between
    # the visibility check and the write (a race) 404s rather than 500s.
    try:
        page_commands.edit_page(
            conn, actor_id=user["id"], page_id=page_id, title=title, body=body.strip()
        )
    except page_commands.PageCommandError:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    # The text IS the page now, so the author's draft of it is a stale copy of
    # something that finally has a real home and a version row. Dropping it is
    # what makes "you have unsaved work" mean it the next time it appears.
    page_drafts.discard_draft(conn, page_id=page_id, owner_id=user["id"])
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post("/mentor/pages/{page_id}/move", dependencies=[Depends(verify_csrf)])
def move_page(
    request: Request,
    page_id: int,
    parent_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Re-parent a page from its detail page. Gated on the session user. An empty
    parent value means "move to the top level"; otherwise it must be a page id, and
    validate_move enforces same-space + no-cycle; its message is a fixed internal
    string today but we HTML-escape it on the way out so this stays safe even if
    the predicate ever grows to echo user input. 303 back so the new breadcrumb shows."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "move pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    page, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err

    parent_id = parent_id.strip()
    if parent_id == "":
        new_parent: int | None = None
    else:
        if not parent_id.isdigit():
            return HTMLResponse(
                '<div class="error">Invalid parent page.</div>', status_code=400
            )
        new_parent = int(parent_id)

    # The command owns the atomic re-parent AND its 'page_moved' event. An illegal move
    # (another space, self, a descendant) comes back as PageCommandError('invalid').
    try:
        page_commands.move_page(
            conn, actor_id=user["id"], page_id=page_id, new_parent_id=new_parent
        )
    except page_commands.PageCommandError as exc:
        if exc.kind == "not_found":
            return HTMLResponse(
                '<div class="error">Page not found.</div>', status_code=404
            )
        return HTMLResponse(
            f'<div class="error">{html.escape(exc.detail)}</div>', status_code=400
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post("/mentor/pages/{page_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_page(
    request: Request,
    page_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delete a page from its detail page. Gated on the session user. Refuses (409)
    if the page still has children — same no-cascade rule as the API. On success the
    page is gone, so we 303 to the space it lived in (captured before the delete)."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "delete pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    page, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    if pages.count_child_pages(conn, page_id) > 0:
        return HTMLResponse(
            '<div class="error">Move or delete its child pages first.</div>',
            status_code=409,
        )
    space_id = page["space_id"]
    # The command owns the atomic delete AND its 'page_deleted' event, then the
    # post-commit blob unlink + index maintenance.
    page_commands.delete_page(
        conn, actor_id=user["id"], page_id=page_id, title=page["title"]
    )
    return RedirectResponse(f"/mentor/spaces/{space_id}", status_code=303)


@router.post("/mentor/pages/{page_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_page(
    request: Request,
    page_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Archive (soft-delete) a page from its detail page — the reversible alternative
    to Delete. Gated on the session user; the command owns the flip AND its atomic
    'page_archived' event. 303 back to the page (it still exists, just archived)."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "archive pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    try:
        page_commands.set_page_archived(
            conn, actor_id=user["id"], page_id=page_id, archived=True
        )
    except page_commands.PageCommandError:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post("/mentor/pages/{page_id}/unarchive", dependencies=[Depends(verify_csrf)])
def unarchive_page(
    request: Request,
    page_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Restore an archived page from its detail page. Gated on the session user; the
    command records 'page_unarchived' only if it was actually archived."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "restore pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    try:
        page_commands.set_page_archived(
            conn, actor_id=user["id"], page_id=page_id, archived=False
        )
    except page_commands.PageCommandError:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post("/mentor/pages/{page_id}/attachments", dependencies=[Depends(verify_csrf)])
def add_page_attachment(
    request: Request,
    page_id: int,
    file: UploadFile = File(...),
    conn=Depends(get_conn),
):
    """Attach a file to a page from its detail page. Open write like editing a page.
    Empty → 400, oversize → 413; otherwise 303 back to the page."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "attach files")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    data = file.file.read()
    if not data:
        return HTMLResponse('<div class="error">File is empty.</div>', status_code=400)
    if len(data) > config.ATTACH_MAX_BYTES:
        return HTMLResponse(
            '<div class="error">File is too large.</div>', status_code=413
        )
    try:
        attachment_commands.create_attachment(
            conn,
            actor=user,
            target_kind="page",
            target_id=page_id,
            filename=file.filename,
            content_type=file.content_type,
            data=data,
            attach_dir=config.ATTACH_DIR,
        )
    except attachment_commands.AttachmentCommandError as exc:
        return HTMLResponse(
            f'<div class="error">{html.escape(str(exc).capitalize())}.</div>',
            status_code=exc.status_code,
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post(
    "/mentor/pages/{page_id}/attachments/{attachment_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_page_attachment(
    request: Request,
    page_id: int,
    attachment_id: int,
    conn=Depends(get_conn),
):
    """Delete a page attachment. Uploader-only. POST because forms can't DELETE."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "remove files")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    att = attachments.get(conn, attachment_id)
    if att is None or att["target_kind"] != "page" or att["target_id"] != page_id:
        return HTMLResponse(
            '<div class="error">Attachment not found.</div>', status_code=404
        )
    if att["uploaded_by"] != user["id"]:
        return HTMLResponse(
            '<div class="error">Only the uploader may remove this file.</div>',
            status_code=403,
        )
    try:
        attachment_commands.remove_attachment(
            conn,
            actor=user,
            attachment_id=attachment_id,
            attach_dir=config.ATTACH_DIR,
        )
    except attachment_commands.AttachmentCommandError as exc:
        return HTMLResponse(
            f'<div class="error">{html.escape(str(exc).capitalize())}.</div>',
            status_code=exc.status_code,
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post("/mentor/pages/{page_id}/watch", dependencies=[Depends(verify_csrf)])
def watch_page(request: Request, page_id: int, conn=Depends(get_conn)):
    """Start watching a page (any signed-in user — a personal subscription)."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to watch.</div>',
            status_code=401,
        )
    # You can't watch what you can't see (and a subscription would later leak the page
    # through notifications) — a hidden page is "not found".
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    notifications.watch(conn, user["id"], "page", page_id)
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post("/mentor/pages/{page_id}/unwatch", dependencies=[Depends(verify_csrf)])
def unwatch_page(request: Request, page_id: int, conn=Depends(get_conn)):
    """Stop watching a page."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a>.</div>',
            status_code=401,
        )
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    notifications.unwatch(conn, user["id"], "page", page_id)
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post(
    "/mentor/pages/{page_id}/versions/{version}/restore",
    dependencies=[Depends(verify_csrf)],
)
def restore_version(
    request: Request,
    page_id: int,
    version: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Restore a page to one of its prior revisions from the history table. Gated on
    the session user, like edit/move (restore IS an edit — the current content is
    kept as a new version, so it's reversible and needs no creator lock). 404 if the
    page or that version is missing; 303 back to the page, which now shows the
    restored content with the previously-live revision added to its history."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "restore pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"

    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    # The command owns the atomic restore AND its 'page_restored' event; a missing
    # page/version comes back as PageCommandError('not_found').
    try:
        page_commands.restore_page_version(
            conn, actor_id=user["id"], page_id=page_id, version=version
        )
    except page_commands.PageCommandError:
        return HTMLResponse(
            '<div class="error">No such page or version.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


# --- Page comments ----------------------------------------------------------


@router.post("/mentor/pages/{page_id}/comments", dependencies=[Depends(verify_csrf)])
def add_page_comment(
    request: Request,
    page_id: int,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Post a comment on a page from its detail page. Gated on the session user (the
    author is the session, never a form field), then 303 back so the new comment
    shows. Mirrors the Aegis issue-comment web route."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "comment")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse(
            '<div class="error">Comment cannot be empty.</div>', status_code=400
        )
    # The command owns the insert AND its atomic 'page_commented' event (auto-watch + mentions).
    page_comment_commands.create_page_comment(
        conn, actor_id=user["id"], page_id=page_id, body=body
    )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


def _own_page_comment_or_response(
    conn, page_id, comment_id, user, *, allow_admin=False
):
    """Return the comment if it belongs to this page and the session user is its
    author; otherwise an HTMLResponse (404/403) to return as-is. Mirrors the API's
    author-ownership rule on the web write paths. allow_admin lets an admin through
    for moderation — used only on delete, matching the API override; edit stays
    author-only."""
    existing = page_comments.get_comment(conn, comment_id)
    if existing is None or existing["page_id"] != page_id:
        return None, HTMLResponse(
            '<div class="error">Comment not found.</div>', status_code=404
        )
    if existing["author_id"] != user["id"] and not (
        allow_admin and identity.is_admin(user)
    ):
        return None, HTMLResponse(
            '<div class="error">You can only change your own comments.</div>',
            status_code=403,
        )
    return existing, None


@router.post(
    "/mentor/pages/{page_id}/comments/{comment_id}/edit",
    dependencies=[Depends(verify_csrf)],
)
def edit_page_comment(
    request: Request,
    page_id: int,
    comment_id: int,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Edit a page comment from its detail page. Gated on the session user AND on
    author-ownership (you may only edit your own), then 303 back to the page."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit comments")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    _, err = _own_page_comment_or_response(conn, page_id, comment_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse(
            '<div class="error">Comment cannot be empty.</div>', status_code=400
        )
    # The command owns the edit AND its atomic 'page_comment_edited' event — this web
    # path previously rewrote the body with NO audit trail at all.
    try:
        page_comment_commands.edit_page_comment(
            conn, actor_id=user["id"], page_id=page_id, comment_id=comment_id, body=body
        )
    except page_comment_commands.PageCommentCommandError:
        # vanished between the author check and the write (a race) — 404, not a
        # silent "success" redirect.
        return HTMLResponse(
            '<div class="error">Comment not found.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post(
    "/mentor/pages/{page_id}/comments/{comment_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def delete_page_comment(
    request: Request,
    page_id: int,
    comment_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delete a page comment from its detail page. Same author-ownership rule as
    edit. POST (not DELETE) because HTML forms can't issue DELETE."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "delete comments")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    _, err = _own_page_comment_or_response(
        conn, page_id, comment_id, user, allow_admin=True
    )
    if err is not None:
        return err
    # The command owns the delete AND its atomic 'page_comment_deleted' event; a comment
    # that vanished in a race records nothing and 404s.
    if not page_comment_commands.delete_page_comment(
        conn, actor_id=user["id"], page_id=page_id, comment_id=comment_id
    ):
        return HTMLResponse(
            '<div class="error">Comment not found.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


# --- Page labels ------------------------------------------------------------


@router.post("/mentor/pages/{page_id}/labels", dependencies=[Depends(verify_csrf)])
def add_page_label(
    request: Request,
    page_id: int,
    name: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Attach a label to a page by typing its name — find-or-create, so the user
    doesn't manage a separate vocabulary first (the same shared vocabulary issues
    use). Open write like editing a page. Empty name → 400. 303 back to the page."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "label pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    name = name.strip()
    if not name:
        return HTMLResponse(
            '<div class="error">Label name is required.</div>', status_code=400
        )
    # The command owns the find-or-create, the atomic attach, and the
    # 'page_labeled' event in one transaction — the page twin of the issue-side
    # attach_label_by_name, so the transport performs no vocabulary write. A page
    # that vanished in the race past the visibility gate lands back on the page
    # route (which 404s) rather than erroring here.
    try:
        page_commands.attach_page_label_by_name(
            conn, actor_id=user["id"], page_id=page_id, name=name
        )
    except page_commands.PageCommandError:
        pass
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


@router.post(
    "/mentor/pages/{page_id}/labels/{label_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_page_label(
    request: Request,
    page_id: int,
    label_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Detach a label from a page. Same write gate. POST (not DELETE) because HTML
    forms can't issue DELETE."""
    user = getattr(request.state, "user", None)
    err = _write_required(user, "label pages")
    if err is not None:
        return err
    assert user is not None, "_write_required accepted a missing user"
    # 404 a missing OR hidden page, symmetric with add_page_label (and the REST detach).
    _, err = _page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    # The command owns the atomic detach + 'page_unlabeled' event. A label that
    # isn't attached is a no-op in the UI (double-submit) — land back on the page
    # rather than 404, the same forgiveness the issue-label form gives.
    try:
        page_commands.detach_page_label(
            conn, actor_id=user["id"], page_id=page_id, label_id=label_id
        )
    except page_commands.PageCommandError:
        pass
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)
