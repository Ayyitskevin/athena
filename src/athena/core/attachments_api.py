"""Cross-cutting attachment endpoints: download and delete.

Upload + list live on the OWNING module's routers (an issue attachment under
/issues, a page attachment under /pages) because they must check the target
exists, and core/ must not import aegis/mentor. Download and delete only touch the
attachments table, so they live here in core/ and serve both kinds.

Download is an open read (like every other read), but always served as an
ATTACHMENT (never inline) with the global nosniff header, so an uploaded HTML/SVG
can't execute in the browser. Delete is uploader-only and requires the write scope
that matches the attachment's kind.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from athena import config
from athena.core import access, activity, attachments, tokens
from athena.core.deps import get_conn
from athena.core.identity import optional_actor, require_token_scope, write_actor

router = APIRouter(prefix="/attachments", tags=["core"])


def _can_see_attachment(
    conn: sqlite3.Connection, actor: dict | None, att: dict
) -> bool:
    """Whether the actor may see this attachment, resolved through its container's
    visibility — an issue attachment is gated by can_see_issue, a page attachment by
    can_see_page. The single place the cross-cutting attachment endpoints (which can't
    import aegis/mentor) reuse the core resolver, so a private issue's/page's file is
    never served to someone who can't see the issue/page."""
    if att["target_kind"] == "issue":
        return access.can_see_issue(conn, actor, att["target_id"])
    return access.can_see_page(conn, actor, att["target_id"])


class AttachmentOut(BaseModel):
    id: int
    target_kind: str
    target_id: int
    filename: str
    content_type: str
    byte_size: int
    sha256: str
    uploaded_by: int
    created_at: str


@router.get("/{attachment_id}")
def download(
    attachment_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
):
    # Open read, but gated by the container's visibility like every other read: a
    # private issue's/page's attachment is served only to someone who can see the
    # issue/page. A hidden (or missing) attachment is the SAME 404 — its bytes,
    # metadata, and existence never leak.
    att = attachments.get(conn, attachment_id)
    if att is None or not _can_see_attachment(conn, actor, att):
        raise HTTPException(status_code=404, detail="no such attachment")
    stored = attachments.get_stored_name(conn, attachment_id)
    path = attachments.disk_path(config.ATTACH_DIR, stored)
    if not path.exists():
        raise HTTPException(status_code=404, detail="attachment file is missing")
    # filename= makes FastAPI emit Content-Disposition: attachment (download, not
    # inline) with RFC 5987 encoding, so the client filename can't inject headers.
    return FileResponse(
        path,
        media_type=att["content_type"],
        filename=att["filename"],
        content_disposition_type="attachment",
    )


@router.delete("/{attachment_id}", status_code=204)
def remove(
    attachment_id: int,
    actor: dict = Depends(write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    att = attachments.get(conn, attachment_id)
    # 404 if missing OR on a container the actor can't see — you can't delete (or probe)
    # a file on a private issue/page you can't see, same 404 as a missing one.
    if att is None or not _can_see_attachment(conn, actor, att):
        raise HTTPException(status_code=404, detail="no such attachment")
    # The write scope is the one for the attachment's module — deleting an issue's
    # file needs issue:write, a page's file needs docs:write.
    scope = (
        tokens.ISSUE_WRITE_SCOPE
        if att["target_kind"] == "issue"
        else tokens.DOCS_WRITE_SCOPE
    )
    require_token_scope(actor, scope)
    # Only the uploader may remove their attachment (mirrors comment ownership).
    if att["uploaded_by"] != actor["id"]:
        raise HTTPException(status_code=403, detail="not the uploader")
    attachments.delete(conn, attachment_id, config.ATTACH_DIR)
    activity.record(
        conn,
        actor_id=actor["id"],
        verb="removed_attachment",
        target_kind=att["target_kind"],
        target_id=att["target_id"],
        detail=att["filename"],
    )
