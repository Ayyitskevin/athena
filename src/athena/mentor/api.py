"""The Mentor REST API: space endpoints.

The first slice of the docs module. Spaces are top-level containers; pages (a
later slice) will live inside them. Mirrors the shape of the Aegis projects
router. Mounted by main.py.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from athena import config
from athena.core import access, activity, attachments, labels, links, users
from athena.core.attachments_api import AttachmentOut
from athena.core.deps import get_conn
from athena.core.identity import docs_write_actor, is_admin, optional_actor
from athena.mentor import page_activity, page_comments, pages, space_activity, spaces

spaces_router = APIRouter(prefix="/spaces", tags=["mentor"])
# A page belongs to a space, so create/list live under /spaces/{id}/pages
# (a sub-resource, like comments under an issue). A single page is addressable on
# its own canonical URL via this top-level router, like GET /issues/{id}.
pages_router = APIRouter(prefix="/pages", tags=["mentor"])


class SpaceCreate(BaseModel):
    key: str
    name: str
    description: str = ""


class SpaceUpdate(BaseModel):
    key: str | None = None
    name: str | None = None
    description: str | None = None


class SpaceOut(BaseModel):
    id: int
    key: str
    name: str
    description: str
    created_by: int
    created_at: str
    # 'public' (anyone may read) or 'private' (creator, admins, members only).
    # Defaults 'public' for every space until explicitly flipped.
    visibility: str = "public"


class VisibilityUpdate(BaseModel):
    # The space privacy flag: 'public' | 'private'. Defined here (not imported from
    # Aegis) so Mentor keeps no dependency on a sibling feature module — same reason
    # LabelOut is redefined above.
    visibility: str


class MemberAdd(BaseModel):
    user_id: int


class MemberOut(BaseModel):
    # One membership row on a private space: who, plus who granted it and when.
    user_id: int
    name: str
    is_agent: bool
    added_by: int | None = None
    added_at: str


class PageCreate(BaseModel):
    title: str
    body: str = ""
    parent_id: int | None = None


class LabelOut(BaseModel):
    # The shared label vocabulary, same shape Aegis returns — a page and an issue
    # carry the same labels. Defined here (not imported from aegis) so Mentor keeps
    # no dependency on a sibling feature module.
    id: int
    name: str
    color: str


class LabelAttach(BaseModel):
    label_id: int


class PageOut(BaseModel):
    id: int
    space_id: int
    parent_id: int | None = None
    title: str
    body: str
    created_by: int
    created_at: str
    # The current revision's editor + time (== creator/created_at until first edit).
    updated_by: int
    updated_at: str
    # The shared labels attached to this page (alphabetical), or [] if none.
    labels: list[LabelOut] = []


class PageUpdate(BaseModel):
    title: str | None = None
    body: str | None = None


class ParentUpdate(BaseModel):
    # null = move to the top level (no parent). Kept a dedicated body, like the
    # issue assignee/project PUTs, so "remove the parent" (null) is unambiguous.
    parent_id: int | None = None


class PageVersionOut(BaseModel):
    id: int
    page_id: int
    version: int
    title: str
    body: str
    edited_by: int
    created_at: str


class LinkOut(BaseModel):
    # One resolved cross-reference: the kind/id it points at, that target's
    # current title, and whether it still exists (title is null when broken).
    kind: str
    id: int
    title: str | None = None
    exists: bool


class PageCommentCreate(BaseModel):
    body: str


class PageCommentOut(BaseModel):
    id: int
    page_id: int
    author_id: int
    author_name: str
    body: str
    created_at: str


def _with_labels(conn: sqlite3.Connection, page: dict) -> dict:
    """Attach a page's labels under a "labels" key, so PageOut carries them. The page
    OWNS its core columns; labels are a join, resolved here in the one place every
    page response funnels through (the Mentor twin of aegis _with_labels)."""
    page["labels"] = labels.labels_for_page(conn, page["id"])
    return page


def _with_labels_many(conn: sqlite3.Connection, page_rows: list[dict]) -> list[dict]:
    """labels for a list of pages, attached in place. Mirrors _with_labels for the
    list endpoint; small page sets, so a per-page lookup is fine."""
    for page in page_rows:
        page["labels"] = labels.labels_for_page(conn, page["id"])
    return page_rows


@spaces_router.get("", response_model=list[SpaceOut])
def list_all_spaces(
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading the space list is open (optional_actor → None for anonymous), but it
    # only lists spaces the caller may see — public ones plus their own private ones;
    # admins see all. A private space never shows to someone outside it.
    return spaces.list_spaces(conn, access.visible_space_filter(conn, actor))


@spaces_router.post("", response_model=SpaceOut, status_code=201)
def create_space(
    payload: SpaceCreate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may create a space (like creating a project).
    # The key is the canonical identity, normalized to uppercase so "eng" and
    # "ENG" are one space and URLs built from it are predictable.
    key = payload.key.strip().upper()
    name = payload.name.strip()
    if not key:
        raise HTTPException(status_code=422, detail="space key is required")
    if not name:
        raise HTTPException(status_code=422, detail="space name is required")
    if spaces.get_space_by_key(conn, key) is not None:
        raise HTTPException(status_code=409, detail="space key already exists")
    space = spaces.create_space(
        conn,
        key=key,
        name=name,
        description=payload.description,
        created_by=actor["id"],
    )
    space_activity.record_space_created(
        conn, actor_id=actor["id"], space_id=space["id"], name=space["name"]
    )
    return space


@spaces_router.get("/{space_id}", response_model=SpaceOut)
def show_space(
    space_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    space = spaces.get_space(conn, space_id)
    # A private space the caller can't see is a 404, indistinguishable from a missing
    # one, so visibility never leaks through existence.
    if space is None or not access.can_see_space(conn, actor, space_id):
        raise HTTPException(status_code=404, detail="no such space")
    return space


@spaces_router.patch("/{space_id}", response_model=SpaceOut)
def edit_space(
    space_id: int,
    payload: SpaceUpdate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Editing a space is open to any authenticated actor, like creating one or
    # editing a page — only DELETE is creator-locked, because removing a whole
    # container is the destructive action. 404 if the space is missing OR private to
    # the caller: you can't edit a space you can't see.
    before = spaces.get_space(conn, space_id)
    if before is None or not access.can_see_space(conn, actor, space_id):
        raise HTTPException(status_code=404, detail="no such space")
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(status_code=422, detail="no fields to update")
    # Normalize + validate exactly as create does: key uppercased and non-empty,
    # name non-empty. Only the fields the client actually sent are touched.
    key = payload.key.strip().upper() if payload.key is not None else None
    name = payload.name.strip() if payload.name is not None else None
    if key is not None and not key:
        raise HTTPException(status_code=422, detail="space key cannot be empty")
    if name is not None and not name:
        raise HTTPException(status_code=422, detail="space name cannot be empty")
    # A key change must not collide with a DIFFERENT space (key is UNIQUE). Checked
    # here for a clean 409 instead of letting the IntegrityError surface as a 500.
    if key is not None:
        clash = spaces.get_space_by_key(conn, key)
        if clash is not None and clash["id"] != space_id:
            raise HTTPException(status_code=409, detail="space key already exists")
    after = spaces.update_space(
        conn, space_id, key=key, name=name, description=payload.description
    )
    space_activity.record_space_edited(
        conn, actor_id=actor["id"], before=before, after=after
    )
    return after


def _space_for_write(conn: sqlite3.Connection, space_id: int, actor: dict) -> dict:
    """Fetch a space the actor may DELETE, or raise: 404 if no such space, 403 if
    the actor isn't its creator. Deleting a space is creator-only — tighter than
    Mentor's otherwise-open write model (any signed-in actor may create spaces and
    edit pages), because removing a whole container is destructive and wipes a
    documentation tree. Mirrors aegis _project_for_write."""
    space = spaces.get_space(conn, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="no such space")
    if space["created_by"] != actor["id"]:
        raise HTTPException(
            status_code=403, detail="only the space creator may delete it"
        )
    return space


@spaces_router.delete("/{space_id}", status_code=204)
def delete_space(
    space_id: int,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    # Creator only (404 if missing, 403 if not permitted).
    space = _space_for_write(conn, space_id, actor)
    # Refuse rather than cascade: a space that still holds pages must be emptied
    # first. 409, mirroring project-delete-on-issues and page-delete-on-children —
    # a delete must not silently wipe a documentation tree.
    if pages.count_pages_in_space(conn, space_id) > 0:
        raise HTTPException(status_code=409, detail="delete or move its pages first")
    spaces.delete_space(conn, space_id)
    space_activity.record_space_deleted(
        conn, actor_id=actor["id"], space_id=space_id, name=space["name"]
    )


# --- Space access control: privacy toggle + membership --------------------
#
# The Mentor twin of the Aegis project access endpoints. Toggling a space private and
# managing its roster is creator-OR-admin (wider than delete, which is creator-only),
# so an admin can administer any space and the creator can't lock themselves out.
# Reading the roster is gated by plain visibility.


def _space_for_privacy(conn: sqlite3.Connection, space_id: int, actor: dict) -> dict:
    """Fetch a space whose privacy/membership the actor may MANAGE, or raise: 404 if no
    such space OR one the actor can't see, 403 if it's visible but the actor is neither
    its creator nor an admin. The access-admin twin of _space_for_write (creator-only
    delete). Visibility is checked first and collapses to a 404, so a private space the
    actor can't see is indistinguishable from a missing one (no existence leak via 403),
    matching the web twin _authorize_space_manage and the read-side list_space_members."""
    space = spaces.get_space(conn, space_id)
    if space is None or not access.can_see_space(conn, actor, space_id):
        raise HTTPException(status_code=404, detail="no such space")
    if space["created_by"] != actor["id"] and not is_admin(actor):
        raise HTTPException(
            status_code=403,
            detail="only the space creator or an admin may manage access",
        )
    return space


@spaces_router.put("/{space_id}/visibility", response_model=SpaceOut)
def set_space_visibility(
    space_id: int,
    payload: VisibilityUpdate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Creator or admin only (404 if missing, 403 otherwise).
    space = _space_for_privacy(conn, space_id, actor)
    visibility = payload.visibility.strip().lower()
    if visibility not in ("public", "private"):
        raise HTTPException(
            status_code=422, detail="visibility must be 'public' or 'private'"
        )
    if visibility == space["visibility"]:
        return space  # no-op set-to-same — no write, no event
    updated = spaces.set_visibility(conn, space_id, visibility)
    # Going private: record the creator as an explicit member so they show in the
    # roster (they keep access via created_by regardless).
    if visibility == "private":
        access.add_space_member(
            conn, space_id, space["created_by"], added_by=actor["id"]
        )
    space_activity.record_space_visibility_changed(
        conn,
        actor_id=actor["id"],
        space_id=space_id,
        name=updated["name"],
        visibility=visibility,
    )
    return updated


@spaces_router.get("/{space_id}/members", response_model=list[MemberOut])
def list_space_members(
    space_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reading the roster is gated by plain visibility: anyone who can see the space sees
    # its members. A private space the caller can't see is a 404.
    space = spaces.get_space(conn, space_id)
    if space is None or not access.can_see_space(conn, actor, space_id):
        raise HTTPException(status_code=404, detail="no such space")
    return access.list_space_members(conn, space_id)


@spaces_router.post(
    "/{space_id}/members", response_model=list[MemberOut], status_code=201
)
def add_space_member(
    space_id: int,
    payload: MemberAdd,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Creator or admin only. 422 if the user id isn't real. Idempotent — a re-add
    # records no event.
    _space_for_privacy(conn, space_id, actor)
    member = users.get_user(conn, payload.user_id)
    if member is None:
        raise HTTPException(status_code=422, detail="no such user")
    if access.add_space_member(conn, space_id, payload.user_id, added_by=actor["id"]):
        space_activity.record_space_member_added(
            conn, actor_id=actor["id"], space_id=space_id, member_name=member["name"]
        )
    return access.list_space_members(conn, space_id)


@spaces_router.delete("/{space_id}/members/{user_id}", response_model=list[MemberOut])
def remove_space_member(
    space_id: int,
    user_id: int,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Creator or admin only. 404 if the user wasn't a member.
    _space_for_privacy(conn, space_id, actor)
    member = users.get_user(conn, user_id)
    if not access.remove_space_member(conn, space_id, user_id):
        raise HTTPException(status_code=404, detail="user is not a member")
    space_activity.record_space_member_removed(
        conn,
        actor_id=actor["id"],
        space_id=space_id,
        member_name=member["name"] if member else str(user_id),
    )
    return access.list_space_members(conn, space_id)


# --- Pages: documents within a space --------------------------------------


@spaces_router.post("/{space_id}/pages", response_model=PageOut, status_code=201)
def create_page(
    space_id: int,
    payload: PageCreate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Any authenticated actor may create a page (like creating an issue) — but only in
    # a space they can see. The space is a path resource: 404 if it doesn't exist OR is
    # private to the caller (you can't add a page to a space you can't see).
    if spaces.get_space(conn, space_id) is None or not access.can_see_space(
        conn, actor, space_id
    ):
        raise HTTPException(status_code=404, detail="no such space")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="page title is required")
    # A parent (if given) must be a real page IN THIS SAME SPACE — a tree can't
    # span spaces. Checked here because the FK can express "is a page" but not
    # "is a page in this space".
    if payload.parent_id is not None:
        parent = pages.get_page(conn, payload.parent_id)
        if parent is None or parent["space_id"] != space_id:
            raise HTTPException(
                status_code=422, detail="parent must be a page in this space"
            )
    page = pages.create_page(
        conn,
        space_id=space_id,
        title=title,
        body=payload.body,
        parent_id=payload.parent_id,
        created_by=actor["id"],
    )
    page_activity.record_page_created(
        conn,
        actor_id=actor["id"],
        page_id=page["id"],
        title=page["title"],
        body=page["body"],
    )
    return _with_labels(conn, page)


@spaces_router.get("/{space_id}/pages", response_model=list[PageOut])
def list_pages(
    space_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reads are open. 404 if the space is missing OR private to the caller (distinct
    # from a real visible space that simply has no pages yet, which returns []). The
    # space gate covers the pages: every page here is in this space.
    if spaces.get_space(conn, space_id) is None or not access.can_see_space(conn, actor, space_id):
        raise HTTPException(status_code=404, detail="no such space")
    return _with_labels_many(conn, pages.list_pages_in_space(conn, space_id))


@pages_router.get("/{page_id}", response_model=PageOut)
def show_page(
    page_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    page = _page_for_read(conn, page_id, actor)
    return _with_labels(conn, page)


def _page_for_read(conn: sqlite3.Connection, page_id: int, actor: dict | None) -> dict:
    """Fetch a page the actor may READ, or raise 404. A page in a private space the
    actor can't see is the SAME 404 as a missing one, so a page sub-resource
    (comments/versions/attachments/backlinks) never leaks for a hidden page. The Mentor
    twin of aegis _issue_for_read — every per-page GET funnels through here, the way the
    issue sub-resources funnel through theirs, so visibility can't be forgotten on one."""
    page = pages.get_page(conn, page_id)
    if page is None or not access.can_see_space(conn, actor, page["space_id"]):
        raise HTTPException(status_code=404, detail="no such page")
    return page


@pages_router.patch("/{page_id}", response_model=PageOut)
def edit_page(
    page_id: int,
    payload: PageUpdate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Editing is open to any authenticated actor, mirroring create (a page has no
    # creator-only lock — Mentor is a shared wiki, and every edit is recorded in
    # history anyway) — but only on a page they can see. 404 if missing or hidden.
    before = _page_for_read(conn, page_id, actor)
    # Only the fields the client actually sent are touched.
    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(status_code=422, detail="no fields to update")
    title = payload.title.strip() if payload.title is not None else None
    if title is not None and not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    after = pages.update_page(
        conn, page_id, editor_id=actor["id"], title=title, body=payload.body
    )
    page_activity.record_page_edited(
        conn, actor_id=actor["id"], before=before, after=after
    )
    return _with_labels(conn, after)


@pages_router.put("/{page_id}/move", response_model=PageOut)
def move_page(
    page_id: int,
    payload: ParentUpdate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Re-parent a page within its space. Open to any authenticated actor, like
    # edit (no creator lock) — but only on a page they can see. 404 if the page is
    # missing or hidden; 422 if the new parent is invalid (another space, the page
    # itself, or its own descendant — a cycle).
    page = _page_for_read(conn, page_id, actor)
    err = pages.validate_move(conn, page, payload.parent_id)
    if err is not None:
        raise HTTPException(status_code=422, detail=err)
    moved = pages.set_parent(conn, page_id, payload.parent_id)
    page_activity.record_page_moved(
        conn,
        actor_id=actor["id"],
        page_id=page_id,
        before_parent_id=page["parent_id"],
        after_parent_id=payload.parent_id,
    )
    return _with_labels(conn, moved)


@pages_router.delete("/{page_id}", status_code=204)
def delete_page(
    page_id: int,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    # Delete a page. Authed like edit/move — only on a page the actor can see. 404 if
    # missing or hidden; 409 if it still has child pages — we refuse rather than
    # cascade, so a delete can't silently wipe a subtree. 204 (no body) on success.
    page = _page_for_read(conn, page_id, actor)
    if pages.count_child_pages(conn, page_id) > 0:
        raise HTTPException(
            status_code=409, detail="move or delete its child pages first"
        )
    pages.delete_page(conn, page_id)
    page_activity.record_page_deleted(
        conn, actor_id=actor["id"], page_id=page_id, title=page["title"]
    )


@pages_router.get("/{page_id}/backlinks", response_model=list[LinkOut])
def backlinks(
    page_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # "What references this page?" — open like other reads, but the sources are gated
    # by the viewer: a private project's issue / a private space's page linking here
    # never reveals itself. 404 if the page is missing OR in a space the caller can't
    # see (so a hidden page's backlink list never reveals the page exists).
    _page_for_read(conn, page_id, actor)
    return links.backlinks(conn, target_kind="page", target_id=page_id, actor=actor)


# --- Page comments: the discussion thread on a page -----------------------


@pages_router.post(
    "/{page_id}/comments", response_model=PageCommentOut, status_code=201
)
def add_page_comment(
    page_id: int,
    payload: PageCommentCreate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Commenting is an open write like editing a page (Mentor's shared-wiki model) —
    # but only on a page the actor can see. The author is the authenticated actor,
    # never a caller-supplied field. 404 if the page is missing or hidden.
    _page_for_read(conn, page_id, actor)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    comment = page_comments.add_comment(
        conn, page_id=page_id, author_id=actor["id"], body=body
    )
    page_activity.record_page_commented(
        conn, actor_id=actor["id"], page_id=page_id, body=body
    )
    return comment


@pages_router.get("/{page_id}/comments", response_model=list[PageCommentOut])
def list_page_comments(
    page_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like listing versions/attachments — but gated by space visibility: a
    # private page's discussion never leaks. 404 if the page is missing or hidden.
    _page_for_read(conn, page_id, actor)
    return page_comments.list_comments(conn, page_id)


def _author_page_comment_or_error(
    conn: sqlite3.Connection, page_id: int, comment_id: int, actor: dict
) -> dict:
    """Fetch a comment that belongs to this page, requiring the actor to be its
    author. 404 if the comment is missing or hangs off another page, 403 if someone
    other than the author tries to change it — the same author-ownership rule the
    Aegis issue comments enforce."""
    existing = page_comments.get_comment(conn, comment_id)
    if existing is None or existing["page_id"] != page_id:
        raise HTTPException(status_code=404, detail="no such comment")
    if existing["author_id"] != actor["id"]:
        raise HTTPException(status_code=403, detail="not the comment author")
    return existing


@pages_router.patch(
    "/{page_id}/comments/{comment_id}", response_model=PageCommentOut
)
def edit_page_comment(
    page_id: int,
    comment_id: int,
    payload: PageCommentCreate,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _page_for_read(conn, page_id, actor)  # 404 if the page is missing or hidden
    _author_page_comment_or_error(conn, page_id, comment_id, actor)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    updated = page_comments.update_comment(conn, comment_id, body=body)
    if updated is None:  # vanished between the author check and the write (a race)
        raise HTTPException(status_code=404, detail="no such comment")
    return updated


@pages_router.delete("/{page_id}/comments/{comment_id}", status_code=204)
def delete_page_comment(
    page_id: int,
    comment_id: int,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> None:
    _page_for_read(conn, page_id, actor)  # 404 if the page is missing or hidden
    _author_page_comment_or_error(conn, page_id, comment_id, actor)
    if not page_comments.delete_comment(conn, comment_id):
        # vanished between the author check and the delete (a race) — don't record
        # an event for a deletion that didn't happen.
        raise HTTPException(status_code=404, detail="no such comment")
    page_activity.record_page_comment_deleted(
        conn, actor_id=actor["id"], page_id=page_id
    )


@pages_router.post(
    "/{page_id}/attachments", response_model=AttachmentOut, status_code=201
)
def upload_page_attachment(
    page_id: int,
    file: UploadFile = File(...),
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Attaching to a page is an open write like editing it (Mentor's shared-wiki
    # model) — but only on a page the actor can see. 404 if missing or hidden.
    _page_for_read(conn, page_id, actor)
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    if len(data) > config.ATTACH_MAX_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    att = attachments.store(
        conn,
        target_kind="page",
        target_id=page_id,
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        uploaded_by=actor["id"],
        attach_dir=config.ATTACH_DIR,
    )
    activity.record(
        conn,
        actor_id=actor["id"],
        verb="added_attachment",
        target_kind="page",
        target_id=page_id,
        detail=att["filename"],
    )
    return att


@pages_router.get("/{page_id}/attachments", response_model=list[AttachmentOut])
def list_page_attachments(
    page_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Open read, like listing versions — gated by space visibility. 404 if the page is
    # missing or in a space the caller can't see.
    _page_for_read(conn, page_id, actor)
    return attachments.list_for(conn, "page", page_id)


@pages_router.get("/{page_id}/versions", response_model=list[PageVersionOut])
def list_versions(
    page_id: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    # Reads are open but gated by space visibility — a private page's body history is
    # exactly its content, so it must not leak. 404 if the page is missing or hidden
    # (distinct from a real visible page never edited, which returns []).
    _page_for_read(conn, page_id, actor)
    return pages.list_page_versions(conn, page_id)


@pages_router.get("/{page_id}/versions/{version}", response_model=PageVersionOut)
def show_version(
    page_id: int,
    version: int,
    actor: dict | None = Depends(optional_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Gate by the page's space first: a historical snapshot is the page's content, so a
    # hidden page's version must 404 like the live page does — never leak the old body.
    _page_for_read(conn, page_id, actor)
    snapshot = pages.get_page_version(conn, page_id, version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="no such version")
    return snapshot


@pages_router.post("/{page_id}/versions/{version}/restore", response_model=PageOut)
def restore_version(
    page_id: int,
    version: int,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Restoring is an EDIT, not a destruction (the current content is preserved as
    # a new version), so it's open to any authenticated actor like edit/move — not
    # creator-locked the way delete is — but only on a page the actor can see. 404 if
    # the page is missing/hidden or that version is missing; the snapshot's author
    # stamps nothing, the restoring actor stamps the new live revision.
    before = _page_for_read(conn, page_id, actor)
    restored = pages.restore_version(conn, page_id, version, editor_id=actor["id"])
    if restored is None:
        raise HTTPException(status_code=404, detail="no such page or version")
    page_activity.record_page_restored(
        conn,
        actor_id=actor["id"],
        page_id=page_id,
        version=version,
        before=before,
        after=restored,
    )
    return _with_labels(conn, restored)


# --- Labels on a page: an open write, like editing it -----------------------


@pages_router.post("/{page_id}/labels", response_model=PageOut, status_code=201)
def attach_label(
    page_id: int,
    payload: LabelAttach,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Labelling a page is an open write like editing it (Mentor's shared-wiki model) —
    # but only on a page the actor can see. 404 if missing or hidden; 422 if the label
    # id isn't real (rather than letting the FK surface a 500). Idempotent — re-attaching
    # records nothing.
    page = _page_for_read(conn, page_id, actor)
    if labels.get_label(conn, payload.label_id) is None:
        raise HTTPException(status_code=422, detail="no such label")
    if labels.add_label_to_page(conn, page_id, payload.label_id):
        page_activity.record_page_label_added(
            conn, actor_id=actor["id"], page_id=page_id, label_id=payload.label_id
        )
    return _with_labels(conn, page)


@pages_router.delete("/{page_id}/labels/{label_id}", response_model=PageOut)
def detach_label(
    page_id: int,
    label_id: int,
    actor: dict = Depends(docs_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    page = _page_for_read(conn, page_id, actor)  # 404 if the page is missing or hidden
    if not labels.remove_label_from_page(conn, page_id, label_id):
        raise HTTPException(status_code=404, detail="label not on this page")
    page_activity.record_page_label_removed(
        conn, actor_id=actor["id"], page_id=page_id, label_id=label_id
    )
    return _with_labels(conn, page)
