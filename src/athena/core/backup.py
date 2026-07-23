"""SQLite backup and restore helpers for Athena operators."""

from __future__ import annotations

from contextlib import closing
from fnmatch import fnmatch
import os
from pathlib import Path
import sqlite3
import tempfile

_SIDECAR_SUFFIXES = ("-wal", "-shm")


def backup_database(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Copy an Athena SQLite database to ``backup_path``.

    Uses SQLite's online backup API, so a running Athena process can keep
    serving readers and writers while the snapshot is taken.
    """
    source = Path(source_path)
    destination = Path(backup_path)
    _require_existing_source(source)
    _require_distinct_paths(source, destination)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"backup path already exists: {destination}")

    staged = _stage_sqlite_database(source, destination)
    try:
        _remove_sqlite_sidecars(destination)
        _replace_staged_database(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    return destination


def validate_retention_plan(
    backup_path: str | Path,
    retention_glob: str,
    *,
    keep: int,
) -> None:
    """Validate backup-retention inputs before a snapshot is written."""
    _validate_retention_inputs(retention_glob, keep=keep)
    name = Path(backup_path).name
    if not fnmatch(name, retention_glob):
        raise ValueError(
            "backup path name must match retention glob: "
            f"{name!r} does not match {retention_glob!r}"
        )


def prune_backup_directory(
    directory_path: str | Path,
    retention_glob: str,
    *,
    keep: int,
    protected: tuple[str | Path, ...] = (),
) -> list[Path]:
    """Delete older backup files matching ``retention_glob`` in one directory.

    This intentionally only accepts a file-name glob, not a path glob. Operators
    choose the directory through the backup destination; retention then stays
    bounded to sibling backup files and cannot walk a broader tree.
    """
    _validate_retention_inputs(retention_glob, keep=keep)
    directory = Path(directory_path)
    if not directory.exists():
        raise FileNotFoundError(f"backup directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"backup path is not a directory: {directory}")

    protected_paths = {Path(path).resolve() for path in protected}
    candidates = [path for path in directory.glob(retention_glob) if path.is_file()]
    candidates.sort(
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    keepers = {path.resolve() for path in candidates[:keep]} | protected_paths
    pruned: list[Path] = []
    for candidate in candidates:
        if candidate.resolve() in keepers:
            continue
        candidate.unlink()
        pruned.append(candidate)
    return pruned


def restore_database(
    backup_path: str | Path,
    target_path: str | Path,
    *,
    force: bool = False,
) -> Path:
    """Restore ``backup_path`` into ``target_path``.

    Athena must be stopped for the entire restore. The backup is staged and
    integrity-checked before the target is touched. When ``force`` replaces an
    existing target, a consistent recovery snapshot is retained until the new
    database has been swapped into place durably.
    """
    source = Path(backup_path)
    target = Path(target_path)
    _require_existing_source(source)
    _require_distinct_paths(source, target)
    if target.exists() and not force:
        raise FileExistsError(f"target database already exists: {target}")

    staged = _stage_sqlite_database(source, target)
    recovery = _stage_sqlite_database(target, target) if target.exists() else None
    preserve_recovery = False
    try:
        _remove_sqlite_sidecars(target)
        try:
            _replace_staged_database(staged, target)
        except Exception:
            if recovery is None:
                raise
            try:
                _remove_sqlite_sidecars(target)
                _restore_recovery_database(recovery, target)
                recovery = None
            except Exception as recovery_error:
                preserve_recovery = True
                raise RuntimeError(
                    "restore failed and automatic recovery also failed; "
                    f"a consistent recovery copy remains at {recovery}"
                ) from recovery_error
            raise
    finally:
        staged.unlink(missing_ok=True)
        if recovery is not None and not preserve_recovery:
            recovery.unlink(missing_ok=True)
    return target


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _stage_sqlite_database(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)

    try:
        with closing(_connect(source)) as source_conn:
            with closing(_connect(temporary)) as destination_conn:
                source_conn.backup(destination_conn)
                result = [
                    row[0] for row in destination_conn.execute("PRAGMA quick_check")
                ]
                if result != ["ok"]:
                    raise sqlite3.DatabaseError(
                        "staged database failed SQLite quick_check"
                    )
        _fsync_file(temporary)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _temporary_path(path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    return Path(name)


def _replace_staged_database(staged: Path, destination: Path) -> None:
    staged.replace(destination)
    _fsync_directory(destination.parent)


def _restore_recovery_database(recovery: Path, destination: Path) -> None:
    staged = _stage_sqlite_database(recovery, destination)
    try:
        _replace_staged_database(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    recovery.unlink()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_existing_source(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"database path does not exist: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"database path is not a file: {path}")


def _require_distinct_paths(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("source and destination database paths must be different")


def _validate_retention_glob(retention_glob: str) -> None:
    if not retention_glob:
        raise ValueError("retention glob must not be empty")
    if (
        Path(retention_glob).is_absolute()
        or "/" in retention_glob
        or "\\" in retention_glob
    ):
        raise ValueError(
            "retention glob must be a file-name pattern without path separators"
        )


def _validate_retention_inputs(retention_glob: str, *, keep: int) -> None:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    _validate_retention_glob(retention_glob)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        path.with_name(f"{path.name}{suffix}").unlink(missing_ok=True)
