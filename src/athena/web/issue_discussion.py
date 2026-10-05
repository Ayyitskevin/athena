"""Browser routes for issue comments and attachments.

Split out of web/issues.py. Comment ownership stays in this module; the
visibility gate and command refusal are the public helpers in web.issue_html.
"""

from __future__ import annotations

import html
import sqlite3

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from athena import config
from athena.aegis import (
    comment_commands,
    comments,
)
from athena.core import (
    attachment_commands,
    attachments,
    identity,
)
from athena.core.deps import get_conn
from athena.web.browsing import (
    ATTACHMENT_STATUS_BY_KIND,
    readonly_response,
)
from athena.web.csrf import verify_csrf
from athena.web.issue_html import (
    issue_visible_or_404,
)

router = APIRouter()


@router.post("/aegis/issues/{issue_id}/comments", dependencies=[Depends(verify_csrf)])
def add_issue_comment(
    request: Request,
    issue_id: int,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Post a comment from the detail page. Gated on the session user (the
    author is the session, never a form field), then 303-redirects back to the
    issue so the new comment shows."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to comment.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return readonly_response()
    _, err = issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse(
            '<div class="error">Comment cannot be empty.</div>', status_code=400
        )

    # The command owns the insert AND its atomic 'commented' event (auto-watch + mentions).
    comment_commands.create_comment(conn, actor=user, issue_id=issue_id, body=body)
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


def _own_comment_or_response(conn, issue_id, comment_id, user, *, allow_admin=False):
    """Return the comment if it belongs to this issue and the session user is its
    author; otherwise an HTMLResponse (404/403) to return as-is. Mirrors the
    API's author-ownership rule on the web write paths. allow_admin lets an admin
    through for moderation — used only on delete, matching the API override; edit
    stays author-only."""
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
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
    "/aegis/issues/{issue_id}/comments/{comment_id}/edit",
    dependencies=[Depends(verify_csrf)],
)
def edit_issue_comment(
    request: Request,
    issue_id: int,
    comment_id: int,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Edit a comment from the detail page. Gated on the session user AND on
    author-ownership (you may only edit your own), then 303 back to the issue."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit comments.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return readonly_response()
    _, err = issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    _, err = _own_comment_or_response(conn, issue_id, comment_id, user)
    if err is not None:
        return err
    body = body.strip()
    if not body:
        return HTMLResponse(
            '<div class="error">Comment cannot be empty.</div>', status_code=400
        )
    # The command owns the edit AND its atomic 'comment_edited' event — this web path
    # previously rewrote the body with NO audit trail at all.
    try:
        comment_commands.edit_comment(
            conn,
            actor=user,
            issue_id=issue_id,
            comment_id=comment_id,
            body=body,
        )
    except comment_commands.CommentCommandError:
        # vanished between the author check and the write (a race) — 404, not a
        # silent "success" redirect.
        return HTMLResponse(
            '<div class="error">Comment not found.</div>', status_code=404
        )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/comments/{comment_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def delete_issue_comment(
    request: Request,
    issue_id: int,
    comment_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delete a comment from the detail page. Same author-ownership rule as edit.
    Uses POST (not DELETE) because HTML forms can't issue DELETE."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to delete comments.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return readonly_response()
    _, err = issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    _, err = _own_comment_or_response(
        conn, issue_id, comment_id, user, allow_admin=True
    )
    if err is not None:
        return err
    # The command owns the delete AND its atomic 'comment_deleted' event; a comment that
    # vanished in a race records nothing and 404s.
    if not comment_commands.delete_comment(
        conn, actor=user, issue_id=issue_id, comment_id=comment_id
    ):
        return HTMLResponse(
            '<div class="error">Comment not found.</div>', status_code=404
        )
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/attachments", dependencies=[Depends(verify_csrf)]
)
def add_issue_attachment(
    request: Request,
    issue_id: int,
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Attach a file to an issue from the detail page. Same write gate as comments.
    Empty file → 400, oversize → 413; otherwise 303 back to the issue."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to attach files.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return readonly_response()
    _, err = issue_visible_or_404(conn, issue_id, user)
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
            target_kind="issue",
            target_id=issue_id,
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
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


@router.post(
    "/aegis/issues/{issue_id}/attachments/{attachment_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
def remove_issue_attachment(
    request: Request,
    issue_id: int,
    attachment_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delete an attachment from the issue detail page. Uploader-only (mirrors
    comment ownership). POST, not DELETE, because HTML forms can't issue DELETE."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to remove files.</div>',
            status_code=401,
        )
    if not identity.can_write(user):
        return readonly_response()
    _, err = issue_visible_or_404(conn, issue_id, user)
    if err is not None:
        return err
    att = attachments.get(conn, attachment_id)
    if att is None or att["target_kind"] != "issue" or att["target_id"] != issue_id:
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
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)
