"""Browser routes for editing an issue: the form, save, draft, and preview.

``POST /aegis/issues/preview`` is a static path. This router is mounted before
the detail router, which owns ``GET /aegis/issues/{ref}``.
"""

from __future__ import annotations

import html
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from athena.aegis import (
    issue_commands,
    issue_drafts,
    issue_etags,
    issues,
)
from athena.core.deps import get_conn
from athena.web.csrf import verify_csrf
from athena.web.issue_html import (
    authorize_issue_write,
    issue_command_response,
)
from athena.web.render import (
    MAX_PREVIEW_CHARS,
    render_issue_body,
)
from athena.web.router import get_templates

router = APIRouter()


@router.get("/aegis/issues/{issue_id}/edit", response_class=HTMLResponse)
def edit_issue_form(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Render the edit form for an issue, prefilled with its current title/body.
    Gated on the session user — editing is a write, so logged-out callers get a
    sign-in prompt rather than a form they can't submit."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    issue, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/issue_edit.html",
        context=_issue_edit_context(
            conn,
            issue,
            user,
            restored=request.query_params.get("restore") == "1",
            notice=request.query_params.get("notice", ""),
        ),
    )


def _issue_edit_context(
    conn: sqlite3.Connection,
    issue: dict,
    user: dict,
    *,
    restored: bool = False,
    notice: str = "",
    conflict: dict | None = None,
) -> dict:
    """The issue edit form's context, built in one place so opening the form and
    the losing side of a concurrent save cannot disagree about the baseline.

    ``issue`` is always the REAL current row, because the ETag is derived from
    it — the form now shows the saved issue in every case, exactly as the page
    editor does, because the author's own text is safe in ``issue_drafts``
    (0074). An unsaved draft is OFFERED, never applied: ``restored`` re-renders
    the fields with the draft's text, and nothing is written until Save."""
    draft = issue_drafts.get_draft(conn, issue_id=issue["id"], owner_id=user["id"])
    if draft is not None and not issue_drafts.differs_from(draft, issue):
        # Identical to the saved issue: not unsaved work, so offering to restore
        # it would just make an author wonder what they had forgotten.
        draft = None
    return {
        "issue": issue,
        "body_html": render_issue_body(conn, issue["body"] or "", actor=user),
        "issue_etag": issue_etags.current_etag(conn, issue),
        "draft": draft,
        "restored": restored and draft is not None,
        "draft_is_stale": draft is not None
        and issue_drafts.is_stale(draft, issue_etags.current_etag(conn, issue)),
        "notice": notice,
        # The author's unsaved text, shown for comparison when this render is a
        # refusal — the page editor's shape, now that issues have a draft store.
        "conflict": conflict,
    }


@router.post("/aegis/issues/{issue_id}/edit", dependencies=[Depends(verify_csrf)])
def edit_issue(
    request: Request,
    issue_id: int,
    title: str = Form(""),
    body: str = Form(""),
    if_match: str = Form(""),
    based_on: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save edits to an issue's title and body from the edit form. Gated on the
    session user (same actor rule as every write), rejects an empty title, then
    303-redirects back to the issue so it reloads with the new content.

    The form carries the issue's ETag as rendered, so a save that would land on
    top of someone else's is refused rather than silently winning — the browser
    half of the optimistic lock REST and MCP already had."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    # An empty if_match means a form rendered before this field existed (a tab
    # left open across the upgrade). Those keep the old last-write-wins behavior
    # rather than being refused over a field their author cannot see or fix.
    try:
        issue_commands.update_issue(
            conn,
            actor=user,
            issue_id=issue_id,
            title=title,
            body=body,
            if_match=[if_match] if if_match.strip() else None,
        )
    except issue_commands.IssueCommandError as exc:
        if exc.kind == "precondition_failed":
            return _issue_conflict_response(
                conn,
                request,
                issue_id,
                user,
                title=title,
                body=body,
                based_on=based_on,
            )
        if exc.kind in ("invalid_precondition", "precondition_too_large"):
            # A tampered or malformed hidden field is not an authorization
            # signal; treat it as no precondition rather than walling an author
            # out of their own issue.
            try:
                issue_commands.update_issue(
                    conn, actor=user, issue_id=issue_id, title=title, body=body
                )
            except issue_commands.IssueCommandError as retry_exc:
                return issue_command_response(retry_exc)
        else:
            return issue_command_response(exc)
    # The text IS the issue now, so the author's draft of it is a stale copy of
    # something that finally has a real home on the trail. Dropping it is what
    # makes "you have unsaved work" mean it the next time it appears.
    issue_drafts.discard_draft(conn, issue_id=issue_id, owner_id=user["id"])
    return RedirectResponse(f"/aegis/issues/{issue_id}", status_code=303)


def _issue_conflict_response(
    conn: sqlite3.Connection,
    request: Request,
    issue_id: int,
    user: dict,
    *,
    title: str,
    body: str,
    based_on: str,
) -> HTMLResponse:
    """The losing editor's answer for an issue — the PAGE editor's answer now.

    This path used to invert the page equivalent because issues had no draft
    store: the loser's text stayed in the fields, admitted to being unstored,
    and navigating away lost it. ``issue_drafts`` (0074) erases that asymmetry.
    The loser's text is written to their own draft first, the fields show the
    winner's version — the issue as it stands — and restoring is one click.
    Nothing is overwritten, nothing is merged, and nothing is lost.

    The draft keeps the baseline the author was editing FROM (the form's
    ``based_on``), never the issue's new tag — stamping today's tag would mark
    stale work fresh and silence the warning at the one moment it exists for.
    The re-rendered form carries the CURRENT tag, so saving again deliberately
    overwrites instead of looping on the same refusal.
    """
    current = issues.get_issue(conn, issue_id)
    if current is None:
        # It was deleted, not edited, between the precondition and this read.
        return HTMLResponse(
            '<div class="error">Issue not found.</div>', status_code=404
        )
    issue_drafts.save_draft(
        conn,
        issue_id=issue_id,
        owner_id=user["id"],
        title=title,
        body=body,
        based_on=based_on,
    )
    # The fields show what won; `conflict` carries what the author typed.
    return get_templates().TemplateResponse(
        request=request,
        name="aegis/issue_edit.html",
        context=_issue_edit_context(
            conn,
            current,
            user,
            conflict={"title": title, "body": body},
        ),
        status_code=409,
    )


@router.post("/aegis/issues/{issue_id}/draft", dependencies=[Depends(verify_csrf)])
def autosave_issue_draft(
    request: Request,
    issue_id: int,
    title: str = Form(""),
    body: str = Form(""),
    based_on: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Record where this author has got to, without touching the issue.

    The mentor autosave's Aegis twin: nothing here writes to ``issues`` — no
    activity event, no watcher notified, no lifecycle fact. A crashed browser
    should cost nothing, and the trail should still say nothing happened until
    a human decides something did. Gated exactly like the edit form itself
    (creator or current assignee), because a draft OF a write belongs only to
    someone who could perform the write.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    issue, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    try:
        saved = issue_drafts.save_draft(
            conn,
            issue_id=issue_id,
            owner_id=user["id"],
            title=title,
            body=body,
            # The etag the EDITOR RENDERED WITH, carried by the form — never
            # re-read here. Stamping the current etag at autosave time would
            # mark a draft fresh the moment someone else saved, which is the
            # exact moment the stale warning exists for. Blank only for a
            # cached pre-upgrade form; falling back to the current etag there
            # restores the old (weaker) behavior instead of refusing the save.
            based_on=based_on.strip() or issue_etags.current_etag(conn, issue),
        )
    except issue_drafts.DraftTooLarge:
        # Fixed literals from the module's own bounds, not the exception's text:
        # the message is identical in substance, and nothing exception-derived
        # reaches the response (CodeQL's stack-trace-exposure rule, honored the
        # strict way rather than suppressed).
        return HTMLResponse(
            '<div class="error">Draft not held — too large. Titles cap at '
            f"{issue_drafts.MAX_TITLE_CHARS} characters and bodies at "
            f"{issue_drafts.MAX_BODY_CHARS:,}.</div>",
            413,
        )
    return HTMLResponse(
        f'<span class="draft-saved">Draft held {html.escape(saved["updated_at"])}</span>'
    )


@router.post(
    "/aegis/issues/{issue_id}/draft/discard", dependencies=[Depends(verify_csrf)]
)
def discard_issue_draft(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    """Throw away this author's draft of this issue. Affects nobody else."""
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to edit issues.</div>',
            status_code=401,
        )
    _, err = authorize_issue_write(conn, issue_id, user)
    if err is not None:
        return err
    issue_drafts.discard_draft(conn, issue_id=issue_id, owner_id=user["id"])
    # int() is redundant to FastAPI's own path coercion, but that coercion is
    # invisible to the URL-redirection taint analysis; making it explicit proves
    # the Location header cannot carry anything but digits.
    return RedirectResponse(
        f"/aegis/issues/{int(issue_id)}/edit?notice=Draft+discarded.",
        status_code=303,
    )


@router.post("/aegis/issues/preview", dependencies=[Depends(verify_csrf)])
def preview_issue_body(
    request: Request,
    body: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Render unsaved issue text exactly as the issue view will render it.

    Calls ``render_issue_body`` — the SAME function the issue page calls — so the
    two cannot drift. That includes the parts an author might wish were
    different: embeds are not resolved on issues, so a directive previews as its
    "not rendered here" box, because showing a live embed here would promise
    something the saved issue will not deliver.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return HTMLResponse(
            '<div class="blocked">Please <a href="/login">sign in</a> to'
            " preview.</div>",
            status_code=401,
        )
    if len(body) > MAX_PREVIEW_CHARS:
        return HTMLResponse(
            '<div class="error">Too long to preview.</div>', status_code=413
        )
    return HTMLResponse(str(render_issue_body(conn, body, actor=user)))
