"""SQLite backup and restore helpers for Athena operators."""

from __future__ import annotations

from contextlib import closing
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


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        path.with_name(f"{path.name}{suffix}").unlink(missing_ok=True)
