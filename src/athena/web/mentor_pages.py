"""Browser routes for Mentor pages: read, edit, comments, labels, versions.

Split out of web/mentor.py."""

from __future__ import annotations
import html
import sqlite3
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from athena import config
from athena.core import (
    access,
    activity,
    attachment_commands,
    attachments,
    identity,
    labels,
    links,
    notifications,
)
from athena.core.deps import get_conn
from athena.mentor import (
    page_commands,
    page_drafts,
    page_etags,
    page_comment_commands,
    page_comments,
    pages,
    spaces,
)
from markupsafe import escape
from athena.web.csrf import verify_csrf
from athena.web.render import MAX_PREVIEW_CHARS, render_comment, render_page_body
from athena.web.browsing import ATTACHMENT_STATUS_BY_KIND
from athena.web.router import get_templates

from athena.web.mentor import (
    page_visible_or_response,
    signin_required,
    tree_rows,
    write_required,
)

router = APIRouter()


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
    tree = tree_rows(page_rows)
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


@router.get("/mentor/pages/{page_id}/edit", response_class=HTMLResponse)
def edit_page_form(
    request: Request, page_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the edit form prefilled with the page's current title/body. Editing is
    a write, so logged-out callers get a sign-in prompt rather than a dead form."""
    templates = get_templates()
    user = getattr(request.state, "user", None)
    err = write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None  # write_required refused a missing user above

    page = pages.get_page(conn, page_id)
    # Can't edit (or even see the form for) a page in a space you can't read — 404,
    # same as a missing page, so the form never leaks a hidden page's content.
    if page is None or not access.can_see_space(conn, user, page["space_id"]):
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="mentor/page_edit.html",
        context=_edit_form_context(
            conn,
            request,
            page,
            user,
            restored=request.query_params.get("restore") == "1",
            notice=(request.query_params.get("notice") or "").strip(),
        ),
    )


def _conflict_response(
    conn: sqlite3.Connection,
    request: Request,
    page_id: int,
    user: dict,
    *,
    title: str,
    body: str,
    based_on: str,
) -> HTMLResponse:
    """The losing editor's answer: refused, nothing lost, both texts in front of you.

    Three things this deliberately does NOT do. It does not overwrite — that is the
    refusal that brought us here. It does not merge, because Athena does not claim
    to have resolved something a person has to read to resolve. And it does not
    throw the author's text away, which is the failure mode of a bare 412 page:
    the browser's back button is not a durable store, and telling someone their
    work is "still in the form" is only true until they navigate.

    So the submitted text is written to the author's own draft — the store that
    already exists for exactly this, owner-scoped and offered-never-applied — and
    the form re-renders showing THEIR version, with yours beside it and one click
    ('Restore draft') to put yours back in the fields. The author reconciles; the
    tool reports.

    409, because it is a conflict with state the caller already had a view of.
    """
    page = pages.get_page(conn, page_id)
    if page is None:
        # It was deleted, not edited, between the precondition and this read.
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    # Keep their work under the baseline they were editing FROM — the tag the form
    # submitted — never the page's new one. Stamping today's tag would mark stale
    # work fresh and silence the stale-draft warning the next time they open the
    # form, which is the one moment that warning exists for.
    page_drafts.save_draft(
        conn,
        page_id=page_id,
        owner_id=user["id"],
        title=title,
        body=body,
        based_on=based_on,
    )
    return get_templates().TemplateResponse(
        request=request,
        name="mentor/page_edit.html",
        context=_edit_form_context(
            conn,
            request,
            page,
            user,
            conflict={"title": title, "body": body},
        ),
        status_code=409,
    )


def _edit_form_context(
    conn: sqlite3.Connection,
    request: Request,
    page: dict,
    user: dict,
    *,
    restored: bool = False,
    notice: str = "",
    conflict: dict | None = None,
) -> dict:
    """The edit form's context, built in one place.

    Two routes render this form: opening it, and the losing side of a concurrent
    save. They must agree about what the author is looking at — especially the
    baseline, since a conflict re-render that stamped a stale tag would refuse the
    author's next save too, forever."""
    draft = page_drafts.get_draft(conn, page_id=page["id"], owner_id=user["id"])
    if draft is not None and not page_drafts.differs_from(draft, page):
        # Identical to the saved page: not unsaved work, so offering to restore
        # it would just make an author wonder what they had forgotten.
        draft = None
    return {
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
        "restored": restored and draft is not None,
        "draft_is_stale": draft is not None
        and page_drafts.is_stale(draft, page_etags.current_etag(conn, page)),
        # The baseline this editing session starts FROM. The form carries it
        # through every autosave, so a draft records the page the author
        # actually saw — not whatever the page had become by the time the
        # autosave timer fired (which would defeat the stale-draft warning).
        "page_etag": page_etags.current_etag(conn, page),
        "notice": notice,
        # The text the author just tried to save, when someone else got there
        # first. Shown beside theirs; never merged into it.
        "conflict": conflict,
    }


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
        return signin_required("preview")
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
    err = write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None
    page, err = page_visible_or_response(conn, page_id, user)
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
    except page_drafts.DraftTooLarge:
        # Fixed literals from the module's own bounds, not the exception's text
        # — mirrored from the issue autosave, where CodeQL's
        # stack-trace-exposure rule flagged the exception-derived variant.
        return HTMLResponse(
            '<div class="error">Draft not held — too large. Titles cap at '
            f"{page_drafts.MAX_TITLE_CHARS} characters and bodies at "
            f"{page_drafts.MAX_BODY_CHARS:,}.</div>",
            413,
        )
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
    err = write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None
    page_drafts.discard_draft(conn, page_id=page_id, owner_id=user["id"])
    # Explicit int() for the URL-redirection taint analysis, mirroring the
    # issue-side discard; FastAPI's own coercion is invisible to it.
    return RedirectResponse(
        f"/mentor/pages/{int(page_id)}/edit?notice=Draft+discarded.",
        status_code=303,
    )


@router.post("/mentor/pages/{page_id}/edit", dependencies=[Depends(verify_csrf)])
def edit_page(
    request: Request,
    page_id: int,
    title: str = Form(""),
    body: str = Form(""),
    if_match: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save edits to a page's title/body. Gated on the session user; empty title is
    rejected. update_page snapshots the prior revision into history before
    overwriting (see mentor/pages.py), then we 303 back to the page.

    The form carries the page's ETag as it was RENDERED, so a save that would land
    on top of someone else's is refused instead of silently winning. What happens
    next is the whole point of this route (see ``_conflict_response``): nothing is
    overwritten, nothing is merged, and nothing the author typed is thrown away."""
    user = getattr(request.state, "user", None)
    err = write_required(user, "edit pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"

    _, err = page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    title = title.strip()
    if not title:
        return HTMLResponse(
            '<div class="error">Title is required.</div>', status_code=400
        )

    # The command owns the atomic snapshot+overwrite, its 'page_edited' event, and
    # the precondition — compared inside the same write lock, so two browsers
    # holding the same tag cannot both pass it. A page that vanished between the
    # visibility check and the write (a race) 404s rather than 500s.
    #
    # An empty if_match means a form rendered before this field existed (a tab left
    # open across the upgrade). Those keep the old last-write-wins behavior rather
    # than being refused on a technicality the author cannot see or fix.
    try:
        page_commands.edit_page(
            conn,
            actor=user,
            page_id=page_id,
            title=title,
            body=body.strip(),
            if_match=[if_match] if if_match.strip() else None,
        )
    except page_commands.PageCommandError as exc:
        if exc.kind == "precondition_failed":
            return _conflict_response(
                conn,
                request,
                page_id,
                user,
                title=title,
                body=body.strip(),
                based_on=if_match,
            )
        if exc.kind in ("invalid_precondition", "precondition_too_large"):
            # A tampered or malformed hidden field. Treat it as no precondition at
            # all rather than blocking the author out of their own page: the field
            # is a concurrency aid, not an authorization check.
            page_commands.edit_page(
                conn,
                actor=user,
                page_id=page_id,
                title=title,
                body=body.strip(),
            )
        else:
            return HTMLResponse(
                '<div class="error">Page not found.</div>', status_code=404
            )
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
    err = write_required(user, "move pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"

    page, err = page_visible_or_response(conn, page_id, user)
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
            conn, actor=user, page_id=page_id, new_parent_id=new_parent
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
    err = write_required(user, "delete pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"

    # The command owns the atomic delete, its 'page_deleted' event, the visibility
    # check, and the no-cascade children rule — then the post-commit blob unlink +
    # index maintenance. It returns the page as it was, for the redirect home.
    try:
        page = page_commands.delete_page(conn, actor=user, page_id=page_id)
    except page_commands.PageCommandError as exc:
        if exc.kind == "conflict":
            return HTMLResponse(
                '<div class="error">Move or delete its child pages first.</div>',
                status_code=409,
            )
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    return RedirectResponse(f"/mentor/spaces/{page['space_id']}", status_code=303)


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
    err = write_required(user, "archive pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    try:
        page_commands.set_page_archived(
            conn, actor=user, page_id=page_id, archived=True
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
    err = write_required(user, "restore pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    try:
        page_commands.set_page_archived(
            conn, actor=user, page_id=page_id, archived=False
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
    err = write_required(user, "attach files")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
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
            status_code=ATTACHMENT_STATUS_BY_KIND[exc.kind],
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
    err = write_required(user, "remove files")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
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
            status_code=ATTACHMENT_STATUS_BY_KIND[exc.kind],
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
    _, err = page_visible_or_response(conn, page_id, user)
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
    _, err = page_visible_or_response(conn, page_id, user)
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
    err = write_required(user, "restore pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"

    _, err = page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    # The command owns the atomic restore AND its 'page_restored' event; a missing
    # page/version comes back as PageCommandError('not_found').
    try:
        page_commands.restore_page_version(
            conn, actor=user, page_id=page_id, version=version
        )
    except page_commands.PageCommandError:
        return HTMLResponse(
            '<div class="error">No such page or version.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


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
    err = write_required(user, "comment")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse(
            '<div class="error">Comment cannot be empty.</div>', status_code=400
        )
    # The command owns the insert AND its atomic 'page_commented' event (auto-watch +
    # mentions), plus the visibility gate re-checked inside its transaction.
    try:
        page_comment_commands.create_page_comment(
            conn, actor=user, page_id=page_id, body=body
        )
    except page_comment_commands.PageCommentCommandError:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
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
    err = write_required(user, "edit comments")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
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
            conn, actor=user, page_id=page_id, comment_id=comment_id, body=body
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
    err = write_required(user, "delete comments")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    _, err = _own_page_comment_or_response(
        conn, page_id, comment_id, user, allow_admin=True
    )
    if err is not None:
        return err
    # The command owns the delete AND its atomic 'page_comment_deleted' event; a comment
    # that vanished in a race records nothing and 404s.
    try:
        removed = page_comment_commands.delete_page_comment(
            conn, actor=user, page_id=page_id, comment_id=comment_id
        )
    except page_comment_commands.PageCommentCommandError:
        removed = False
    if not removed:
        return HTMLResponse(
            '<div class="error">Comment not found.</div>', status_code=404
        )
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)


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
    err = write_required(user, "label pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    _, err = page_visible_or_response(conn, page_id, user)
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
            conn, actor=user, page_id=page_id, name=name
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
    err = write_required(user, "label pages")
    if err is not None:
        return err
    assert user is not None, "write_required accepted a missing user"
    # 404 a missing OR hidden page, symmetric with add_page_label (and the REST detach).
    _, err = page_visible_or_response(conn, page_id, user)
    if err is not None:
        return err
    # The command owns the atomic detach + 'page_unlabeled' event. A label that
    # isn't attached is a no-op in the UI (double-submit) — land back on the page
    # rather than 404, the same forgiveness the issue-label form gives.
    try:
        page_commands.detach_page_label(
            conn, actor=user, page_id=page_id, label_id=label_id
        )
    except page_commands.PageCommandError:
        pass
    return RedirectResponse(f"/mentor/pages/{page_id}", status_code=303)
