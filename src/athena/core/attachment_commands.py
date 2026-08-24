"""Atomic command boundary for attachment metadata, blobs, and audit events.

A blob cannot participate in SQLite's transaction, so uploads publish it before the
single metadata+activity commit and remove it if that commit fails. Deletes commit the
metadata removal and audit event together, then perform observable blob cleanup.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from athena.core import access, activity, attachments, db


class AttachmentCommandError(Exception):
    """A transport-neutral attachment rejection."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _require_command_owned_transaction(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError(
            "attachment commands require a fresh connection so blob rollback "
            "cannot outlive an outer database transaction"
        )


def _target_is_visible(
    conn: sqlite3.Connection, actor: dict, target_kind: str, target_id: int
) -> bool:
    if target_kind == "issue":
        return access.can_see_issue(conn, actor, target_id)
    if target_kind == "page":
        return access.can_see_page(conn, actor, target_id)
    raise AttachmentCommandError("invalid", "invalid attachment target")


def create_attachment(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    target_kind: str,
    target_id: int,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    attach_dir: str | Path,
) -> dict:
    """Publish a blob and atomically commit its metadata plus audit event."""
    _require_command_owned_transaction(conn)
    pending: attachments.PendingAttachment | None = None
    try:
        with db.transaction(conn, immediate=True):
            if not _target_is_visible(conn, actor, target_kind, target_id):
                raise AttachmentCommandError("not_found", "no such attachment target")
            pending = attachments.insert_and_publish(
                conn,
                target_kind=target_kind,
                target_id=target_id,
                filename=filename,
                content_type=content_type,
                data=data,
                uploaded_by=actor["id"],
                attach_dir=attach_dir,
            )
            activity.record(
                conn,
                actor_id=actor["id"],
                verb="added_attachment",
                target_kind=target_kind,
                target_id=target_id,
                detail=pending.attachment["filename"],
                commit=False,
            )
    except BaseException as primary:
        if pending is not None:
            try:
                attachments.unlink_blobs(
                    pending.blob_path.parent, [pending.blob_path.name]
                )
            except BaseException as cleanup:  # noqa: BLE001 — the blob rollback must run for KeyboardInterrupt too; re-raised as a group
                raise BaseExceptionGroup(
                    "attachment transaction and blob rollback both failed",
                    [primary, cleanup],
                ) from None
        raise
    assert pending is not None
    return pending.attachment


def remove_attachment(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    attachment_id: int,
    attach_dir: str | Path,
) -> dict:
    """Atomically commit metadata deletion plus audit, then unlink the blob."""
    _require_command_owned_transaction(conn)
    stored_name: str | None = None
    removed: dict | None = None
    with db.transaction(conn, immediate=True):
        current = attachments.get(conn, attachment_id)
        if current is None or not _target_is_visible(
            conn, actor, current["target_kind"], current["target_id"]
        ):
            raise AttachmentCommandError("not_found", "no such attachment")
        if current["uploaded_by"] != actor["id"]:
            raise AttachmentCommandError("forbidden", "not the uploader")
        deleted = attachments.delete_row(conn, attachment_id)
        assert deleted is not None
        removed, stored_name = deleted
        activity.record(
            conn,
            actor_id=actor["id"],
            verb="removed_attachment",
            target_kind=removed["target_kind"],
            target_id=removed["target_id"],
            detail=removed["filename"],
            commit=False,
        )
    assert removed is not None and stored_name is not None
    attachments.unlink_blobs(attach_dir, [stored_name])
    return removed
