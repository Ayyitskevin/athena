"""Data access + filesystem storage for attachments (shared by issues and pages).

The blob lives on disk under a configured directory; this module owns the metadata
row and the mapping to disk. The safety rules live here, in one place:

  * the client's filename is kept ONLY for display — never used as a path. The file
    is written under a server-generated random `stored_name`, so an upload named
    "../../etc/passwd" can't escape the attachment directory;
  * a sha256 is recorded for integrity/dedup visibility;
  * reads/writes go through `disk_path`, which joins the configured dir with the
    random name — callers never build paths from user input.

This module is core/ and imports no feature module (no aegis/mentor), so it can
serve both without a dependency cycle; the owning modules' HTTP layers call it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import secrets
import sqlite3

# Columns safe to return to a caller — never `stored_name` (an internal disk
# detail; the download route resolves it from the id).
_PUBLIC_COLS = (
    "id, target_kind, target_id, filename, content_type, byte_size, sha256, "
    "uploaded_by, created_at"
)

# A short, safe extension to preserve on the stored file (letters/digits only).
_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,12})$")


def _display_name(filename: str | None) -> str:
    """Sanitize a client filename for DISPLAY: strip any path, control chars, and
    cap the length. Never used to build a path — purely what we show the user."""
    raw = filename or ""
    # Strip directory components from either separator, then keep the basename.
    base = raw.replace("\\", "/").split("/")[-1].strip()
    base = "".join(ch for ch in base if ch.isprintable())
    return base[:255] or "file"


def _stored_name(display: str) -> str:
    """A random, collision-free on-disk name, preserving a safe extension so the
    downloaded file keeps a useful suffix. The random stem is the security: the
    client never influences where the file lands."""
    match = _EXT_RE.search(display)
    ext = f".{match.group(1).lower()}" if match else ""
    return secrets.token_hex(16) + ext


def disk_path(attach_dir: str | Path, stored_name: str) -> Path:
    """The on-disk path for a stored blob. The only path-builder; both ends are
    server-controlled (the configured dir + the random stored name)."""
    return Path(attach_dir) / stored_name


def get(conn: sqlite3.Connection, attachment_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    return dict(row) if row else None


def get_stored_name(conn: sqlite3.Connection, attachment_id: int) -> str | None:
    """The internal disk handle for an attachment, for the download/delete paths."""
    row = conn.execute(
        "SELECT stored_name FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    return row["stored_name"] if row else None


def list_for(
    conn: sqlite3.Connection, target_kind: str, target_id: int
) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_PUBLIC_COLS} FROM attachments "
        "WHERE target_kind = ? AND target_id = ? ORDER BY id",
        (target_kind, target_id),
    ).fetchall()
    return [dict(r) for r in rows]


def store(
    conn: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: int,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    uploaded_by: int,
    attach_dir: str | Path,
) -> dict:
    """Persist one upload: write the blob under a random name, then record the row.
    Returns the public metadata. The caller validates size/emptiness at the
    boundary; this layer just stores. Raises sqlite3.IntegrityError if uploaded_by
    isn't a real user."""
    display = _display_name(filename)
    stored = _stored_name(display)
    directory = Path(attach_dir)
    directory.mkdir(parents=True, exist_ok=True)
    disk_path(directory, stored).write_bytes(data)

    cur = conn.execute(
        "INSERT INTO attachments "
        "(target_kind, target_id, filename, content_type, byte_size, sha256, "
        "stored_name, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target_kind,
            target_id,
            display,
            (content_type or "application/octet-stream"),
            len(data),
            hashlib.sha256(data).hexdigest(),
            stored,
            uploaded_by,
        ),
    )
    conn.commit()
    return get(conn, cur.lastrowid)


def delete(
    conn: sqlite3.Connection, attachment_id: int, attach_dir: str | Path
) -> bool:
    """Remove an attachment row and its blob. Returns False if no such row. The
    file is unlinked best-effort (a missing file is fine — the row is the truth and
    must go)."""
    stored = get_stored_name(conn, attachment_id)
    if stored is None:
        return False
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    conn.commit()
    path = disk_path(attach_dir, stored)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # the row is gone; a leftover blob is harmless and never served
    return True


def purge_target(
    conn: sqlite3.Connection, target_kind: str, target_id: int
) -> list[str]:
    """Delete every attachment ROW for a target and return their stored disk names,
    for the caller to unlink AFTER its transaction commits. Does NOT commit and does
    NOT touch the filesystem: this is meant to run INSIDE the owning delete's
    BEGIN IMMEDIATE so the metadata vanishes atomically with the target, while the
    blobs — a non-transactional side effect — are removed post-commit via unlink_blobs.
    Called when a page is deleted: attachments key their target polymorphically with
    NO foreign key, so nothing at the DB level clears them; without this the metadata
    rows and on-disk blobs would dangle forever."""
    rows = conn.execute(
        "SELECT stored_name FROM attachments WHERE target_kind = ? AND target_id = ?",
        (target_kind, target_id),
    ).fetchall()
    conn.execute(
        "DELETE FROM attachments WHERE target_kind = ? AND target_id = ?",
        (target_kind, target_id),
    )
    return [r["stored_name"] for r in rows]


def unlink_blobs(attach_dir: str | Path, stored_names: list[str]) -> None:
    """Best-effort unlink of blob files by stored name — the filesystem half of
    purge_target, run after its transaction has committed. A missing file is fine
    (the row is already gone, and the row is the truth); an OS error is swallowed —
    a leftover blob is harmless and never served, since nothing addresses it now."""
    directory = Path(attach_dir)
    for stored in stored_names:
        try:
            disk_path(directory, stored).unlink(missing_ok=True)
        except OSError:
            pass
