"""Database access for Athena.

One SQLite file, opened in WAL mode, with a tiny forward-only migration runner.
Every other part of the app gets its connection from here, so the connection
settings (and the schema) live in exactly one place.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Iterator

# The .sql migration files live next to this module, in migrations/.
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME_RE = re.compile(r"^(?P<number>[0-9]{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")


class MigrationIntegrityError(ValueError):
    """The packaged migration history and database ledger cannot be reconciled."""


@dataclass(frozen=True)
class MigrationStatus:
    """A validated view of the packaged and applied migration histories."""

    required: tuple[str, ...]
    applied: tuple[str, ...]
    pending: tuple[str, ...]
    latest: str

    @property
    def current(self) -> bool:
        return not self.pending


@dataclass(frozen=True)
class _Migration:
    version: str
    number: int
    checksum: str
    sql: str


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the settings Athena always wants."""
    # FastAPI may enter a synchronous yield dependency and run its endpoint/exit
    # on different worker threads. A request still uses this connection serially,
    # but SQLite's default creator-thread affinity would turn that legitimate handoff
    # into a production-only 500 (TestClient does not reproduce the scheduler hop).
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # rows act like dicts: row["email"]
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    conn.execute("PRAGMA foreign_keys = ON")  # actually enforce REFERENCES
    # WAL lets readers and ONE writer coexist, but a second writer still needs the
    # write lock — and without a busy timeout SQLite gives up instantly with
    # "database is locked" (a 500) instead of waiting its turn. Wait up to 5s so
    # concurrent actors queue behind each other rather than erroring out.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """Run a group of writes as one atomic unit.

    Request handlers normally arrive with a fresh connection, so the outer form
    opens a real transaction and owns the final commit/rollback. A command may
    also be composed inside an existing transaction (imports and future compound
    commands need that), in which case a savepoint gives it the same all-or-nothing
    boundary without committing the caller's work.

    ``immediate`` acquires SQLite's single-writer reservation before any read used
    to validate the command. That closes the check-then-write race for commands
    whose authorization or audit detail depends on the current row. When composing
    inside an existing transaction, the caller owns its transaction mode; an outer
    writer must already have acquired the reservation if it needs that guarantee.

    Functions called inside this context must not commit independently; their
    transaction-aware variants receive ``commit=False`` from the command owner.
    """
    nested = conn.in_transaction
    savepoint = "athena_command"
    if nested:
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
    except BaseException:
        if nested and conn.in_transaction:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                # If finalizing destroyed/invalidated the savepoint, prefer a clean
                # connection over leaving the caller in a poisoned transaction.
                with suppress(sqlite3.Error):
                    conn.rollback()
        else:
            with suppress(sqlite3.Error):
                conn.rollback()
        raise


def _migration_inventory() -> tuple[_Migration, ...]:
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda path: path.name)
    if not paths:
        raise MigrationIntegrityError("no Athena migrations were found")

    inventory: list[_Migration] = []
    versions_by_number: dict[int, str] = {}
    for path in paths:
        match = _MIGRATION_NAME_RE.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            raise MigrationIntegrityError(
                f"invalid migration inventory entry: {path.name}"
            )
        number = int(match.group("number"))
        if number == 0:
            raise MigrationIntegrityError(
                f"invalid migration inventory entry: {path.name}"
            )
        duplicate = versions_by_number.get(number)
        if duplicate is not None:
            raise MigrationIntegrityError(
                f"duplicate migration sequence {number:04d}: {duplicate}, {path.name}"
            )
        payload = path.read_bytes()
        try:
            sql = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationIntegrityError(
                f"migration is not valid UTF-8: {path.name}"
            ) from exc
        versions_by_number[number] = path.name
        inventory.append(
            _Migration(
                version=path.name,
                number=number,
                checksum=hashlib.sha256(payload).hexdigest(),
                sql=sql,
            )
        )

    expected = list(range(1, inventory[-1].number + 1))
    actual = [migration.number for migration in inventory]
    if actual != expected:
        missing = [
            f"{number:04d}" for number in expected if number not in versions_by_number
        ]
        raise MigrationIntegrityError(
            f"migration inventory is missing sequence(s): {_format_versions(missing)}"
        )
    return tuple(inventory)


def _format_versions(versions: tuple[str, ...] | list[str]) -> str:
    shown = ", ".join(versions[:5])
    if len(versions) > 5:
        shown = f"{shown}, ... ({len(versions)} total)"
    return shown


def _migration_table_columns(
    conn: sqlite3.Connection,
) -> dict[str, sqlite3.Row]:
    rows = conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
    if not rows:
        raise MigrationIntegrityError(
            "database is not migrated: schema_migrations is missing"
        )
    columns = {str(row["name"]): row for row in rows}
    if set(columns) not in (
        {"version", "applied_at"},
        {"version", "checksum", "applied_at"},
    ):
        raise MigrationIntegrityError("schema_migrations has an invalid schema")
    if int(columns["version"]["pk"]) != 1:
        raise MigrationIntegrityError("schema_migrations version is not a primary key")
    if int(columns["applied_at"]["notnull"]) != 1:
        raise MigrationIntegrityError("schema_migrations applied_at must be non-null")
    for name in ("version", "applied_at", "checksum"):
        if name in columns and str(columns[name]["type"]).upper() != "TEXT":
            raise MigrationIntegrityError(
                f"schema_migrations {name} has an invalid type"
            )
    return columns


def _read_applied_versions(conn: sqlite3.Connection) -> list[str]:
    versions: list[str] = []
    for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"):
        version = row["version"]
        if not isinstance(version, str) or not version:
            raise MigrationIntegrityError(
                "schema_migrations contains an invalid version"
            )
        versions.append(version)
    return versions


def _unpackaged_version_error(
    version: str, inventory: tuple[_Migration, ...]
) -> MigrationIntegrityError:
    match = _MIGRATION_NAME_RE.fullmatch(version)
    if match is not None and int(match.group("number")) > inventory[-1].number:
        return MigrationIntegrityError(f"database has future migration: {version}")
    if match is not None:
        return MigrationIntegrityError(f"applied migration is not packaged: {version}")
    return MigrationIntegrityError(f"database has unknown applied migration: {version}")


def _validate_applied_versions(
    versions: list[str], inventory: tuple[_Migration, ...]
) -> dict[str, _Migration]:
    packaged = {migration.version: migration for migration in inventory}
    validated: dict[str, _Migration] = {}
    for version in versions:
        if version in validated:
            raise MigrationIntegrityError(
                f"schema_migrations contains duplicate version: {version}"
            )
        migration = packaged.get(version)
        if migration is None:
            raise _unpackaged_version_error(version, inventory)
        validated[version] = migration
    return validated


def _ensure_schema_migrations(
    conn: sqlite3.Connection, inventory: tuple[_Migration, ...]
) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY,"
        " checksum TEXT NOT NULL CHECK(length(checksum) = 64),"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()

    columns = _migration_table_columns(conn)
    if "checksum" in columns:
        return

    versions = _read_applied_versions(conn)
    validated = _validate_applied_versions(versions, inventory)
    with transaction(conn, immediate=True):
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")
        for version, migration in validated.items():
            updated = conn.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                (migration.checksum, version),
            )
            if updated.rowcount != 1:
                raise MigrationIntegrityError(
                    f"could not backfill migration checksum: {version}"
                )
        missing = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE checksum IS NULL"
        ).fetchone()[0]
        if missing:
            raise MigrationIntegrityError(
                "legacy migration checksum backfill was incomplete"
            )


def _migration_status(
    conn: sqlite3.Connection,
    inventory: tuple[_Migration, ...],
    *,
    require_current: bool,
) -> MigrationStatus:
    columns = _migration_table_columns(conn)
    if "checksum" not in columns:
        raise MigrationIntegrityError("schema_migrations checksum metadata is missing")

    versions: list[str] = []
    checksums: list[tuple[str, str]] = []
    for row in conn.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ):
        version = row["version"]
        checksum = row["checksum"]
        if not isinstance(version, str) or not version:
            raise MigrationIntegrityError(
                "schema_migrations contains an invalid version"
            )
        if not isinstance(checksum, str) or not checksum:
            raise MigrationIntegrityError(
                f"applied migration is missing its checksum: {version}"
            )
        versions.append(version)
        checksums.append((version, checksum))

    validated = _validate_applied_versions(versions, inventory)
    for version, checksum in checksums:
        if checksum != validated[version].checksum:
            raise MigrationIntegrityError(f"migration checksum mismatch: {version}")

    required = tuple(migration.version for migration in inventory)
    applied = tuple(
        migration.version for migration in inventory if migration.version in validated
    )
    if applied != required[: len(applied)]:
        raise MigrationIntegrityError(
            "schema_migrations is not a contiguous migration prefix"
        )
    pending = required[len(applied) :]
    if require_current and pending:
        raise MigrationIntegrityError(
            f"database is missing migrations: {_format_versions(pending)}"
        )
    return MigrationStatus(
        required=required,
        applied=applied,
        pending=pending,
        latest=inventory[-1].version,
    )


def migration_status(
    conn: sqlite3.Connection, *, require_current: bool = True
) -> MigrationStatus:
    """Validate migration inventory, ledger completeness, and applied checksums."""
    return _migration_status(
        conn, _migration_inventory(), require_current=require_current
    )


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every migration that hasn't run yet, in filename order.

    Returns the list of migrations applied this call (empty if already current).
    Safe to run on every startup: it only applies what's missing.
    """
    inventory = _migration_inventory()
    _ensure_schema_migrations(conn, inventory)
    status = _migration_status(conn, inventory, require_current=False)
    pending = set(status.pending)

    applied: list[str] = []
    for migration in inventory:
        if migration.version not in pending:
            continue
        # Apply the migration and its immutable ledger entry in one transaction.
        # executescript otherwise commits any earlier statements before a later
        # failure, leaving a half-applied schema with no trustworthy history.
        version_literal = migration.version.replace("'", "''")
        checksum_literal = migration.checksum.replace("'", "''")
        script = (
            "BEGIN;\n"
            + migration.sql
            + "\n;\n"
            + "INSERT INTO schema_migrations (version, checksum) VALUES "
            + f"('{version_literal}', '{checksum_literal}');\n"
            + "COMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            # A retry must see neither partial DDL nor a misleading ledger row.
            conn.rollback()
            raise
        applied.append(migration.version)

    migration_status(conn)
    return applied
