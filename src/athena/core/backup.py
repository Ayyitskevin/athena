"""SQLite backup and restore helpers for Athena operators."""

from __future__ import annotations

from contextlib import closing
from fnmatch import fnmatch
from pathlib import Path
import sqlite3

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

    _copy_sqlite_database(source, destination)
    _remove_sqlite_sidecars(destination)
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

    Restores should be run while Athena is stopped. If ``force`` is true and the
    target exists, the main database file is replaced and stale WAL/shm sidecars
    for the target are removed.
    """
    source = Path(backup_path)
    target = Path(target_path)
    _require_existing_source(source)
    _require_distinct_paths(source, target)
    if target.exists() and not force:
        raise FileExistsError(f"target database already exists: {target}")

    _copy_sqlite_database(source, target)
    _remove_sqlite_sidecars(target)
    return target


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)
    temporary.unlink(missing_ok=True)

    try:
        with closing(_connect(source)) as source_conn:
            with closing(_connect(temporary)) as destination_conn:
                source_conn.backup(destination_conn)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp")


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
