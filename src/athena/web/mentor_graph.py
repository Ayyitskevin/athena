"""Browser routes for the Mentor knowledge graph: ego graph and link-mention.

Split out of web/mentor.py."""

from __future__ import annotations
import sqlite3
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from athena.core import (
    graph,
    identity,
    mentions,
)
from athena.core.deps import get_conn
from athena.mentor import (
    page_commands,
)
from athena.web.csrf import verify_csrf
from athena.web.router import get_templates

from athena.web.mentor import (
    _page_visible_or_response,
    _write_required,
)

router = APIRouter()


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
        page_commands.edit_page(conn, actor=user, page_id=page_id, body=body)
    except page_commands.PageCommandError:
        return HTMLResponse('<div class="error">Page not found.</div>', status_code=404)
    back = (
        f"/mentor/pages/{target_id}/graph"
        if target_kind == "page"
        else (f"/aegis/issues/{target_id}/graph")
    )
    return RedirectResponse(back, status_code=303)
