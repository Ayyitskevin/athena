"""Browser preview for the shared agent work-context projection.

This route is deliberately only a transport adapter. The Aegis projection owns
visibility, clipping, and every fact in the response; the browser adds no SQL and
does not rebuild any part of the contract for presentation.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from athena.aegis import work_context
from athena.core.deps import get_conn
from athena.web.router import get_templates

router = APIRouter()

_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie"}


@router.get("/aegis/issues/{ref}/work-context", response_class=HTMLResponse)
def issue_work_context(
    request: Request,
    ref: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Preview exactly the context an agent can read for one visible issue.

    A hidden issue and an unknown reference deliberately produce the same body and
    status so this browser surface cannot be used as an existence oracle.
    """
    viewer = getattr(request.state, "user", None)
    projection = work_context.build_work_context(conn, ref, actor=viewer)
    if projection is None:
        return HTMLResponse(
            "<h1>Issue not found</h1>",
            status_code=404,
            headers=_PRIVATE_HEADERS,
        )

    return get_templates().TemplateResponse(
        request=request,
        name="aegis/work_context.html",
        # Starlette injects the request into this mapping. Keep that framework
        # concern from mutating the application-layer projection we were handed.
        context=projection.copy(),
        headers=_PRIVATE_HEADERS,
    )
