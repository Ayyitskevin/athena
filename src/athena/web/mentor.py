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

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from athena import config
from athena.core import access, activity, attachments, identity, labels, links, notifications
from athena.core.deps import get_conn
from athena.mentor import page_activity, page_comments, pages, space_activity, spaces
from athena.web.csrf import verify_csrf
from athena.web.render import render_body, render_comment
from athena.web.router import get_templates

router = APIRouter()


def _signin_required(verb: str) -> HTMLResponse:
    """The 401 body shown when a logged-out browser tries to write."""
    return HTMLResponse(
        f'<div class="blocked">Please <a href="/login">sign in</a> to {verb}.</div>',
        status_code=401,
    )


def _readonly_response() -> HTMLResponse:
    return HTMLResponse(
        '<div class="blocked">Viewer role is read-only.</div>',
        status_code=403,
    )


def _write_required(user: dict | None, verb: str) -> HTMLResponse | None:
    if user is None:
        return _signin_required(verb)
    if not identity.can_write(user):
        return _readonly_response()
    return None


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


# --- Spaces -----------------------------------------------------------------


@router.get("/mentor", response_class=HTMLResponse)
def spaces_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """List every space with a New Space form (the form is gated; reading is open).
    Each space links to its page tree. Mirrors /aegis/projects."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    # Only the spaces this viewer may see (public + their own private ones; admins
    # all). A private space never appears here to someone outside it.
    all_spaces = spaces.list_spaces(conn, access.visible_space_filter(conn, user))
    # One page-count per space — cheap on the small lists Mentor holds, and it
    # comes from the real data layer (no cached counter to drift).
    counts = {
        s["id"]: pages.count_pages_in_space(conn, s["id"]) for s in all_spaces
    }
    can_write = user is not None and identity.can_write(user)
    return templates.TemplateResponse(
        request=request,
        name="mentor/spaces.html",
        context={"spaces": all_spaces, "counts": counts, "can_write": can_write},
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

    key = key.strip().upper()
    name = name.strip()
    if not key:
        return HTMLResponse('<div class="error">Space key is required.</div>', status_code=400)
    if not name:
        return HTMLResponse('<div class="error">Space name is required.</div>', status_code=400)
    if spaces.get_space_by_key(conn, key) is not None:
        return HTMLResponse('<div class="error">A space with that key already exists.</div>', status_code=409)

    space = spaces.create_space(
        conn, key=key, name=name, description=description.strip(), created_by=user["id"]
    )
    space_activity.record_space_created(
        conn, actor_id=user["id"], space_id=space["id"], name=space["name"]
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

    space = spaces.get_space(conn, space_id)
    if space is None:
        return HTMLResponse('<div class="error">Space not found.</div>', status_code=404)
    if space["created_by"] != user["id"]:
        return HTMLResponse(
            '<div class="blocked">Only the space creator may delete it.</div>',
            status_code=403,
        )
    if pages.count_pages_in_space(conn, space_id) > 0:
        return HTMLResponse(
            '<div class="error">Delete or move this space\'s pages first.</div>',
            status_code=409,
        )
    spaces.delete_space(conn, space_id)
    space_activity.record_space_deleted(
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit spaces")
    if err is not None:
        return err

    space = spaces.get_space(conn, space_id)
    if space is None:
        return HTMLResponse('<div class="error">Space not found.</div>', status_code=404)
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

    before = spaces.get_space(conn, space_id)
    if before is None:
        return HTMLResponse('<div class="error">Space not found.</div>', status_code=404)
    key = key.strip().upper()
    name = name.strip()
    if not key:
        return HTMLResponse('<div class="error">Space key is required.</div>', status_code=400)
    if not name:
        return HTMLResponse('<div class="error">Space name is required.</div>', status_code=400)
    clash = spaces.get_space_by_key(conn, key)
    if clash is not None and clash["id"] != space_id:
        return HTMLResponse(
            '<div class="error">A space with that key already exists.</div>', status_code=409
        )

    after = spaces.update_space(
        conn, space_id, key=key, name=name, description=description.strip()
    )
    space_activity.record_space_edited(
        conn, actor_id=user["id"], before=before, after=after
    )
    return RedirectResponse(f"/mentor/spaces/{space_id}", status_code=303)


@router.get("/mentor/spaces/{space_id}", response_class=HTMLResponse)
def space_detail(
    request: Request, space_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """A space's page tree plus a New Page form (gated). 404-ish empty render if the
    space doesn't exist (consistent with the Aegis not-found handling)."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

    user = getattr(request.state, "user", None)
    space = spaces.get_space(conn, space_id)
    # A private space the viewer can't see is treated exactly like a missing one — same
    # 404, and the fallback space list is itself gated so it never leaks private names.
    if space is None or not access.can_see_space(conn, user, space_id):
        return templates.TemplateResponse(
            request=request,
            name="mentor/spaces.html",
            context={
                "spaces": spaces.list_spaces(conn, access.visible_space_filter(conn, user)),
                "counts": {},
                "can_write": False,
                "error": f"Space #{space_id} not found",
            },
            status_code=404,
        )

    page_rows = pages.list_pages_in_space(conn, space_id)
    can_write = user is not None and identity.can_write(user)
    return templates.TemplateResponse(
        request=request,
        name="mentor/space_detail.html",
        context={
            "space": space,
            "tree": _tree_rows(page_rows),
            # Flat list (alpha) for the optional "nest under" parent select.
            "all_pages": page_rows,
            # Drives the danger zone: only the creator sees Delete (creator-only,
            # tighter than Mentor's open write model), and it's disabled while the
            # space still holds pages (the API would refuse that delete with 409).
            "can_write": can_write,
            "can_delete": can_write and user["id"] == space["created_by"],
            "page_count": len(page_rows),
            "activity": activity.list_activity(
                conn, target_kind="space", target_id=space_id
            ),
        },
    )


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

    if spaces.get_space(conn, space_id) is None:
        return HTMLResponse('<div class="error">Space not found.</div>', status_code=404)

    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Page title is required.</div>', status_code=400)

    parent_id = parent_id.strip()
    if parent_id == "":
        parent: int | None = None
    else:
        if not parent_id.isdigit():
            return HTMLResponse('<div class="error">Invalid parent page.</div>', status_code=400)
        parent_page = pages.get_page(conn, int(parent_id))
        if parent_page is None or parent_page["space_id"] != space_id:
            return HTMLResponse(
                '<div class="error">Parent must be a page in this space.</div>',
                status_code=400,
            )
        parent = int(parent_id)

    page = pages.create_page(
        conn, space_id=space_id, title=title, body=body.strip() or "",
        parent_id=parent, created_by=user["id"],
    )
    page_activity.record_page_created(
        conn,
        actor_id=user["id"],
        page_id=page["id"],
        title=page["title"],
        body=page["body"],
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
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)

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
    # issue comment is (cross-links + safe HTML), so [[page:N]]/[[issue:N]] mentions
    # resolve in a page comment exactly as they do everywhere else.
    comment_rows = page_comments.list_comments(conn, page_id)
    for comment in comment_rows:
        comment["body_html"] = render_comment(conn, comment["body"])
    return templates.TemplateResponse(
        request=request,
        name="mentor/page_detail.html",
        context={
            "page": page,
            "body_html": render_body(conn, page["body"]),
            "comments": comment_rows,
            "page_labels": labels.labels_for_page(conn, page_id),
            "all_labels": labels.list_labels(conn),  # the shared vocabulary, for autocomplete
            "attachments": attachments.list_for(conn, "page", page_id),
            "is_watching": user is not None
            and notifications.is_watching(conn, user["id"], "page", page_id),
            "backlinks": links.backlinks(conn, "page", page_id),
            "space": spaces.get_space(conn, page["space_id"]),
            "ancestors": ancestors,
            "tree": tree,
            "versions": pages.list_page_versions(conn, page_id),
            "activity": activity.list_activity(
                conn, target_kind="page", target_id=page_id
            ),
            "move_candidates": siblings,
            "can_write": can_write,
            # Drives the Delete button: a page with children can't be deleted.
            "child_count": pages.count_child_pages(conn, page_id),
        },
    )


@router.get("/mentor/pages/{page_id}/edit", response_class=HTMLResponse)
def edit_page_form(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the edit form prefilled with the page's current title/body. Editing is
    a write, so logged-out callers get a sign-in prompt rather than a dead form."""
    templates = get_templates()
    if templates is None:
        return HTMLResponse("<h1>Configuration error</h1>", status_code=500)
    user = getattr(request.state, "user", None)
    err = _write_required(user, "edit pages")
    if err is not None:
        return err

    page = pages.get_page(conn, page_id)
    # Can't edit (or even see the form for) a page in a space you can't read — 404,
    # same as a missing page, so the form never leaks a hidden page's content.
    if page is None or not access.can_see_space(conn, user, page["space_id"]):
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="mentor/page_edit.html",
        context={"page": page, "space": spaces.get_space(conn, page["space_id"])},
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

    before = pages.get_page(conn, page_id)
    if before is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    title = title.strip()
    if not title:
        return HTMLResponse('<div class="error">Title is required.</div>', status_code=400)

    after = pages.update_page(
        conn, page_id, editor_id=user["id"], title=title, body=body.strip()
    )
    page_activity.record_page_edited(
        conn, actor_id=user["id"], before=before, after=after
    )
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

    page = pages.get_page(conn, page_id)
    if page is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)

    parent_id = parent_id.strip()
    if parent_id == "":
        new_parent: int | None = None
    else:
        if not parent_id.isdigit():
            return HTMLResponse('<div class="error">Invalid parent page.</div>', status_code=400)
        new_parent = int(parent_id)

    err = pages.validate_move(conn, page, new_parent)
    if err is not None:
        return HTMLResponse(
            f'<div class="error">{html.escape(err)}</div>', status_code=400
        )
    pages.set_parent(conn, page_id, new_parent)
    page_activity.record_page_moved(
        conn,
        actor_id=user["id"],
        page_id=page_id,
        before_parent_id=page["parent_id"],
        after_parent_id=new_parent,
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

    page = pages.get_page(conn, page_id)
    if page is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    if pages.count_child_pages(conn, page_id) > 0:
        return HTMLResponse(
            '<div class="error">Move or delete its child pages first.</div>',
            status_code=409,
        )
    space_id = page["space_id"]
    pages.delete_page(conn, page_id)
    page_activity.record_page_deleted(
        conn, actor_id=user["id"], page_id=page_id, title=page["title"]
    )
    return RedirectResponse(f"/mentor/spaces/{space_id}", status_code=303)


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
    if pages.get_page(conn, page_id) is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    data = file.file.read()
    if not data:
        return HTMLResponse('<div class="error">File is empty.</div>', status_code=400)
    if len(data) > config.ATTACH_MAX_BYTES:
        return HTMLResponse('<div class="error">File is too large.</div>', status_code=413)
    att = attachments.store(
        conn,
        target_kind="page",
        target_id=page_id,
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        uploaded_by=user["id"],
        attach_dir=config.ATTACH_DIR,
    )
    activity.record(
        conn,
        actor_id=user["id"],
        verb="added_attachment",
        target_kind="page",
        target_id=page_id,
        detail=att["filename"],
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
    att = attachments.get(conn, attachment_id)
    if att is None or att["target_kind"] != "page" or att["target_id"] != page_id:
        return HTMLResponse('<div class="error">Attachment not found.</div>', status_code=404)
    if att["uploaded_by"] != user["id"]:
        return HTMLResponse(
            '<div class="error">Only the uploader may remove this file.</div>',
            status_code=403,
        )
    attachments.delete(conn, attachment_id, config.ATTACH_DIR)
    activity.record(
        conn,
        actor_id=user["id"],
        verb="removed_attachment",
        target_kind="page",
        target_id=page_id,
        detail=att["filename"],
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

    before = pages.get_page(conn, page_id)
    restored = pages.restore_version(conn, page_id, version, editor_id=user["id"])
    if restored is None:
        return HTMLResponse(
            '<div class="error">No such page or version.</div>', status_code=404
        )
    page_activity.record_page_restored(
        conn,
        actor_id=user["id"],
        page_id=page_id,
        version=version,
        before=before,
        after=restored,
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
    if pages.get_page(conn, page_id) is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    body = body.strip()
    if not body:
        return HTMLResponse('<div class="error">Comment cannot be empty.</div>', status_code=400)
    page_comments.add_comment(conn, page_id=page_id, author_id=user["id"], body=body)
    page_activity.record_page_commented(
        conn, actor_id=user["id"], page_id=page_id, body=body
    )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


def _own_page_comment_or_response(conn, page_id, comment_id, user):
    """Return the comment if it belongs to this page and the session user is its
    author; otherwise an HTMLResponse (404/403) to return as-is. Mirrors the API's
    author-ownership rule on the web write paths."""
    existing = page_comments.get_comment(conn, comment_id)
    if existing is None or existing["page_id"] != page_id:
        return None, HTMLResponse('<div class="error">Comment not found.</div>', status_code=404)
    if existing["author_id"] != user["id"]:
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
    _, err = _own_page_comment_or_response(conn, page_id, comment_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse('<div class="error">Comment cannot be empty.</div>', status_code=400)
    if page_comments.update_comment(conn, comment_id, body=body) is None:
        # vanished between the author check and the write (a race) — 404, not a
        # silent "success" redirect.
        return HTMLResponse('<div class="error">Comment not found.</div>', status_code=404)
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
    _, err = _own_page_comment_or_response(conn, page_id, comment_id, user)
    if err is not None:
        return err
    if not page_comments.delete_comment(conn, comment_id):
        # vanished between the author check and the delete (a race) — 404, and don't
        # record an event for a deletion that didn't happen.
        return HTMLResponse('<div class="error">Comment not found.</div>', status_code=404)
    page_activity.record_page_comment_deleted(
        conn, actor_id=user["id"], page_id=page_id
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
    if pages.get_page(conn, page_id) is None:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    name = name.strip()
    if not name:
        return HTMLResponse('<div class="error">Label name is required.</div>', status_code=400)
    label = labels.get_or_create_label(conn, name=name)
    if labels.add_label_to_page(conn, page_id, label["id"]):  # idempotent
        page_activity.record_page_label_added(
            conn, actor_id=user["id"], page_id=page_id, label_id=label["id"]
        )
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
    if pages.get_page(conn, page_id) is None:
        # 404 a missing page, symmetric with add_page_label (and the REST detach).
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    if labels.remove_label_from_page(conn, page_id, label_id):
        page_activity.record_page_label_removed(
            conn, actor_id=user["id"], page_id=page_id, label_id=label_id
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)
