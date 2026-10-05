"""Issue comments, attachments, links, and label routes.

Attaches to the issue router in ``aegis.api``. Every path is nested under an
issue id, so registration order against ``GET /{ref}`` does not collide.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, File, HTTPException, UploadFile

from athena import config
from athena.aegis import comment_commands, comments, dependencies, issue_commands
from athena.aegis.api import (
    STATUS_BY_KIND,
    CommentCreate,
    CommentOut,
    IssueLinksOut,
    IssueOut,
    LabelAttach,
    LinkCreate,
    router,
)
from athena.aegis.rest_support import (
    issue_command_http_error,
    issue_for_read,
    with_labels,
)
from athena.core import attachment_commands, attachments
from athena.core.attachments_api import AttachmentOut
from athena.core.deps import get_conn
from athena.core.ids import RowIdPath
from athena.core.identity import is_admin, issue_write_actor, optional_actor


@router.post("/{issue_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    issue_id: RowIdPath,
    payload: CommentCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # author is the authenticated actor, never a caller-supplied field. Commenting is
    # an additive write any issue WRITER may do — but only on an issue they can see, so
    # gate by visibility (404 if missing or hidden), not by can_modify.
    issue_for_read(conn, issue_id, actor)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    # The command owns the insert AND its atomic 'commented' event (with the auto-watch
    # and any mentions), so a comment and its activity footprint land together.
    return comment_commands.create_comment(
        conn, actor=actor, issue_id=issue_id, body=body
    )


@router.get("/{issue_id}/comments", response_model=list[CommentOut])
def list_comments(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    issue_for_read(conn, issue_id, actor)  # 404 if missing or not visible
    return comments.list_comments(conn, issue_id)


def _author_comment_or_error(
    conn: sqlite3.Connection,
    issue_id: RowIdPath,
    comment_id: RowIdPath,
    actor: dict,
    *,
    allow_admin: bool = False,
) -> dict:
    """Fetch a comment that belongs to this issue, requiring the actor to be its
    author. Raises 404 if the comment is missing or hangs off another issue, 403
    if someone other than the author tries to change it. This author-ownership
    check is the one place we enforce per-row ownership today (issues themselves
    are still 'any authenticated actor' — a separate, deferred design).

    allow_admin lifts the author restriction for admins — a moderation override used
    ONLY on delete, so an admin can remove another user's comment (spam, abuse). Edit
    stays strictly author-only even for admins: removing someone's words is moderation,
    but rewriting them would put words in their mouth. The delete is still audited to
    the admin, so the moderation is on the record."""
    existing = comments.get_comment(conn, comment_id)
    if existing is None or existing["issue_id"] != issue_id:
        raise HTTPException(status_code=404, detail="no such comment")
    if existing["author_id"] != actor["id"] and not (allow_admin and is_admin(actor)):
        raise HTTPException(status_code=403, detail="not the comment author")
    return existing


@router.patch("/{issue_id}/comments/{comment_id}", response_model=CommentOut)
def edit_comment(
    issue_id: RowIdPath,
    comment_id: RowIdPath,
    payload: CommentCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    issue_for_read(conn, issue_id, actor)  # 404 if the issue is missing or hidden
    _author_comment_or_error(conn, issue_id, comment_id, actor)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    # The command owns the edit AND its atomic 'comment_edited' event — previously a
    # silent content rewrite.
    try:
        return comment_commands.edit_comment(
            conn,
            actor=actor,
            issue_id=issue_id,
            comment_id=comment_id,
            body=body,
        )
    except comment_commands.CommentCommandError as exc:
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=str(exc)
        ) from exc


@router.delete("/{issue_id}/comments/{comment_id}", status_code=204)
def delete_comment(
    issue_id: RowIdPath,
    comment_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    issue_for_read(conn, issue_id, actor)  # 404 if the issue is missing or hidden
    _author_comment_or_error(conn, issue_id, comment_id, actor, allow_admin=True)
    # The command owns the delete AND its atomic 'comment_deleted' event; a comment that
    # vanished in a race records nothing and 404s.
    if not comment_commands.delete_comment(
        conn, actor=actor, issue_id=issue_id, comment_id=comment_id
    ):
        raise HTTPException(status_code=404, detail="no such comment")


# --- Attachments on an issue ----------------------------------------------


@router.post("/{issue_id}/attachments", response_model=AttachmentOut, status_code=201)
def upload_issue_attachment(
    issue_id: RowIdPath,
    file: UploadFile = File(...),
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Attaching is additive, like commenting: any issue writer may do it (not just
    # the creator/assignee) — but only on an issue they can see. 404 if missing or
    # hidden.
    issue_for_read(conn, issue_id, actor)
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    if len(data) > config.ATTACH_MAX_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    try:
        return attachment_commands.create_attachment(
            conn,
            actor=actor,
            target_kind="issue",
            target_id=issue_id,
            filename=file.filename,
            content_type=file.content_type,
            data=data,
            attach_dir=config.ATTACH_DIR,
        )
    except attachment_commands.AttachmentCommandError as exc:
        raise HTTPException(
            status_code=STATUS_BY_KIND[exc.kind], detail=str(exc)
        ) from exc


@router.get("/{issue_id}/attachments", response_model=list[AttachmentOut])
def list_issue_attachments(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like listing comments. 404 if the issue is missing or not visible.
    issue_for_read(conn, issue_id, actor)
    return attachments.list_for(conn, "issue", issue_id)


# --- Links: typed dependencies between issues -----------------------------


@router.get("/{issue_id}/links", response_model=IssueLinksOut)
def list_links(
    issue_id: RowIdPath,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Open read, like backlinks/comments. 404 if the issue is missing or not visible,
    # so a hidden/typo'd id reads as not-found rather than three empty lists.
    issue_for_read(conn, issue_id, actor)
    return dependencies.list_links(conn, issue_id, actor=actor)


@router.post("/{issue_id}/links", response_model=IssueLinksOut, status_code=201)
def add_link(
    issue_id: RowIdPath,
    payload: LinkCreate,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Declaring a relationship FROM this issue is a write on it — the command owns the
    # creator-or-assignee gate, the target visibility check, and now the audit event,
    # so the same edge created here or over MCP records one attributable "linked" event
    # atomically. (A hidden target collapses to the same 422 as a missing one.)
    try:
        return issue_commands.link_issues(
            conn,
            actor=actor,
            issue_id=issue_id,
            target_ref=payload.target_ref,
            relation=payload.relation,
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc


@router.delete("/{issue_id}/links/{relation}/{target_id}", response_model=IssueLinksOut)
def remove_link(
    issue_id: RowIdPath,
    relation: str,
    target_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Removing a relationship is a write on this issue too; the command records the
    # audit event and 404s (not_found) when there's nothing to remove.
    try:
        return issue_commands.unlink_issues(
            conn,
            actor=actor,
            issue_id=issue_id,
            target_id=target_id,
            relation=relation,
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc


# --- Labels on an issue: a write, so creator-or-assignee gated -------------


@router.post("/{issue_id}/labels", response_model=IssueOut, status_code=201)
def attach_label(
    issue_id: RowIdPath,
    payload: LabelAttach,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # The command owns the attach AND its atomic 'labeled' event under the
    # creator/assignee/delegated/admin gate. Idempotent: re-attach records nothing.
    try:
        issue = issue_commands.attach_label(
            conn, actor=actor, issue_id=issue_id, label_id=payload.label_id
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    return with_labels(conn, issue)


@router.delete("/{issue_id}/labels/{label_id}", response_model=IssueOut)
def detach_label(
    issue_id: RowIdPath,
    label_id: RowIdPath,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    try:
        issue = issue_commands.detach_label(
            conn, actor=actor, issue_id=issue_id, label_id=label_id
        )
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    return with_labels(conn, issue)
