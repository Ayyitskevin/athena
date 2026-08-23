"""Descriptor-pinned recovery bundle and scratch-drill helpers."""

from __future__ import annotations

from contextlib import closing, contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
from typing import Iterator, NoReturn

from athena import __version__
from athena.core import attachments, db

_BUNDLE_SCHEMA = "athena.recovery_bundle.v1"
_BUNDLE_ROOT_ENTRIES = frozenset({"manifest.json", "athena.db", "attachments"})
_MANIFEST_MAX_BYTES = 64 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_RENAME_NOREPLACE = 1


@dataclass(frozen=True, slots=True)
class RecoveryBundle:
    """A verified local recovery bundle and its bounded operator metadata."""

    path: Path
    capture_started_at: datetime
    capture_completed_at: datetime
    database_bytes: int
    attachment_count: int
    attachment_bytes: int
    athena_version: str
    latest_migration: str


@dataclass(frozen=True, slots=True)
class RecoveryDrill:
    """A materialized database/attachment pair created only for a local drill."""

    path: Path
    source_bundle: RecoveryBundle


@dataclass(frozen=True, slots=True)
class _AttachmentRow:
    attachment_id: int
    stored_name: str
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _VerifiedBundleState:
    summary: RecoveryBundle
    root_descriptor: int
    root_identity: tuple[int, int]
    database_path: Path
    database_sha256: str
    attachment_rows: tuple[_AttachmentRow, ...]
    attachment_directory_descriptor: int


@dataclass(frozen=True, slots=True)
class _BundleManifest:
    capture_started_at: datetime
    capture_completed_at: datetime
    database_bytes: int
    database_sha256: str
    latest_migration: str
    attachment_count: int
    attachment_bytes: int
    attachment_set_sha256: str
    athena_version: str


@dataclass(frozen=True, slots=True)
class _NewDirectoryDestination:
    path: Path
    name: str
    parent_path: Path
    parent_descriptor: int
    parent_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _PrivateStage:
    path: Path
    name: str
    identity: tuple[int, int]
    descriptor: int


@dataclass(slots=True)
class _StageOwner:
    stage: _PrivateStage | None = None


@dataclass(frozen=True, slots=True)
class _ProtectedDirectory:
    descriptor: int
    identity: tuple[int, int]
    label: str


@dataclass(frozen=True, slots=True)
class _PinnedStagedDatabase:
    path: Path
    descriptor: int
    identity: tuple[int, int]


class RecoveryBundleDurabilityError(OSError):
    """Publication succeeded, but durability of the directory entry is unknown."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"recovery artifact was published at {path}, but parent-directory "
            "durability could not be confirmed; preserve and verify it"
        )


class RecoveryRaceError(OSError):
    """A recovery path or descriptor changed across a guarded operation."""


class RecoveryStagePreservedError(OSError):
    """A failed operation left its private stage for explicit operator handling."""

    def __init__(self, path: Path, *, reason: str) -> None:
        self.path = path
        self.recovery_stage_path = path
        super().__init__(
            f"recovery operation failed ({reason}); private stage retained at {path}; "
            "inspect it and remove it explicitly"
        )


def validate_current_database(conn: sqlite3.Connection) -> db.MigrationStatus:
    """Validate a connection as an exact, current Athena database.

    The caller chooses how the connection is opened. Recovery verification uses
    an immutable private copy, while normal operator checks may use a live
    connection. This function deliberately performs no migration or journal-mode
    write.
    """
    conn.row_factory = sqlite3.Row
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        reason = "no result" if result is None else result[0]
        raise ValueError(f"database integrity check failed: {reason}")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("database foreign key check failed")

    status = db.migration_status(conn)
    expected = sqlite3.connect(":memory:")
    expected.row_factory = sqlite3.Row
    try:
        expected.execute("PRAGMA foreign_keys = ON")
        db.migrate(expected)
        expected_schema = _application_schema(expected)
    finally:
        expected.close()
    if _application_schema(conn) != expected_schema:
        raise ValueError("database schema does not match the packaged Athena schema")
    return status


def create_recovery_bundle(
    db_path: str | Path,
    attach_dir: str | Path,
    bundle_path: str | Path,
) -> RecoveryBundle:
    """Create and durably publish one snapshot-bound local recovery bundle.

    The SQLite snapshot is taken first and is the sole attachment-membership
    authority. Uploads committed afterward and unrelated live orphans are omitted;
    a snapshot-visible blob that cannot be copied exactly makes the attempt fail.
    """
    source_database = Path(db_path)
    source_descriptor, source_identity = _open_recovery_source_database(source_database)
    try:
        (
            attachment_directory,
            attachment_descriptor,
            attachment_identity,
        ) = _open_source_attachment_directory(Path(attach_dir))
        try:
            with _new_directory_destination(
                Path(bundle_path),
                label="recovery bundle",
                forbidden_root=attachment_directory,
            ) as destination:
                _require_open_directory_path_stable(
                    attachment_directory,
                    attachment_descriptor,
                    attachment_identity,
                    label="attachment directory",
                )
                _require_destination_outside_open_directory(
                    destination,
                    attachment_descriptor,
                    attachment_identity,
                    label="attachment directory",
                )
                capture_started_at = _utc_now()
                stage_owner = _StageOwner()
                published = False
                publication_attempted = False
                committed = False
                try:
                    stage = _make_private_stage(destination, owner=stage_owner)
                    staged_database = stage.path / "athena.db"
                    _snapshot_recovery_database(
                        source_database,
                        source_descriptor,
                        source_identity,
                        staged_database,
                        stage.descriptor,
                    )
                    os.chmod(staged_database, 0o600)

                    with _immutable_connection(staged_database) as conn:
                        status = validate_current_database(conn)
                        attachment_rows = _read_attachment_rows(conn)

                    staged_attachments = stage.path / "attachments"
                    staged_attachments.mkdir(mode=0o700)
                    os.chmod(staged_attachments, 0o700)
                    staged_attachment_descriptor = _open_directory_path(
                        staged_attachments,
                        label="staged attachment directory",
                        expected_mode=0o700,
                    )
                    try:
                        for row in attachment_rows:
                            _copy_attachment(
                                attachment_descriptor,
                                staged_attachment_descriptor,
                                row,
                            )
                        os.fsync(staged_attachment_descriptor)
                    finally:
                        os.close(staged_attachment_descriptor)

                    _validate_directory_descriptor(
                        stage.descriptor,
                        label="staged recovery bundle",
                        expected_mode=0o700,
                    )
                    database_bytes, database_sha256, _ = _hash_regular_at(
                        stage.descriptor,
                        "athena.db",
                        label="staged recovery database",
                        expected_mode=0o600,
                    )

                    capture_completed_at = _utc_now()
                    attachment_bytes = sum(row.byte_size for row in attachment_rows)
                    manifest: dict[str, object] = {
                        "schema": _BUNDLE_SCHEMA,
                        "athena_version": __version__,
                        "capture_started_at": _format_utc(capture_started_at),
                        "capture_completed_at": _format_utc(capture_completed_at),
                        "database": {
                            "path": "athena.db",
                            "byte_size": database_bytes,
                            "sha256": database_sha256,
                            "latest_migration": status.latest,
                        },
                        "attachments": {
                            "path": "attachments",
                            "count": len(attachment_rows),
                            "byte_size": attachment_bytes,
                            "set_sha256": _attachment_set_sha256(attachment_rows),
                        },
                    }
                    _write_manifest(stage.path / "manifest.json", manifest)
                    _fsync_directory(staged_attachments)
                    os.fsync(stage.descriptor)

                    with _verified_bundle(
                        stage.path,
                        allow_root_rename=True,
                        final_root_path=destination.path,
                        pinned_stage=stage,
                    ) as verified_state:
                        verified = verified_state.summary
                        _require_open_directory_path_stable(
                            attachment_directory,
                            attachment_descriptor,
                            attachment_identity,
                            label="attachment directory",
                        )
                        _require_destination_outside_open_directory(
                            destination,
                            attachment_descriptor,
                            attachment_identity,
                            label="attachment directory",
                        )
                        publication_attempted = True
                        _publish_private_stage(
                            stage,
                            destination,
                            protected=_ProtectedDirectory(
                                descriptor=attachment_descriptor,
                                identity=attachment_identity,
                                label="attachment directory",
                            ),
                        )
                        published = True
                    _require_open_directory_path_stable(
                        attachment_directory,
                        attachment_descriptor,
                        attachment_identity,
                        label="attachment directory",
                    )
                    _require_destination_outside_open_directory(
                        destination,
                        attachment_descriptor,
                        attachment_identity,
                        label="attachment directory",
                    )
                    result = RecoveryBundle(
                        path=destination.path,
                        capture_started_at=verified.capture_started_at,
                        capture_completed_at=verified.capture_completed_at,
                        database_bytes=verified.database_bytes,
                        attachment_count=verified.attachment_count,
                        attachment_bytes=verified.attachment_bytes,
                        athena_version=verified.athena_version,
                        latest_migration=verified.latest_migration,
                    )
                    committed = True
                    return result
                except BaseException as exc:
                    owned_stage = stage_owner.stage
                    if owned_stage is not None:
                        if committed and published:
                            exc.recovery_published_path = destination.path  # type: ignore[attr-defined]
                        if isinstance(exc, RecoveryBundleDurabilityError):
                            published = True
                        elif publication_attempted and not committed:
                            _retract_after_failed_validation(
                                owned_stage,
                                destination,
                            )
                            published = False
                        if not published:
                            _record_preserved_stage(
                                destination,
                                owned_stage,
                                reason=exc,
                            )
                    raise
                finally:
                    if stage_owner.stage is not None:
                        os.close(stage_owner.stage.descriptor)
        finally:
            os.close(attachment_descriptor)
    finally:
        os.close(source_descriptor)


def verify_recovery_bundle(bundle_path: str | Path) -> RecoveryBundle:
    """Strictly verify a recovery bundle without writing into its tree."""
    with _verified_bundle(Path(bundle_path)) as state:
        return state.summary


def materialize_recovery_bundle(
    bundle_path: str | Path,
    drill_path: str | Path,
) -> RecoveryDrill:
    """Materialize a verified bundle into a new, private data-recovery drill tree.

    This never replaces a live database or attachment directory. The destination
    must be absent, and the source bundle remains descriptor-pinned and read-only
    for the duration of the materialization.
    """
    bundle = Path(bundle_path)
    bundle_root = _canonical_existing_directory(bundle, label="recovery bundle")
    with _new_directory_destination(
        Path(drill_path),
        label="data-recovery drill",
        forbidden_root=bundle_root,
    ) as destination:
        stage_owner = _StageOwner()
        published = False
        publication_attempted = False
        committed = False
        summary: RecoveryBundle | None = None
        try:
            stage = _make_private_stage(destination, owner=stage_owner)
            staged_attachments = stage.path / "attachments"
            staged_attachments.mkdir(mode=0o700)
            os.chmod(staged_attachments, 0o700)
            with _verified_bundle(bundle) as state:
                summary = state.summary
                staged_database = stage.path / "athena.db"
                copied_database_bytes, copied_database_sha256 = _copy_verified_database(
                    state.database_path, staged_database
                )
                if (
                    copied_database_bytes != state.summary.database_bytes
                    or copied_database_sha256 != state.database_sha256
                ):
                    raise ValueError(
                        "materialized recovery database does not match its bundle"
                    )

                staged_attachment_descriptor = _open_directory_path(
                    staged_attachments,
                    label="staged drill attachment directory",
                    expected_mode=0o700,
                )
                try:
                    for row in state.attachment_rows:
                        _copy_attachment(
                            state.attachment_directory_descriptor,
                            staged_attachment_descriptor,
                            row,
                        )
                    os.fsync(staged_attachment_descriptor)
                finally:
                    os.close(staged_attachment_descriptor)

                _fsync_directory(staged_attachments)
                os.fsync(stage.descriptor)

                with _verified_materialized_pair(
                    stage.path,
                    state.attachment_rows,
                    expected_database_bytes=state.summary.database_bytes,
                    expected_database_sha256=state.database_sha256,
                    allow_root_rename=True,
                    final_root_path=destination.path,
                    pinned_stage=stage,
                ):
                    publication_attempted = True
                    _publish_private_stage(
                        stage,
                        destination,
                        protected=_ProtectedDirectory(
                            descriptor=state.root_descriptor,
                            identity=state.root_identity,
                            label="recovery bundle",
                        ),
                    )
                    published = True

            assert summary is not None
            result = RecoveryDrill(path=destination.path, source_bundle=summary)
            committed = True
            return result
        except BaseException as exc:
            owned_stage = stage_owner.stage
            if owned_stage is not None:
                if committed and published:
                    exc.recovery_published_path = destination.path  # type: ignore[attr-defined]
                if isinstance(exc, RecoveryBundleDurabilityError):
                    published = True
                elif publication_attempted and not committed:
                    _retract_after_failed_validation(
                        owned_stage,
                        destination,
                    )
                    published = False
                if not published:
                    _record_preserved_stage(
                        destination,
                        owned_stage,
                        reason=exc,
                    )
            raise
        finally:
            if stage_owner.stage is not None:
                os.close(stage_owner.stage.descriptor)


def _application_schema(conn: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' AND name != 'schema_migrations' "
            "ORDER BY type, name"
        ).fetchall()
    )


@contextmanager
def _immutable_connection(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"{path.absolute().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


def _read_attachment_rows(conn: sqlite3.Connection) -> tuple[_AttachmentRow, ...]:
    rows = conn.execute(
        "SELECT id, target_kind, target_id, filename, content_type, byte_size, "
        "sha256, stored_name, uploaded_by, created_at, "
        "typeof(id) AS id_type, typeof(target_kind) AS target_kind_type, "
        "typeof(target_id) AS target_id_type, typeof(filename) AS filename_type, "
        "typeof(content_type) AS content_type_type, "
        "typeof(byte_size) AS byte_size_type, typeof(sha256) AS sha256_type, "
        "typeof(stored_name) AS stored_name_type, "
        "typeof(uploaded_by) AS uploaded_by_type, "
        "typeof(created_at) AS created_at_type "
        "FROM attachments ORDER BY stored_name"
    ).fetchall()
    result: list[_AttachmentRow] = []
    seen_names: set[str] = set()
    for position, row in enumerate(rows, start=1):
        if (
            row["id_type"] != "integer"
            or row["target_kind_type"] != "text"
            or row["target_id_type"] != "integer"
            or row["filename_type"] != "text"
            or row["content_type_type"] != "text"
            or row["byte_size_type"] != "integer"
            or row["sha256_type"] != "text"
            or row["stored_name_type"] != "text"
            or row["uploaded_by_type"] != "integer"
            or row["created_at_type"] != "text"
        ):
            raise ValueError(f"attachment metadata row {position} has invalid types")
        attachment_id = row["id"]
        target_kind = row["target_kind"]
        target_id = row["target_id"]
        filename = row["filename"]
        content_type = row["content_type"]
        byte_size = row["byte_size"]
        sha256 = row["sha256"]
        stored_name = row["stored_name"]
        uploaded_by = row["uploaded_by"]
        created_at = row["created_at"]
        if (
            not isinstance(attachment_id, int)
            or attachment_id <= 0
            or not isinstance(target_id, int)
            or target_id <= 0
            or not isinstance(byte_size, int)
            or byte_size < 0
            or not isinstance(uploaded_by, int)
            or uploaded_by <= 0
            or not isinstance(filename, str)
            or not filename
            or len(filename) > 255
            or not isinstance(content_type, str)
            or not content_type
            or not isinstance(created_at, str)
            or not created_at
            or target_kind not in {"issue", "page"}
            or not isinstance(sha256, str)
            or _SHA256_RE.fullmatch(sha256) is None
            or not isinstance(stored_name, str)
        ):
            raise ValueError(f"attachment metadata row {position} is invalid")
        try:
            attachments.disk_path(Path("."), stored_name)
        except ValueError as exc:
            raise ValueError(
                f"attachment metadata row {position} has an invalid stored name"
            ) from exc
        if stored_name in seen_names:
            raise ValueError("attachment metadata contains duplicate stored names")
        seen_names.add(stored_name)
        target_table = "issues" if target_kind == "issue" else "pages"
        if (
            conn.execute(
                f"SELECT 1 FROM {target_table} WHERE id = ?", (target_id,)
            ).fetchone()
            is None
        ):
            raise ValueError(f"attachment metadata row {position} has a missing target")
        result.append(
            _AttachmentRow(
                attachment_id=attachment_id,
                stored_name=stored_name,
                byte_size=byte_size,
                sha256=sha256,
            )
        )
    return tuple(result)


def _attachment_set_sha256(rows: tuple[_AttachmentRow, ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        payload = json.dumps(
            [row.stored_name, row.byte_size, row.sha256],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        digest.update(payload)
        digest.update(b"\n")
    return digest.hexdigest()


@contextmanager
def _verified_bundle(
    bundle_path: Path,
    *,
    allow_root_rename: bool = False,
    final_root_path: Path | None = None,
    pinned_stage: _PrivateStage | None = None,
) -> Iterator[_VerifiedBundleState]:
    bundle = bundle_path.absolute()
    root_descriptor = _open_recovery_root(
        bundle,
        label="recovery bundle",
        expected_mode=0o700,
        pinned_stage=pinned_stage,
    )
    manifest_descriptor = -1
    database_descriptor = -1
    attachment_descriptor = -1
    try:
        root_metadata = os.fstat(root_descriptor)
        root_entries = _directory_names(root_descriptor)
        if root_entries != _BUNDLE_ROOT_ENTRIES:
            raise ValueError("recovery bundle has missing or unexpected root entries")

        manifest_descriptor, manifest_metadata = _open_regular_at(
            root_descriptor,
            "manifest.json",
            label="recovery manifest",
            expected_mode=0o600,
        )
        manifest_payload = _read_bounded_descriptor(
            manifest_descriptor,
            maximum_bytes=_MANIFEST_MAX_BYTES,
            label="recovery manifest",
        )
        _require_stable_descriptor(
            manifest_descriptor,
            manifest_metadata,
            label="recovery manifest",
        )
        manifest = _parse_manifest(manifest_payload)

        database_descriptor, database_metadata = _open_regular_at(
            root_descriptor,
            "athena.db",
            label="recovery database",
            expected_mode=0o600,
        )
        attachment_descriptor, attachment_directory_metadata = _open_directory_at(
            root_descriptor,
            "attachments",
            label="recovery attachment directory",
            expected_mode=0o700,
        )

        with tempfile.TemporaryDirectory(prefix="athena-recovery-verify-") as temp:
            private_database = Path(temp) / "athena.db"
            database_bytes, database_sha256 = _copy_descriptor_to_path(
                database_descriptor,
                private_database,
                source_metadata=database_metadata,
                label="recovery database",
            )
            if (
                database_bytes != manifest.database_bytes
                or database_sha256 != manifest.database_sha256
            ):
                raise ValueError("recovery database does not match its manifest")

            with _immutable_connection(private_database) as conn:
                status = validate_current_database(conn)
                attachment_rows = _read_attachment_rows(conn)
            if status.latest != manifest.latest_migration:
                raise ValueError("recovery database migration does not match manifest")

            attachment_names = _directory_names(attachment_descriptor)
            expected_names = frozenset(row.stored_name for row in attachment_rows)
            if attachment_names != expected_names:
                raise ValueError(
                    "recovery attachment directory has missing or unexpected entries"
                )
            attachment_bytes, attachment_metadata = _hash_attachment_rows(
                attachment_descriptor,
                attachment_rows,
                expected_mode=0o600,
            )
            if (
                len(attachment_rows) != manifest.attachment_count
                or attachment_bytes != manifest.attachment_bytes
                or _attachment_set_sha256(attachment_rows)
                != manifest.attachment_set_sha256
            ):
                raise ValueError("recovery attachments do not match their manifest")

            summary = RecoveryBundle(
                path=bundle,
                capture_started_at=manifest.capture_started_at,
                capture_completed_at=manifest.capture_completed_at,
                database_bytes=database_bytes,
                attachment_count=len(attachment_rows),
                attachment_bytes=attachment_bytes,
                athena_version=manifest.athena_version,
                latest_migration=status.latest,
            )
            state = _VerifiedBundleState(
                summary=summary,
                root_descriptor=root_descriptor,
                root_identity=(root_metadata.st_dev, root_metadata.st_ino),
                database_path=private_database,
                database_sha256=database_sha256,
                attachment_rows=attachment_rows,
                attachment_directory_descriptor=attachment_descriptor,
            )
            yield state

            _require_verified_bundle_stable(
                bundle=bundle,
                final_root_path=final_root_path,
                allow_root_rename=allow_root_rename,
                root_descriptor=root_descriptor,
                root_metadata=root_metadata,
                root_entries=root_entries,
                manifest_descriptor=manifest_descriptor,
                manifest_metadata=manifest_metadata,
                database_descriptor=database_descriptor,
                database_metadata=database_metadata,
                attachment_descriptor=attachment_descriptor,
                attachment_directory_metadata=attachment_directory_metadata,
                attachment_names=attachment_names,
                attachment_metadata=attachment_metadata,
            )
    finally:
        for descriptor in (
            attachment_descriptor,
            database_descriptor,
            manifest_descriptor,
            root_descriptor,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def _require_verified_bundle_stable(
    *,
    bundle: Path,
    final_root_path: Path | None,
    allow_root_rename: bool,
    root_descriptor: int,
    root_metadata: os.stat_result,
    root_entries: frozenset[str],
    manifest_descriptor: int,
    manifest_metadata: os.stat_result,
    database_descriptor: int,
    database_metadata: os.stat_result,
    attachment_descriptor: int,
    attachment_directory_metadata: os.stat_result,
    attachment_names: frozenset[str],
    attachment_metadata: dict[str, os.stat_result],
) -> None:
    _require_attachment_rows_stable(attachment_descriptor, attachment_metadata)
    if _directory_names(attachment_descriptor) != attachment_names:
        raise ValueError("recovery attachment directory changed during check")
    _require_stable_descriptor(
        attachment_descriptor,
        attachment_directory_metadata,
        label="recovery attachment directory",
    )
    _require_stable_descriptor(
        database_descriptor,
        database_metadata,
        label="recovery database",
    )
    _require_stable_descriptor(
        manifest_descriptor,
        manifest_metadata,
        label="recovery manifest",
    )
    if _directory_names(root_descriptor) != root_entries:
        raise ValueError("recovery bundle changed during check")
    if allow_root_rename:
        _require_stable_directory_after_rename(
            root_descriptor,
            root_metadata,
            label="recovery bundle",
        )
    else:
        _require_stable_descriptor(
            root_descriptor,
            root_metadata,
            label="recovery bundle",
        )
    _require_directory_identity(
        final_root_path if final_root_path is not None else bundle,
        (root_metadata.st_dev, root_metadata.st_ino),
        label="recovery bundle path",
    )


@contextmanager
def _verified_materialized_pair(
    stage: Path,
    expected_rows: tuple[_AttachmentRow, ...],
    *,
    expected_database_bytes: int,
    expected_database_sha256: str,
    allow_root_rename: bool = False,
    final_root_path: Path | None = None,
    pinned_stage: _PrivateStage | None = None,
) -> Iterator[None]:
    root_descriptor = _open_recovery_root(
        stage,
        label="staged data-recovery drill",
        expected_mode=0o700,
        pinned_stage=pinned_stage,
    )
    database_descriptor = -1
    attachment_descriptor = -1
    try:
        root_metadata = os.fstat(root_descriptor)
        root_entries = _directory_names(root_descriptor)
        if root_entries != frozenset({"athena.db", "attachments"}):
            raise ValueError("data-recovery drill has an unexpected layout")
        database_descriptor, database_metadata = _open_regular_at(
            root_descriptor,
            "athena.db",
            label="drill database",
            expected_mode=0o600,
        )
        attachment_descriptor, attachment_directory_metadata = _open_directory_at(
            root_descriptor,
            "attachments",
            label="drill attachment directory",
            expected_mode=0o700,
        )
        with tempfile.TemporaryDirectory(
            prefix="athena-recovery-drill-verify-"
        ) as temp:
            private_database = Path(temp) / "athena.db"
            database_bytes, database_sha256 = _copy_descriptor_to_path(
                database_descriptor,
                private_database,
                source_metadata=database_metadata,
                label="drill database",
            )
            if (
                database_bytes != expected_database_bytes
                or database_sha256 != expected_database_sha256
            ):
                raise ValueError(
                    "materialized recovery database does not match its bundle"
                )
            with _immutable_connection(private_database) as conn:
                validate_current_database(conn)
                actual_rows = _read_attachment_rows(conn)
        if actual_rows != expected_rows:
            raise ValueError(
                "data-recovery drill database changed attachment membership"
            )
        attachment_names = _directory_names(attachment_descriptor)
        if attachment_names != frozenset(row.stored_name for row in actual_rows):
            raise ValueError("data-recovery drill attachment layout is incomplete")
        _, attachment_metadata = _hash_attachment_rows(
            attachment_descriptor,
            actual_rows,
            expected_mode=0o600,
        )
        yield

        _require_attachment_rows_stable(
            attachment_descriptor,
            attachment_metadata,
        )
        if _directory_names(attachment_descriptor) != attachment_names:
            raise ValueError("data-recovery drill attachment directory changed")
        _require_stable_descriptor(
            attachment_descriptor,
            attachment_directory_metadata,
            label="drill attachment directory",
        )
        _require_stable_descriptor(
            database_descriptor,
            database_metadata,
            label="drill database",
        )
        if _directory_names(root_descriptor) != root_entries:
            raise ValueError("data-recovery drill changed during publication")
        if allow_root_rename:
            _require_stable_directory_after_rename(
                root_descriptor,
                root_metadata,
                label="data-recovery drill",
            )
        else:
            _require_stable_descriptor(
                root_descriptor,
                root_metadata,
                label="data-recovery drill",
            )
        _require_directory_identity(
            final_root_path if final_root_path is not None else stage,
            (root_metadata.st_dev, root_metadata.st_ino),
            label="data-recovery drill path",
        )
    finally:
        if attachment_descriptor >= 0:
            os.close(attachment_descriptor)
        if database_descriptor >= 0:
            os.close(database_descriptor)
        os.close(root_descriptor)


def _parse_manifest(payload: bytes) -> _BundleManifest:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("recovery manifest is not valid UTF-8") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("recovery manifest is not strict JSON") from exc
    root = _require_json_object(parsed, label="recovery manifest")
    _require_exact_keys(
        root,
        {
            "schema",
            "athena_version",
            "capture_started_at",
            "capture_completed_at",
            "database",
            "attachments",
        },
        label="recovery manifest",
    )
    if _require_string(root, "schema") != _BUNDLE_SCHEMA:
        raise ValueError("recovery manifest has an unsupported schema")
    athena_version = _require_string(root, "athena_version", maximum_length=64)
    if not athena_version:
        raise ValueError("recovery manifest has an invalid Athena version")
    capture_started_at = _parse_utc(
        _require_string(root, "capture_started_at", maximum_length=32)
    )
    capture_completed_at = _parse_utc(
        _require_string(root, "capture_completed_at", maximum_length=32)
    )
    now = _utc_now()
    if capture_started_at > capture_completed_at:
        raise ValueError("recovery capture timestamps are reversed")
    if capture_completed_at > now:
        raise ValueError("recovery capture timestamp is in the future")

    database = _require_json_object(root["database"], label="database manifest")
    _require_exact_keys(
        database,
        {"path", "byte_size", "sha256", "latest_migration"},
        label="database manifest",
    )
    if _require_string(database, "path") != "athena.db":
        raise ValueError("recovery manifest has an invalid database path")
    database_bytes = _require_nonnegative_integer(database, "byte_size")
    database_sha256 = _require_sha256(database, "sha256")
    latest_migration = _require_string(
        database,
        "latest_migration",
        maximum_length=255,
    )
    if not latest_migration:
        raise ValueError("recovery manifest has an invalid latest migration")

    attachment_manifest = _require_json_object(
        root["attachments"], label="attachment manifest"
    )
    _require_exact_keys(
        attachment_manifest,
        {"path", "count", "byte_size", "set_sha256"},
        label="attachment manifest",
    )
    if _require_string(attachment_manifest, "path") != "attachments":
        raise ValueError("recovery manifest has an invalid attachment path")
    return _BundleManifest(
        capture_started_at=capture_started_at,
        capture_completed_at=capture_completed_at,
        database_bytes=database_bytes,
        database_sha256=database_sha256,
        latest_migration=latest_migration,
        attachment_count=_require_nonnegative_integer(attachment_manifest, "count"),
        attachment_bytes=_require_nonnegative_integer(attachment_manifest, "byte_size"),
        attachment_set_sha256=_require_sha256(attachment_manifest, "set_sha256"),
        athena_version=athena_version,
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _require_json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _require_exact_keys(
    value: dict[str, object],
    expected: set[str],
    *,
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} has missing or unknown fields")


def _require_string(
    value: dict[str, object],
    key: str,
    *,
    maximum_length: int | None = None,
) -> str:
    field = value[key]
    if not isinstance(field, str):
        raise ValueError(f"recovery manifest field {key!r} must be a string")
    if maximum_length is not None and len(field) > maximum_length:
        raise ValueError(f"recovery manifest field {key!r} is too long")
    return field


def _require_nonnegative_integer(value: dict[str, object], key: str) -> int:
    field = value[key]
    if type(field) is not int or field < 0:
        raise ValueError(
            f"recovery manifest field {key!r} must be a non-negative integer"
        )
    return field


def _require_sha256(value: dict[str, object], key: str) -> str:
    field = _require_string(value, key, maximum_length=64)
    if _SHA256_RE.fullmatch(field) is None:
        raise ValueError(f"recovery manifest field {key!r} is not SHA-256")
    return field


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    if _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("recovery manifest timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("recovery manifest timestamp is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("recovery manifest timestamp is not UTC")
    return parsed


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    payload = (
        json.dumps(
            manifest,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    if len(payload) > _MANIFEST_MAX_BYTES:
        raise ValueError("recovery manifest exceeds its size limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_bounded_descriptor(
    descriptor: int,
    *,
    maximum_bytes: int,
    label: str,
) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(descriptor, min(_HASH_CHUNK_BYTES, maximum_bytes + 1)):
        total += len(chunk)
        if total > maximum_bytes:
            raise ValueError(f"{label} exceeds its size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while creating recovery artifact")
        view = view[written:]


def _directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_only = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_only is None:
        raise OSError(
            errno.ENOTSUP,
            "this platform cannot verify recovery directories without symlinks",
        )
    return os.O_RDONLY | no_follow | directory_only | getattr(os, "O_CLOEXEC", 0)


def _regular_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(
            errno.ENOTSUP,
            "this platform cannot verify recovery files without symlinks",
        )
    return (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _open_directory_path(
    path: Path,
    *,
    label: str,
    expected_mode: int | None = None,
) -> int:
    descriptor = os.open(path, _directory_flags())
    try:
        _validate_directory_descriptor(
            descriptor,
            label=label,
            expected_mode=expected_mode,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_recovery_root(
    path: Path,
    *,
    label: str,
    expected_mode: int,
    pinned_stage: _PrivateStage | None,
) -> int:
    if pinned_stage is None:
        return _open_directory_path(
            path,
            label=label,
            expected_mode=expected_mode,
        )
    descriptor = os.dup(pinned_stage.descriptor)
    try:
        metadata = _validate_directory_descriptor(
            descriptor,
            label=label,
            expected_mode=expected_mode,
        )
        if (metadata.st_dev, metadata.st_ino) != pinned_stage.identity:
            raise RecoveryRaceError(f"{label} descriptor was replaced")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
    expected_mode: int | None = None,
) -> tuple[int, os.stat_result]:
    descriptor = os.open(
        name,
        _directory_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        metadata = _validate_directory_descriptor(
            descriptor,
            label=label,
            expected_mode=expected_mode,
        )
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _validate_directory_descriptor(
    descriptor: int,
    *,
    label: str,
    expected_mode: int | None,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"{label} is not a directory")
    if expected_mode is not None and stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PermissionError(f"{label} must have mode {expected_mode:04o}")
    return metadata


def _open_regular_at(
    directory_descriptor: int,
    name: str,
    *,
    label: str,
    expected_mode: int | None = None,
) -> tuple[int, os.stat_result]:
    descriptor = os.open(
        name,
        _regular_flags(),
        dir_fd=directory_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file")
        if metadata.st_nlink != 1:
            raise ValueError(f"{label} must not be hard-linked")
        if (
            expected_mode is not None
            and stat.S_IMODE(metadata.st_mode) != expected_mode
        ):
            raise PermissionError(f"{label} must have mode {expected_mode:04o}")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _directory_names(descriptor: int) -> frozenset[str]:
    with os.scandir(descriptor) as entries:
        return frozenset(entry.name for entry in entries)


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_stable_descriptor(
    descriptor: int,
    initial: os.stat_result,
    *,
    label: str,
    allow_unlinked: bool = False,
) -> None:
    current = os.fstat(descriptor)
    if _metadata_signature(current) == _metadata_signature(initial):
        return
    # Unlinking a regular file after it has been safely opened changes ctime and
    # drops nlink to zero without changing the descriptor-pinned historical bytes.
    # Athena deletion commits row removal before unlink, so allowing only this
    # exact transition preserves a valid snapshot-visible blob. A replacement,
    # hard-link addition, write, truncate, or append still changes a compared field.
    if (
        allow_unlinked
        and stat.S_ISREG(initial.st_mode)
        and initial.st_nlink == 1
        and current.st_nlink == 0
        and (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_size,
            current.st_mtime_ns,
        )
        == (
            initial.st_dev,
            initial.st_ino,
            initial.st_mode,
            initial.st_size,
            initial.st_mtime_ns,
        )
    ):
        return
    raise ValueError(f"{label} changed during recovery processing")


def _require_stable_directory_after_rename(
    descriptor: int,
    initial: os.stat_result,
    *,
    label: str,
) -> None:
    """Allow only the ctime transition caused by publishing a directory rename."""
    current = os.fstat(descriptor)
    if (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_nlink,
        current.st_size,
        current.st_mtime_ns,
    ) != (
        initial.st_dev,
        initial.st_ino,
        initial.st_mode,
        initial.st_nlink,
        initial.st_size,
        initial.st_mtime_ns,
    ):
        raise ValueError(f"{label} changed during recovery publication")


def _hash_descriptor(
    descriptor: int,
    initial: os.stat_result,
    *,
    label: str,
) -> tuple[int, str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    byte_size = 0
    while chunk := os.read(descriptor, _HASH_CHUNK_BYTES):
        byte_size += len(chunk)
        digest.update(chunk)
    _require_stable_descriptor(descriptor, initial, label=label)
    if byte_size != initial.st_size:
        raise ValueError(f"{label} changed size during recovery processing")
    return byte_size, digest.hexdigest()


def _hash_regular_at(
    directory_descriptor: int,
    name: str,
    *,
    label: str,
    expected_mode: int | None,
) -> tuple[int, str, os.stat_result]:
    descriptor, metadata = _open_regular_at(
        directory_descriptor,
        name,
        label=label,
        expected_mode=expected_mode,
    )
    try:
        byte_size, digest = _hash_descriptor(descriptor, metadata, label=label)
        return byte_size, digest, metadata
    finally:
        os.close(descriptor)


def _copy_descriptor_to_path(
    source_descriptor: int,
    destination: Path,
    *,
    source_metadata: os.stat_result,
    label: str,
) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    destination_descriptor = os.open(destination, flags, 0o600)
    try:
        os.fchmod(destination_descriptor, 0o600)
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        byte_size = 0
        while chunk := os.read(source_descriptor, _HASH_CHUNK_BYTES):
            byte_size += len(chunk)
            digest.update(chunk)
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
        _require_stable_descriptor(
            source_descriptor,
            source_metadata,
            label=label,
        )
        if byte_size != source_metadata.st_size:
            raise ValueError(f"{label} changed size during recovery processing")
        return byte_size, digest.hexdigest()
    finally:
        os.close(destination_descriptor)


def _copy_verified_database(source: Path, destination: Path) -> tuple[int, str]:
    parent_descriptor = _open_directory_path(
        source.parent,
        label="verified recovery database parent",
    )
    source_descriptor = -1
    try:
        source_descriptor, source_metadata = _open_regular_at(
            parent_descriptor,
            source.name,
            label="verified recovery database",
            expected_mode=0o600,
        )
        return _copy_descriptor_to_path(
            source_descriptor,
            destination,
            source_metadata=source_metadata,
            label="verified recovery database",
        )
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(parent_descriptor)


def _copy_attachment(
    source_directory_descriptor: int,
    destination_directory_descriptor: int,
    row: _AttachmentRow,
) -> os.stat_result:
    label = f"attachment blob for metadata row {row.attachment_id}"
    source_descriptor = -1
    destination_descriptor = -1
    try:
        source_descriptor, source_metadata = _open_regular_at(
            source_directory_descriptor,
            row.stored_name,
            label=label,
        )
        if source_metadata.st_size != row.byte_size:
            raise ValueError(f"{label} size does not match the database snapshot")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        destination_descriptor = os.open(
            row.stored_name,
            flags,
            0o600,
            dir_fd=destination_directory_descriptor,
        )
        os.fchmod(destination_descriptor, 0o600)
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        byte_size = 0
        while chunk := os.read(source_descriptor, _HASH_CHUNK_BYTES):
            byte_size += len(chunk)
            if byte_size > row.byte_size:
                raise ValueError(f"{label} grew during recovery capture")
            digest.update(chunk)
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
        _require_stable_descriptor(
            source_descriptor,
            source_metadata,
            label=label,
            allow_unlinked=True,
        )
        if byte_size != row.byte_size or digest.hexdigest() != row.sha256:
            raise ValueError(f"{label} does not match the database snapshot")
        return source_metadata
    except OSError as exc:
        bounded = _bounded_attachment_os_error(
            exc,
            label=label,
            action="copied",
            sensitive_name=row.stored_name,
        )
        if bounded is exc:
            raise
        raise bounded from exc
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)


def _hash_attachment_rows(
    directory_descriptor: int,
    rows: tuple[_AttachmentRow, ...],
    *,
    expected_mode: int,
) -> tuple[int, dict[str, os.stat_result]]:
    total_bytes = 0
    metadata: dict[str, os.stat_result] = {}
    for row in rows:
        label = f"attachment blob for metadata row {row.attachment_id}"
        try:
            byte_size, sha256, initial = _hash_regular_at(
                directory_descriptor,
                row.stored_name,
                label=label,
                expected_mode=expected_mode,
            )
        except OSError as exc:
            raise _bounded_attachment_os_error(
                exc,
                label=label,
                action="verified",
                sensitive_name=row.stored_name,
            ) from exc
        if byte_size != row.byte_size or sha256 != row.sha256:
            raise ValueError(f"{label} does not match the recovery database")
        metadata[row.stored_name] = initial
        total_bytes += byte_size
    return total_bytes, metadata


def _require_attachment_rows_stable(
    directory_descriptor: int,
    initial: dict[str, os.stat_result],
) -> None:
    for position, stored_name in enumerate(sorted(initial), start=1):
        label = f"attachment blob {position}"
        try:
            descriptor, _ = _open_regular_at(
                directory_descriptor,
                stored_name,
                label=label,
                expected_mode=0o600,
            )
        except OSError as exc:
            raise _bounded_attachment_os_error(
                exc,
                label=label,
                action="rechecked",
                sensitive_name=stored_name,
            ) from exc
        try:
            _require_stable_descriptor(
                descriptor,
                initial[stored_name],
                label=label,
            )
        finally:
            os.close(descriptor)


def _bounded_attachment_os_error(
    exc: OSError,
    *,
    label: str,
    action: str,
    sensitive_name: str,
) -> OSError:
    if sensitive_name not in str(exc):
        return exc
    return OSError(
        exc.errno if exc.errno is not None else errno.EIO,
        f"{label} could not be {action}",
    )


def _open_recovery_source_database(path: Path) -> tuple[int, tuple[int, int]]:
    try:
        descriptor = os.open(path, _regular_flags())
    except FileNotFoundError:
        raise FileNotFoundError(f"database path does not exist: {path}") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FileNotFoundError(
                f"database path is not a regular file: {path}"
            ) from None
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FileNotFoundError(f"database path is not a regular file: {path}")
        if metadata.st_nlink != 1:
            raise ValueError("recovery source database must not be hard-linked")
        return descriptor, (metadata.st_dev, metadata.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _open_source_attachment_directory(
    path: Path,
) -> tuple[Path, int, tuple[int, int]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"attachment directory does not exist: {path}"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"attachment path is not a directory: {path}")
    descriptor = _open_directory_path(path, label="attachment directory")
    try:
        canonical = path.resolve(strict=True)
        metadata = os.fstat(descriptor)
        _require_directory_identity(
            canonical,
            (metadata.st_dev, metadata.st_ino),
            label="attachment directory",
        )
        identity = (metadata.st_dev, metadata.st_ino)
        return canonical, descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _canonical_existing_directory(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"{label} does not exist: {path}") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"{label} is not a directory: {path}")
    descriptor = _open_directory_path(path, label=label)
    try:
        canonical = path.resolve(strict=True)
        opened = os.fstat(descriptor)
        _require_directory_identity(
            canonical,
            (opened.st_dev, opened.st_ino),
            label=label,
        )
        return canonical
    finally:
        os.close(descriptor)


def _canonical_descriptor_path(
    descriptor: int,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> Path:
    descriptor_path = Path(f"/proc/self/fd/{descriptor}")
    try:
        canonical = descriptor_path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryRaceError(f"{label} no longer has a stable path") from exc
    _require_directory_identity(canonical, expected_identity, label=label)
    return canonical


def _require_open_directory_path_stable(
    path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    metadata = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise RecoveryRaceError(f"{label} descriptor was replaced")
    _require_directory_identity(path, expected_identity, label=label)


def _require_destination_outside_open_directory(
    destination: _NewDirectoryDestination,
    protected_descriptor: int,
    protected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    protected = _canonical_descriptor_path(
        protected_descriptor,
        protected_identity,
        label=label,
    )
    current_parent = _canonical_descriptor_path(
        destination.parent_descriptor,
        destination.parent_identity,
        label="recovery destination parent",
    )
    current_destination = current_parent / destination.name
    if current_destination == protected or current_destination.is_relative_to(
        protected
    ):
        raise ValueError(f"recovery destination moved inside the {label}")


@contextmanager
def _new_directory_destination(
    requested: Path,
    *,
    label: str,
    forbidden_root: Path,
) -> Iterator[_NewDirectoryDestination]:
    if requested.name in {"", ".", ".."}:
        raise ValueError(f"{label} path must name a new directory")
    parent = requested.parent
    try:
        parent_metadata = parent.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{label} parent directory does not exist: {parent}"
        ) from None
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise NotADirectoryError(f"{label} parent is not a directory: {parent}")
    parent_descriptor = _open_directory_path(parent, label=f"{label} parent")
    try:
        canonical_parent = parent.resolve(strict=True)
        opened_parent = os.fstat(parent_descriptor)
        parent_identity = (opened_parent.st_dev, opened_parent.st_ino)
        _require_directory_identity(
            canonical_parent,
            parent_identity,
            label=f"{label} parent",
        )
        destination = canonical_parent / requested.name
        if _entry_exists(parent_descriptor, requested.name):
            raise FileExistsError(f"{label} path already exists: {destination}")
        forbidden = forbidden_root.resolve(strict=True)
        if destination == forbidden or destination.is_relative_to(forbidden):
            raise ValueError(f"{label} must not be inside the protected source tree")
        yield _NewDirectoryDestination(
            path=destination,
            name=requested.name,
            parent_path=canonical_parent,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
        )
    finally:
        os.close(parent_descriptor)


def _entry_exists(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _record_preserved_stage(
    destination: _NewDirectoryDestination,
    stage: _PrivateStage,
    *,
    reason: BaseException,
) -> Path | None:
    if not _entry_exists(destination.parent_descriptor, stage.name):
        return None
    _require_directory_identity_at(
        destination.parent_descriptor,
        stage.name,
        stage.identity,
        label="failed recovery stage",
    )
    current_parent = _canonical_descriptor_path(
        destination.parent_descriptor,
        destination.parent_identity,
        label="recovery destination parent",
    )
    path = current_parent / stage.name
    reason.recovery_stage_path = path  # type: ignore[attr-defined]
    return path


def _raise_stage_setup_failure(
    destination: _NewDirectoryDestination,
    name: str,
    exc: BaseException,
) -> NoReturn:
    current_parent = _canonical_descriptor_path(
        destination.parent_descriptor,
        destination.parent_identity,
        label="recovery destination parent",
    )
    path = current_parent / name
    if not isinstance(exc, Exception):
        exc.recovery_stage_path = path  # type: ignore[attr-defined]
        raise exc
    raise RecoveryStagePreservedError(path, reason=str(exc)) from exc


def _make_private_stage(
    destination: _NewDirectoryDestination,
    *,
    owner: _StageOwner | None = None,
) -> _PrivateStage:
    for _ in range(100):
        name = f".{destination.name}.{secrets.token_hex(12)}.tmp"
        try:
            os.mkdir(name, 0o700, dir_fd=destination.parent_descriptor)
        except FileExistsError:
            continue
        except BaseException as exc:
            if _entry_exists(destination.parent_descriptor, name):
                _raise_stage_setup_failure(destination, name, exc)
            raise
        try:
            descriptor, metadata = _open_directory_at(
                destination.parent_descriptor,
                name,
                label="private recovery stage",
            )
        except BaseException as exc:  # noqa: BLE001 — translated into a stage-setup failure, never swallowed
            _raise_stage_setup_failure(destination, name, exc)
        stage = _PrivateStage(
            path=Path(f"/proc/self/fd/{descriptor}"),
            name=name,
            identity=(metadata.st_dev, metadata.st_ino),
            descriptor=descriptor,
        )
        try:
            if _directory_names(descriptor):
                raise RecoveryRaceError(
                    "private recovery stage was replaced during allocation"
                )
            os.fchmod(descriptor, 0o700)
            _require_directory_identity_at(
                destination.parent_descriptor,
                name,
                stage.identity,
                label="private recovery stage",
            )
            if owner is not None:
                owner.stage = stage
            return stage
        except BaseException as exc:  # noqa: BLE001 — translated into a stage-setup failure, never swallowed
            if owner is not None:
                owner.stage = None
            try:
                _raise_stage_setup_failure(destination, name, exc)
            finally:
                os.close(descriptor)
    raise FileExistsError("could not allocate a unique private recovery stage")


def _publish_private_stage(
    stage: _PrivateStage,
    destination: _NewDirectoryDestination,
    *,
    protected: _ProtectedDirectory,
) -> None:
    _require_directory_identity_at(
        destination.parent_descriptor,
        stage.name,
        stage.identity,
        label="verified recovery stage",
    )
    _require_destination_outside_open_directory(
        destination,
        protected.descriptor,
        protected.identity,
        label=protected.label,
    )
    _rename_no_replace_at(
        destination.parent_descriptor,
        stage.name,
        destination.name,
    )
    try:
        _require_published_stage_safe(stage, destination, protected=protected)
    except Exception as exc:  # noqa: BLE001 — any validation failure must retract the publication
        _raise_retracted_publication(stage, destination, cause=exc)
    try:
        os.fsync(destination.parent_descriptor)
    except OSError as exc:
        try:
            _require_published_stage_safe(stage, destination, protected=protected)
        except Exception as validation_exc:  # noqa: BLE001 — any validation failure must retract the publication
            _raise_retracted_publication(
                stage,
                destination,
                cause=validation_exc,
            )
        raise RecoveryBundleDurabilityError(destination.path) from exc
    try:
        _require_published_stage_safe(stage, destination, protected=protected)
    except Exception as exc:  # noqa: BLE001 — any validation failure must retract the publication
        _raise_retracted_publication(stage, destination, cause=exc)


def _require_published_stage_safe(
    stage: _PrivateStage,
    destination: _NewDirectoryDestination,
    *,
    protected: _ProtectedDirectory,
) -> None:
    _require_directory_identity_at(
        destination.parent_descriptor,
        destination.name,
        stage.identity,
        label="published recovery artifact",
    )
    _require_destination_outside_open_directory(
        destination,
        protected.descriptor,
        protected.identity,
        label=protected.label,
    )
    _require_directory_identity(
        destination.parent_path,
        destination.parent_identity,
        label="recovery destination parent",
    )
    _require_directory_identity(
        destination.path,
        stage.identity,
        label="published recovery artifact path",
    )


def _retract_published_stage(
    stage: _PrivateStage,
    destination: _NewDirectoryDestination,
) -> None:
    _require_directory_identity_at(
        destination.parent_descriptor,
        destination.name,
        stage.identity,
        label="published recovery artifact",
    )
    _rename_no_replace_at(
        destination.parent_descriptor,
        destination.name,
        stage.name,
    )
    _require_directory_identity_at(
        destination.parent_descriptor,
        stage.name,
        stage.identity,
        label="retracted recovery stage",
    )
    os.fsync(destination.parent_descriptor)


def _raise_retracted_publication(
    stage: _PrivateStage,
    destination: _NewDirectoryDestination,
    *,
    cause: BaseException,
) -> None:
    try:
        _retract_published_stage(stage, destination)
    except BaseException as rollback_exc:
        raise RecoveryRaceError(
            "recovery artifact failed post-publication validation and rollback "
            "failed; inspect the destination parent before retrying"
        ) from rollback_exc
    raise RecoveryRaceError(
        "recovery destination ancestry changed during publication; publication "
        "was rolled back to its private stage"
    ) from cause


def _retract_after_failed_validation(
    stage: _PrivateStage,
    destination: _NewDirectoryDestination,
) -> None:
    if _entry_exists(destination.parent_descriptor, stage.name):
        _require_directory_identity_at(
            destination.parent_descriptor,
            stage.name,
            stage.identity,
            label="failed recovery stage",
        )
        return
    try:
        _retract_published_stage(stage, destination)
    except BaseException as rollback_exc:
        raise RecoveryRaceError(
            "published recovery artifact failed final validation and rollback "
            "failed; inspect the destination parent before retrying"
        ) from rollback_exc


def _require_directory_identity(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    descriptor = _open_directory_path(path, label=label)
    try:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != expected_identity:
            raise RecoveryRaceError(f"{label} was replaced during publication")
    finally:
        os.close(descriptor)


def _require_directory_identity_at(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    descriptor, metadata = _open_directory_at(
        parent_descriptor,
        name,
        label=label,
    )
    try:
        if (metadata.st_dev, metadata.st_ino) != expected_identity:
            raise RecoveryRaceError(f"{label} was replaced during processing")
    finally:
        os.close(descriptor)


def _rename_no_replace_at(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-clobber directory publication is unavailable",
        ) from exc
    result = renameat2(
        parent_descriptor,
        os.fsencode(source_name),
        parent_descriptor,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            "recovery destination appeared during publication",
            destination_name,
        )
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _snapshot_recovery_database(
    source_path: Path,
    source_descriptor: int,
    source_identity: tuple[int, int],
    destination: Path,
    destination_directory_descriptor: int,
) -> Path:
    source = Path(f"/proc/self/fd/{source_descriptor}")
    staged = _stage_sqlite_database(
        source,
        destination,
        expected_source_path=source_path,
        expected_source_descriptor=source_descriptor,
        expected_source_identity=source_identity,
        source_readonly=True,
    )
    try:
        _require_regular_descriptor_identity(
            staged.descriptor,
            staged.identity,
            label="staged recovery database",
        )
        _require_regular_path_identity(
            staged.path,
            staged.identity,
            label="staged recovery database",
        )
        _rename_no_replace_at(
            destination_directory_descriptor,
            staged.path.name,
            destination.name,
        )
        published_metadata = os.stat(
            destination.name,
            dir_fd=destination_directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published_metadata.st_mode)
            or published_metadata.st_nlink != 1
            or (published_metadata.st_dev, published_metadata.st_ino) != staged.identity
        ):
            raise RecoveryRaceError(
                "staged recovery database was replaced during rename"
            )
        os.fsync(destination_directory_descriptor)
        return destination
    finally:
        os.close(staged.descriptor)


def _require_regular_descriptor_identity(
    descriptor: int,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise RecoveryRaceError(f"{label} descriptor was lost during snapshot") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (metadata.st_dev, metadata.st_ino) != expected_identity
    ):
        raise RecoveryRaceError(f"{label} was replaced during snapshot")


def _require_regular_path_identity(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise RecoveryRaceError(f"{label} disappeared during snapshot") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (metadata.st_dev, metadata.st_ino) != expected_identity
    ):
        raise RecoveryRaceError(f"{label} path was replaced during snapshot")


def _stage_sqlite_database(
    source: Path,
    destination: Path,
    *,
    expected_source_path: Path | None = None,
    expected_source_descriptor: int | None = None,
    expected_source_identity: tuple[int, int] | None = None,
    source_readonly: bool = False,
) -> _PinnedStagedDatabase:
    source_guards = (
        expected_source_path,
        expected_source_descriptor,
        expected_source_identity,
    )
    if any(value is None for value in source_guards) and any(
        value is not None for value in source_guards
    ):
        raise ValueError(
            "source path, descriptor, and identity must be supplied together"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _make_pinned_temporary_file(destination)

    try:
        source_connection = _connect_readonly if source_readonly else _connect
        with closing(source_connection(source)) as source_conn:
            if expected_source_descriptor is not None:
                assert expected_source_path is not None
                assert expected_source_identity is not None
                _require_regular_path_identity(
                    expected_source_path,
                    expected_source_identity,
                    label="recovery source database",
                )
                _require_regular_descriptor_identity(
                    expected_source_descriptor,
                    expected_source_identity,
                    label="recovery source database",
                )
            pinned_path = Path(f"/proc/self/fd/{temporary.descriptor}")
            with closing(_connect(pinned_path)) as destination_conn:
                source_conn.backup(destination_conn)
                if expected_source_descriptor is not None:
                    assert expected_source_path is not None
                    assert expected_source_identity is not None
                    _require_regular_path_identity(
                        expected_source_path,
                        expected_source_identity,
                        label="recovery source database",
                    )
                    _require_regular_descriptor_identity(
                        expected_source_descriptor,
                        expected_source_identity,
                        label="recovery source database",
                    )
                result = [
                    row[0] for row in destination_conn.execute("PRAGMA quick_check")
                ]
                if result != ["ok"]:
                    raise sqlite3.DatabaseError(
                        "staged database failed SQLite quick_check"
                    )
        os.fsync(temporary.descriptor)
        _require_regular_descriptor_identity(
            temporary.descriptor,
            temporary.identity,
            label="staged recovery database",
        )
        _require_regular_path_identity(
            temporary.path,
            temporary.identity,
            label="staged recovery database",
        )
        return temporary
    except BaseException:
        os.close(temporary.descriptor)
        raise


def _make_pinned_temporary_file(path: Path) -> _PinnedStagedDatabase:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        metadata = os.fstat(descriptor)
        return _PinnedStagedDatabase(
            path=Path(name),
            descriptor=descriptor,
            identity=(metadata.st_dev, metadata.st_ino),
        )
    except BaseException:
        os.close(descriptor)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
