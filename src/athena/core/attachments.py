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

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
from typing import BinaryIO, cast

# Columns safe to return to a caller — never `stored_name` (an internal disk
# detail; the download route resolves it from the id).
_PUBLIC_COLS = (
    "id, target_kind, target_id, filename, content_type, byte_size, sha256, "
    "uploaded_by, created_at"
)

# A short, safe extension to preserve on the stored file (letters/digits only).
_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,12})$")
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}(?:\.[a-z0-9]{1,12})?$")
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class AttachmentBlobReference:
    attachment_id: int
    stored_name: str


@dataclass(frozen=True, slots=True)
class PendingAttachment:
    """An uncommitted metadata row and its already-published blob."""

    attachment: dict
    blob_path: Path


@dataclass(frozen=True, slots=True)
class AttachmentChecksumMismatch:
    attachment_id: int
    stored_name: str
    expected_sha256: str
    actual_sha256: str


@dataclass(frozen=True, slots=True)
class AttachmentSizeMismatch:
    attachment_id: int
    stored_name: str
    expected_byte_size: int
    actual_byte_size: int


@dataclass(frozen=True, slots=True)
class AttachmentNonRegularEntry:
    attachment_id: int | None
    stored_name: str
    file_type: str


@dataclass(frozen=True, slots=True)
class AttachmentUnreadableEntry:
    attachment_id: int | None
    stored_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class AttachmentIntegrityReport:
    """A deterministic, non-mutating reconciliation of attachment rows and blobs.

    All row-backed findings are ordered by attachment id and direct on-disk findings
    by stored name. ``non_regular`` and ``unreadable`` make skipped hashes explicit:
    reconciliation never follows a symlink or reads a FIFO/device/socket.
    """

    missing: tuple[AttachmentBlobReference, ...]
    tampered: tuple[AttachmentChecksumMismatch, ...]
    size_mismatched: tuple[AttachmentSizeMismatch, ...]
    non_regular: tuple[AttachmentNonRegularEntry, ...]
    unreadable: tuple[AttachmentUnreadableEntry, ...]
    orphan_files: tuple[str, ...]
    storage_root_problem: str | None = None

    @property
    def ok(self) -> bool:
        return not (
            self.missing
            or self.tampered
            or self.size_mismatched
            or self.non_regular
            or self.unreadable
            or self.orphan_files
            or self.storage_root_problem
        )


@dataclass(frozen=True, slots=True)
class BlobUnlinkFailure:
    stored_name: str
    error: OSError


class BlobCleanupError(OSError):
    """One or more post-commit attachment blobs could not be removed."""

    def __init__(self, failures: tuple[BlobUnlinkFailure, ...]) -> None:
        self.failures = failures
        names = ", ".join(repr(failure.stored_name) for failure in failures)
        super().__init__(f"failed to unlink attachment blob(s): {names}")


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
    if _STORED_NAME_RE.fullmatch(stored_name) is None:
        raise ValueError("invalid attachment stored name")
    return Path(attach_dir) / stored_name


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_blob_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _prepare_storage_directory(directory: Path) -> None:
    """Create the configured root if needed and reject a final-component symlink."""
    directory.mkdir(parents=True, exist_ok=True)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"attachment path is not a directory: {directory}")


def _publish_blob(directory: Path, stored_name: str, data: bytes) -> Path:
    """Durably publish bytes through a private, unique sibling staging file."""
    destination = disk_path(directory, stored_name)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{stored_name}.",
        suffix=".tmp",
        dir=directory,
    )
    temporary = Path(temporary_name)
    published = False
    try:
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        published = True
        _fsync_directory(directory)
        return destination
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if published:
            _unlink_blob_path(destination)
        else:
            temporary.unlink(missing_ok=True)
        raise


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


def list_for(conn: sqlite3.Connection, target_kind: str, target_id: int) -> list[dict]:
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
    """Persist one upload without exposing a partial or row-less blob.

    The metadata insert starts first, then a private staged file is synced and
    atomically published before commit. Any insert, write, publication, or commit
    failure explicitly rolls the database back; a published blob is synchronously
    removed before the error escapes. The caller validates size/emptiness at the
    boundary. Raises sqlite3.IntegrityError if uploaded_by is not a real user.
    """
    published: Path | None = None
    try:
        pending = insert_and_publish(
            conn,
            target_kind=target_kind,
            target_id=target_id,
            filename=filename,
            content_type=content_type,
            data=data,
            uploaded_by=uploaded_by,
            attach_dir=attach_dir,
        )
        published = pending.blob_path
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        finally:
            if published is not None:
                _unlink_blob_path(published)
        raise
    return pending.attachment


def insert_and_publish(
    conn: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: int,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    uploaded_by: int,
    attach_dir: str | Path,
) -> PendingAttachment:
    """Insert metadata and publish its blob without committing.

    The caller owns the surrounding database transaction and must unlink
    ``blob_path`` if anything after this function fails. The private publication
    helper still removes a partial stage or publication when publication itself
    fails.
    """
    display = _display_name(filename)
    stored = _stored_name(display)
    directory = Path(attach_dir)
    _prepare_storage_directory(directory)
    published: Path | None = None
    try:
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
        published = _publish_blob(directory, stored, data)
        attachment_id = cur.lastrowid
        assert attachment_id is not None
        attachment = get(conn, attachment_id)
        assert attachment is not None
        return PendingAttachment(attachment=attachment, blob_path=published)
    except BaseException:
        if published is not None:
            _unlink_blob_path(published)
        raise


def delete(
    conn: sqlite3.Connection, attachment_id: int, attach_dir: str | Path
) -> bool:
    """Remove an attachment row and its blob. Returns False if no such row.

    The database row commits before the non-transactional unlink. A missing blob is
    already the desired end state. Any other unlink/fsync failure propagates so the
    caller cannot claim full cleanup; reconciliation will report the leftover orphan.
    """
    deleted = delete_row(conn, attachment_id)
    if deleted is None:
        return False
    _, stored = deleted
    try:
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _unlink_blob_path(disk_path(attach_dir, stored))
    return True


def delete_row(conn: sqlite3.Connection, attachment_id: int) -> tuple[dict, str] | None:
    """Delete one metadata row without committing or touching the filesystem."""
    attachment = get(conn, attachment_id)
    if attachment is None:
        return None
    stored = get_stored_name(conn, attachment_id)
    assert stored is not None
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    return attachment, stored


def open_blob(
    attach_dir: str | Path, stored_name: str, *, expected_size: int
) -> BinaryIO:
    """Open a regular blob by descriptor without following either final symlink.

    The descriptor is acquired before the response is created, eliminating the
    pathname check/open race. The caller owns and must close the returned handle.
    """
    # Validate the database-held component before passing it to openat(); a corrupt
    # row must not turn the directory descriptor into a traversal primitive.
    disk_path(attach_dir, stored_name)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_only = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_only is None:
        raise OSError("this platform cannot open attachment blobs without symlinks")
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_descriptor = os.open(
        Path(attach_dir), os.O_RDONLY | directory_only | no_follow | close_on_exec
    )
    descriptor = -1
    try:
        descriptor = os.open(
            stored_name,
            os.O_RDONLY | no_follow | close_on_exec,
            dir_fd=directory_descriptor,
        )
    finally:
        os.close(directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("attachment blob is not a regular file")
        if metadata.st_size != expected_size:
            raise OSError("attachment blob size does not match metadata")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        return cast(BinaryIO, handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
    """Unlink post-commit blobs, attempting every name and reporting all failures.

    Missing files are already the desired end state. Other failures are aggregated
    only after every requested name has been attempted, so cleanup is both observable
    and as complete as possible.
    """
    directory = Path(attach_dir)
    failures: list[BlobUnlinkFailure] = []
    for stored in stored_names:
        try:
            _unlink_blob_path(disk_path(directory, stored))
        except OSError as exc:
            failures.append(BlobUnlinkFailure(stored_name=stored, error=exc))
    if failures:
        raise BlobCleanupError(tuple(failures))


def _file_type(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block_device"
    if stat.S_ISCHR(mode):
        return "character_device"
    return "non_regular"


def _os_error_reason(exc: OSError) -> str:
    return f"os_error:{exc.errno}" if exc.errno is not None else "os_error"


def _open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _hash_regular_entry(
    directory_descriptor: int,
    stored_name: str,
) -> tuple[int, str] | AttachmentNonRegularEntry:
    descriptor = os.open(stored_name, _open_flags(), dir_fd=directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return AttachmentNonRegularEntry(
                attachment_id=None,
                stored_name=stored_name,
                file_type=_file_type(metadata.st_mode),
            )
        digest = hashlib.sha256()
        byte_size = 0
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                byte_size += len(chunk)
                digest.update(chunk)
        return byte_size, digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def reconcile_storage(
    conn: sqlite3.Connection, attach_dir: str | Path
) -> AttachmentIntegrityReport:
    """Compare metadata rows with direct blob-directory entries without mutating either.

    Only direct, regular files are opened. Directory entries are inspected without
    following symlinks, and the final open uses ``O_NOFOLLOW`` where the platform
    provides it plus an ``fstat`` regular-file check before chunked hashing.
    """
    rows = conn.execute(
        "SELECT id, stored_name, byte_size, sha256 FROM attachments ORDER BY id"
    ).fetchall()

    missing: list[AttachmentBlobReference] = []
    tampered: list[AttachmentChecksumMismatch] = []
    size_mismatched: list[AttachmentSizeMismatch] = []
    non_regular: list[AttachmentNonRegularEntry] = []
    unreadable: list[AttachmentUnreadableEntry] = []
    orphan_files: list[str] = []

    valid_rows: list[sqlite3.Row] = []
    for row in rows:
        stored_name = str(row["stored_name"])
        if _STORED_NAME_RE.fullmatch(stored_name) is None:
            unreadable.append(
                AttachmentUnreadableEntry(
                    attachment_id=int(row["id"]),
                    stored_name=stored_name,
                    reason="invalid_stored_name",
                )
            )
        else:
            valid_rows.append(row)

    directory = Path(attach_dir)
    try:
        root_metadata = directory.lstat()
    except FileNotFoundError:
        missing.extend(
            AttachmentBlobReference(
                attachment_id=int(row["id"]), stored_name=str(row["stored_name"])
            )
            for row in valid_rows
        )
        return AttachmentIntegrityReport(
            missing=tuple(missing),
            tampered=(),
            size_mismatched=(),
            non_regular=(),
            unreadable=tuple(unreadable),
            orphan_files=(),
        )
    except OSError as exc:
        return AttachmentIntegrityReport(
            missing=(),
            tampered=(),
            size_mismatched=(),
            non_regular=(),
            unreadable=tuple(unreadable),
            orphan_files=(),
            storage_root_problem=_os_error_reason(exc),
        )

    if not stat.S_ISDIR(root_metadata.st_mode):
        return AttachmentIntegrityReport(
            missing=(),
            tampered=(),
            size_mismatched=(),
            non_regular=(),
            unreadable=tuple(unreadable),
            orphan_files=(),
            storage_root_problem=_file_type(root_metadata.st_mode),
        )

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(directory, directory_flags)
    except OSError as exc:
        return AttachmentIntegrityReport(
            missing=(),
            tampered=(),
            size_mismatched=(),
            non_regular=(),
            unreadable=tuple(unreadable),
            orphan_files=(),
            storage_root_problem=_os_error_reason(exc),
        )

    try:
        entries: dict[str, os.stat_result | OSError] = {}
        try:
            with os.scandir(directory_descriptor) as iterator:
                for entry in iterator:
                    try:
                        entries[entry.name] = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        entries[entry.name] = exc
        except OSError as exc:
            return AttachmentIntegrityReport(
                missing=(),
                tampered=(),
                size_mismatched=(),
                non_regular=(),
                unreadable=tuple(unreadable),
                orphan_files=(),
                storage_root_problem=_os_error_reason(exc),
            )

        referenced_names = {str(row["stored_name"]) for row in valid_rows}
        for stored_name in sorted(entries):
            if stored_name in referenced_names:
                continue
            metadata = entries[stored_name]
            if isinstance(metadata, OSError):
                unreadable.append(
                    AttachmentUnreadableEntry(
                        attachment_id=None,
                        stored_name=stored_name,
                        reason=_os_error_reason(metadata),
                    )
                )
            elif stat.S_ISREG(metadata.st_mode):
                orphan_files.append(stored_name)
            else:
                non_regular.append(
                    AttachmentNonRegularEntry(
                        attachment_id=None,
                        stored_name=stored_name,
                        file_type=_file_type(metadata.st_mode),
                    )
                )

        for row in valid_rows:
            attachment_id = int(row["id"])
            stored_name = str(row["stored_name"])
            row_metadata = entries.get(stored_name)
            if row_metadata is None:
                missing.append(
                    AttachmentBlobReference(
                        attachment_id=attachment_id, stored_name=stored_name
                    )
                )
                continue
            if isinstance(row_metadata, OSError):
                unreadable.append(
                    AttachmentUnreadableEntry(
                        attachment_id=attachment_id,
                        stored_name=stored_name,
                        reason=_os_error_reason(row_metadata),
                    )
                )
                continue
            if not stat.S_ISREG(row_metadata.st_mode):
                non_regular.append(
                    AttachmentNonRegularEntry(
                        attachment_id=attachment_id,
                        stored_name=stored_name,
                        file_type=_file_type(row_metadata.st_mode),
                    )
                )
                continue
            try:
                measured = _hash_regular_entry(directory_descriptor, stored_name)
            except FileNotFoundError:
                missing.append(
                    AttachmentBlobReference(
                        attachment_id=attachment_id, stored_name=stored_name
                    )
                )
                continue
            except OSError as exc:
                unreadable.append(
                    AttachmentUnreadableEntry(
                        attachment_id=attachment_id,
                        stored_name=stored_name,
                        reason=_os_error_reason(exc),
                    )
                )
                continue
            if isinstance(measured, AttachmentNonRegularEntry):
                non_regular.append(
                    AttachmentNonRegularEntry(
                        attachment_id=attachment_id,
                        stored_name=stored_name,
                        file_type=measured.file_type,
                    )
                )
                continue

            actual_byte_size, actual_sha256 = measured
            expected_byte_size = int(row["byte_size"])
            if actual_byte_size != expected_byte_size:
                size_mismatched.append(
                    AttachmentSizeMismatch(
                        attachment_id=attachment_id,
                        stored_name=stored_name,
                        expected_byte_size=expected_byte_size,
                        actual_byte_size=actual_byte_size,
                    )
                )
            if actual_sha256 != str(row["sha256"]):
                tampered.append(
                    AttachmentChecksumMismatch(
                        attachment_id=attachment_id,
                        stored_name=stored_name,
                        expected_sha256=str(row["sha256"]),
                        actual_sha256=actual_sha256,
                    )
                )
    finally:
        os.close(directory_descriptor)

    return AttachmentIntegrityReport(
        missing=tuple(missing),
        tampered=tuple(tampered),
        size_mismatched=tuple(size_mismatched),
        non_regular=tuple(non_regular),
        unreadable=tuple(unreadable),
        orphan_files=tuple(orphan_files),
    )
