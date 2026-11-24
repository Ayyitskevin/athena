"""Operator command-line entry points for Athena."""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, TextIO
from datetime import datetime, timezone

from athena import background
from athena.core import (
    activity_chain,
    attachments,
    db,
    deployment,
    maintenance_commands,
    token_commands,
    tokens,
    users,
)
from athena.core.backup import (
    backup_database,
    prune_backup_directory,
    restore_database,
    validate_retention_plan,
)
from athena.core.recovery import (
    RecoveryActivityChainVerification,
    SchemaMismatchError,
    RecoveryChainVerificationError,
    RecoveryPhaseResult,
    create_recovery_bundle,
    materialize_recovery_bundle,
    recovery_bundle_root_for_database,
    validate_current_database,
    verify_recovery_bundle,
)
from athena.core.portability import (
    ATTACHMENT_POLICIES,
    dry_run_import_database,
    export_database,
    import_manifest_database,
    write_import_manifest_database,
)
from athena.core.run_replay import export_run_replay_database
from athena.core.source_import import SOURCE_KINDS, write_source_bundle


def _printable(text: str) -> str:
    """``text`` with every character a terminal would act on escaped.

    Every line the operator CLIs print about a database goes through here: a
    path, a schema object name or a migration name read from the database can
    hold a newline or an escape sequence, and none may reach the terminal raw."""
    return "".join(
        character
        if character.isprintable()
        else character.encode("unicode_escape", "backslashreplace").decode("ascii")
        for character in text
    )


def _shell_word(text: str) -> str | None:
    """``text`` as one shell word, or None when it holds a character a terminal
    acts on: no quoting form is safe to paste into every shell (bash's
    ``$'...'`` runs embedded text under POSIX sh), so no command is printed."""
    if text.isprintable():
        return shlex.quote(text)
    return None


def _sqlite_uri(path: Path, *, mode: str) -> str:
    """Build an exact SQLite URI without treating path bytes as URI syntax."""
    if mode not in {"ro", "rw"}:
        raise ValueError("SQLite URI mode must be ro or rw")
    return f"{path.absolute().as_uri()}?mode={mode}"


def _refusal(exc: BaseException, message: str) -> BaseException:
    """``exc`` restated as one line naming its file, object and command (H12).

    The refusal keeps the type it was raised with, so every caller that catches
    it still does, and carries the original as ``athena_refusal_cause``: the
    doctor's receipt classifies that, not the remedy text."""
    try:
        refusal = type(exc)(message)
    except TypeError:
        refusal = ValueError(message)
    setattr(refusal, "athena_refusal_cause", exc)
    return refusal


#: The database path a documented remedy is rendered with (docs/OPERATIONS.md).
DOCUMENTED_PATH = Path("/var/lib/athena/athena.db")

#: The listener arguments ``athena-serve`` was started with (``--host``,
#: ``--port``), so a remedy that starts it again can carry them. None in the
#: doctor and for an inherited ``--fd`` listener: then no start command is
#: printed, since none would be certain to start the same listener.
_START_ARGUMENTS: list[str] | None = None

_RESTORE_RUNBOOK = 'docs/OPERATIONS.md "Restore a matched pair"'
_BOOTSTRAP_RUNBOOK = 'docs/OPERATIONS.md "First User Bootstrap"'
_PREFLIGHT_RUNBOOK = 'docs/OPERATIONS.md "Deploy Preflight"'
_TOKEN_EXPORT = (
    'export ATHENA_BOOTSTRAP_TOKEN="$(python3 -c '
    "'import secrets; print(secrets.token_urlsafe(32))')\""
)
_WRONG_PATH = (
    "ATHENA_DB is likely wrong: set it to the Athena database. Bootstrap does not "
    "take over a file that holds other data; for a new instance, choose a path in "
    f"a directory of its own and follow {_BOOTSTRAP_RUNBOOK}"
)
_RESTORE = (
    "{keep}, then restore a backup taken with this release as "
    f"{_RESTORE_RUNBOOK} describes"
)
_NEWER_RELEASE = "a newer Athena release migrated this database: run that release; or "


@dataclass(frozen=True, slots=True)
class DatabaseRefusal:
    """One kind of database refusal: how its line starts or what it says
    (``label``), and the remedy it prints (``remedy``). The remedy's
    placeholders are filled per state by ``render_remedy``: ``{keep}`` (stop
    Athena, then copy the database with its ``-wal`` and ``-shm`` files),
    ``{migrate}``, ``{bootstrap}`` and ``{normal_start}``. Each becomes a
    command only where it runs as printed and does what the line says;
    otherwise it is prose naming the runbook section."""

    label: str
    remedy: str
    prefix: bool = True


#: Every database refusal the operate lane prints, in the order they are
#: matched. ``docs/OPERATIONS.md`` lists every row (tested), and
#: ``tests/test_startup_refusals.py`` runs every command each prints.
DATABASE_REFUSALS: tuple[DatabaseRefusal, ...] = (
    DatabaseRefusal(
        "is a directory, not a database file",
        "set ATHENA_DB to the database file itself",
        prefix=False,
    ),
    DatabaseRefusal(
        "cannot be written",
        "as its owner, run `chmod u+w {db}` for the file or `chmod u+w {dir}` for "
        "its directory, whichever the line names",
        prefix=False,
    ),
    DatabaseRefusal(
        "cannot be created",
        "create it and the data directory as " + _BOOTSTRAP_RUNBOOK + " does, or "
        "set ATHENA_DB to the existing database",
        prefix=False,
    ),
    DatabaseRefusal(
        "is a symbolic link to",
        "create that directory, or point the link or ATHENA_DB at the database",
        prefix=False,
    ),
    DatabaseRefusal(
        "is a file, not a directory",
        "set ATHENA_DB to a path inside a directory",
        prefix=False,
    ),
    DatabaseRefusal(
        "database disk image is malformed",
        "the database file is damaged; " + _RESTORE,
    ),
    DatabaseRefusal("file is not a database", _WRONG_PATH),
    DatabaseRefusal(
        "failed",
        "it was not applied, and the database is unchanged by it; the schema has "
        "drifted from what the migration expects: {keep}, restore a backup taken "
        f"before the drift as {_RESTORE_RUNBOOK} describes, then apply migrations "
        f"as {_PREFLIGHT_RUNBOOK} shows",
        prefix=False,
    ),
    DatabaseRefusal(
        "database does not exist",
        "if ATHENA_DB is wrong, set it to the existing database; for a new "
        "instance at this path, {bootstrap}",
    ),
    DatabaseRefusal(
        "database is not migrated: schema_migrations is missing", _WRONG_PATH
    ),
    DatabaseRefusal(
        "database is missing migrations",
        "this Athena release has migrations the database has not applied; "
        "{keep}, then apply them: {migrate}",
    ),
    DatabaseRefusal(
        "schema_migrations checksum metadata is missing",
        "an older Athena wrote this migration ledger without checksums; {keep}, "
        "then let the migration runner add them: {migrate}",
    ),
    DatabaseRefusal("database has future migration", _NEWER_RELEASE + _RESTORE),
    DatabaseRefusal("applied migration is not packaged", _NEWER_RELEASE + _RESTORE),
    DatabaseRefusal(
        "database has unknown applied migration", _NEWER_RELEASE + _RESTORE
    ),
    DatabaseRefusal(
        "migration checksum mismatch",
        "the database applied different SQL under that name than this release "
        "ships: run the release that applied it; or " + _RESTORE,
    ),
    DatabaseRefusal(
        "is not part of this release's schema",
        "if you or a tool added it, {keep}, then remove it: Athena starts only on "
        "the schema its migrations make",
        prefix=False,
    ),
    DatabaseRefusal(
        "database schema does not match the packaged Athena schema",
        "the schema was changed outside Athena's migrations; " + _RESTORE,
    ),
    DatabaseRefusal(
        "database integrity check failed",
        "SQLite reports the file damaged; " + _RESTORE,
    ),
    DatabaseRefusal(
        "database foreign key check failed",
        "rows point at rows that do not exist; " + _RESTORE,
    ),
    DatabaseRefusal(
        "bootstrap startup requires a database with zero users",
        "this database already has users; {normal_start}",
    ),
    DatabaseRefusal(
        "bootstrap refuses an existing database that is not recognized as Athena",
        _WRONG_PATH,
    ),
    DatabaseRefusal(
        "bootstrap refuses an existing database without an applied Athena schema",
        _WRONG_PATH,
    ),
    DatabaseRefusal(
        "invalid migration inventory entry",
        "the installed Athena package's migration files are damaged; reinstall "
        "this Athena release from the wheel or checkout it came from",
    ),
    DatabaseRefusal(
        "duplicate migration sequence",
        "the installed Athena package's migration files are damaged; reinstall "
        "this Athena release from the wheel or checkout it came from",
    ),
    DatabaseRefusal(
        "migration inventory is missing",
        "the installed Athena package's migration files are damaged; reinstall "
        "this Athena release from the wheel or checkout it came from",
    ),
    DatabaseRefusal(
        "migration is not valid UTF-8",
        "the installed Athena package's migration files are damaged; reinstall "
        "this Athena release from the wheel or checkout it came from",
    ),
    DatabaseRefusal(
        "no Athena migrations were found",
        "the installed Athena package's migration files are damaged; reinstall "
        "this Athena release from the wheel or checkout it came from",
    ),
    # Every other migration-ledger refusal (an invalid, duplicate or
    # non-contiguous schema_migrations row): matched by its type.
    DatabaseRefusal(
        "schema_migrations",
        "the migration ledger is damaged; " + _RESTORE,
        prefix=False,
    ),
)
_BY_LABEL = {refusal.label: refusal for refusal in DATABASE_REFUSALS}

# SQLite's primary result codes.
_SQLITE_CORRUPT = 11
_SQLITE_READONLY = 8
_SQLITE_CANTOPEN = 14
_SQLITE_NOTADB = 26


class DatabaseNotWritable(PermissionError):
    """The start found the database or its directory not writable."""


def _keep_text(db_path: Path) -> str:
    """Stop Athena, then copy the database with whichever of its ``-wal`` and
    ``-shm`` files exist, into a new private directory beside it. ``mkdir``
    refuses an existing copy, so the command never overwrites one; where a
    copy from an earlier refusal is still there the command would stop at
    ``mkdir``, so the line names that copy and prints no command."""
    words = [_shell_word(f"{db_path}{suffix}") for suffix in ("", "-wal", "-shm")]
    copy = _shell_word(f"{db_path}.kept")
    if os.path.lexists(f"{db_path}.kept"):
        return (
            "stop Athena first; a copy kept at an earlier refusal is at "
            f"{db_path}.kept and is never overwritten: move it elsewhere, and "
            "this line will print the command that keeps a new copy of the "
            "database with its -wal and -shm files"
        )
    if copy is None or any(word is None for word in words):
        return (
            "stop Athena first, then keep a copy of the database file with its "
            "-wal and -shm files (its path holds a control character, so no "
            "command is printed)"
        )
    files = " ".join(word for word in words if word is not None)
    return (
        "stop Athena first, then keep a copy of the database with its -wal and "
        f"-shm files: `mkdir -m 700 {copy} && for f in {files}; do if [ -e "
        f'"$f" ]; then cp -p "$f" {copy}/; fi; done`'
    )


def _start_words() -> str | None:
    if _START_ARGUMENTS is None:
        return None
    return shlex.join(["athena-serve", *_START_ARGUMENTS])


def render_remedy(remedy: str, db_path: Path) -> str:
    """A remedy for ``db_path``, each command printed only where it runs as
    printed and does what the line says."""
    db_word = _shell_word(str(db_path))
    dir_word = _shell_word(str(db_path.parent))
    migrate = (
        f"`athena-doctor {db_word} --migrate`"
        if db_word is not None
        else f"athena-doctor --migrate, as {_PREFLIGHT_RUNBOOK} shows (the path "
        "holds a control character, so no command is printed)"
    )
    start = _start_words()
    if start is None:
        bootstrap = f"follow {_BOOTSTRAP_RUNBOOK}"
        normal_start = (
            "start athena-serve again with the same arguments, without --bootstrap "
            "and without ATHENA_BOOTSTRAP_TOKEN"
        )
    else:
        bootstrap = (
            f"in one shell run `{_TOKEN_EXPORT}`, then `{start} --bootstrap`, and "
            f"create the first user from that shell as {_BOOTSTRAP_RUNBOOK} shows"
        )
        normal_start = (
            "start it normally, without --bootstrap and without the bootstrap "
            f"token: `env -u ATHENA_BOOTSTRAP_TOKEN {start}`"
        )
    return remedy.format(
        db=db_word if db_word is not None else str(db_path),
        dir=dir_word if dir_word is not None else str(db_path.parent),
        keep=_keep_text(db_path),
        migrate=migrate,
        bootstrap=bootstrap,
        normal_start=normal_start,
    )


def _one_line(text: str) -> str:
    """The condition as raised. A newline it holds (a schema object or path
    name) stays in it, to be escaped where the line is printed."""
    return text.strip()


def _sqlite_code(exc: BaseException) -> int | None:
    code = getattr(exc, "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


def _read_only_mount(directory: Path) -> bool:
    try:
        return bool(os.statvfs(directory).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _not_writable(db_path: Path) -> str | None:
    """Why this process could not write the database, or None: what a write
    would meet (``os.access``), not what the mode bits say, so a root process
    that writes a 0400 file is not refused."""
    if _read_only_mount(db_path.parent):
        return "its filesystem is mounted read-only"
    if not os.access(db_path, os.W_OK):
        mode = stat.S_IMODE(db_path.stat().st_mode)
        return f"database file is not writable (mode {mode:04o})"
    if not os.access(db_path.parent, os.W_OK | os.X_OK):
        mode = stat.S_IMODE(db_path.parent.stat().st_mode)
        return f"data directory is not writable (mode {mode:04o})"
    return None


def _owner_command(path: Path, label: str) -> str:
    """``chmod u+w`` for ``path`` when this account may run it (it owns the
    path, or is root) and the path prints safely; otherwise the condition."""
    try:
        metadata = path.stat()
    except OSError:
        return f"{label} {path} cannot be inspected"
    mode = stat.S_IMODE(metadata.st_mode)
    word = _shell_word(str(path))
    if metadata.st_uid != os.geteuid() and os.geteuid() != 0:
        return (
            f"{label} {path} (mode {mode:04o}) belongs to another account, so no "
            "command is printed: give the account Athena runs as write access"
        )
    if word is None:
        return (
            f"{label} {path} is not writable (mode {mode:04o}); its path holds a "
            "control character, so no command is printed"
        )
    return f"{label} {path} is not writable (mode {mode:04o}): run `chmod u+w {word}`"


def _write_access_remedy(db_path: Path) -> str:
    """The remedy for a database that cannot be written. It names whichever of
    the file and its directory a write would fail on, and only that one, with
    ``chmod u+w`` (which adds the owner's write permission and narrows nothing)
    when this account may run it."""
    directory = db_path.parent
    base = (
        f"Athena writes the file and creates {db_path.name}-wal and "
        f"{db_path.name}-shm beside it; "
    )
    if _read_only_mount(directory):
        return (
            base + f"the filesystem holding {directory} is mounted read-only: "
            "remount it read-write"
        )
    shared = _shared_directory(directory)
    problems = []
    if not os.access(directory, os.W_OK | os.X_OK):
        if shared is not None:
            problems.append(
                f"its directory is not writable and {shared}: give the database a "
                "directory of its own that the account Athena runs as can write"
            )
        else:
            problems.append(_owner_command(directory, "its directory"))
    if db_path.exists() and not os.access(db_path, os.W_OK):
        problems.append(_owner_command(db_path, "the database file"))
    if not problems:
        return base + (
            "the file and its directory allow this account to write, so the cause "
            "is outside their modes (a read-only mount, an immutable attribute, a "
            "missing capability) and no command is printed; see docs/OPERATIONS.md "
            '"Startup Refusals"'
        )
    return base + "; ".join(problems)


def _refusal_line(exc: BaseException, db_path: Path) -> str | None:
    """The one line for ``exc``: its condition, the database, and the remedy;
    None when ``exc`` is not about the database."""
    condition = _one_line(str(exc))
    named = "" if str(db_path) in condition else f" (database {db_path})"
    code = _sqlite_code(exc)
    if db_path.is_dir() and (
        code == _SQLITE_CANTOPEN or isinstance(exc, IsADirectoryError)
    ):
        refusal = _BY_LABEL["is a directory, not a database file"]
        return (
            f"database {db_path} is a directory, not a database file; "
            + render_remedy(refusal.remedy, db_path)
        )
    if isinstance(exc, DatabaseNotWritable) or code in (
        _SQLITE_READONLY,
        _SQLITE_CANTOPEN,
    ):
        return (
            f"{condition}: database {db_path} cannot be written; "
            + _write_access_remedy(db_path)
        )
    if isinstance(exc, FileNotFoundError) and os.path.islink(db_path):
        refusal = _BY_LABEL["is a symbolic link to"]
        target = Path(os.path.realpath(db_path))
        return (
            f"database {db_path} is a symbolic link to {target}, whose directory "
            f"{target.parent} does not exist; " + render_remedy(refusal.remedy, db_path)
        )
    if isinstance(exc, FileNotFoundError) and exc.filename == str(db_path.parent):
        refusal = _BY_LABEL["cannot be created"]
        return (
            f"{db_path.parent.parent} does not exist, so the data directory "
            f"{db_path.parent} cannot be created; "
            + render_remedy(refusal.remedy, db_path)
        )
    if isinstance(exc, NotADirectoryError):
        refusal = _BY_LABEL["is a file, not a directory"]
        return (
            f"a part of {db_path} is a file, not a directory, so the database "
            "cannot be created there; " + render_remedy(refusal.remedy, db_path)
        )
    if code == _SQLITE_CORRUPT:
        refusal = _BY_LABEL["database disk image is malformed"]
        return f"{condition}{named}; " + render_remedy(refusal.remedy, db_path)
    if code == _SQLITE_NOTADB:
        refusal = _BY_LABEL["file is not a database"]
        return f"{condition}{named}; " + render_remedy(refusal.remedy, db_path)
    if isinstance(exc, SchemaMismatchError) and not (exc.missing or exc.changed):
        refusal = _BY_LABEL["is not part of this release's schema"]
        objects = ", ".join(exc.unexpected)
        verb = "is" if len(exc.unexpected) == 1 else "are"
        return (
            f"{condition}{named}; {objects} {verb} not part of this release's "
            "schema; " + render_remedy(refusal.remedy, db_path)
        )
    for refusal in DATABASE_REFUSALS:
        if refusal.prefix and condition.startswith(refusal.label):
            return f"{condition}{named}; " + render_remedy(refusal.remedy, db_path)
    if isinstance(exc, db.MigrationIntegrityError):
        refusal = _BY_LABEL["schema_migrations"]
        return f"{condition}{named}; " + render_remedy(refusal.remedy, db_path)
    return None


def _database_refusal(exc: BaseException, db_path: Path) -> BaseException | None:
    """The refusal ``exc`` restated with its file, object and remedy, or None
    when it is not about the database (it then propagates as it was)."""
    line = _refusal_line(exc, db_path)
    return None if line is None else _refusal(exc, line)


@contextlib.contextmanager
def _database_refusals(db_path: Path) -> Iterator[None]:
    """Re-raise a refusal about ``db_path`` with its file, object and remedy."""
    try:
        yield
    except (OSError, sqlite3.Error, ValueError) as exc:
        if hasattr(exc, "athena_refusal_cause"):
            raise  # already restated
        refusal = _database_refusal(exc, db_path)
        if refusal is None:
            raise
        raise refusal from exc


def _migrate_naming_failure(conn: sqlite3.Connection, db_path: Path) -> list[str]:
    """``db.migrate``, with a failing migration named by its file.

    The runner applies each migration and its ledger row in one transaction and
    rolls a failure back, so the first migration the ledger it leaves still has
    pending is the one that failed."""
    try:
        return db.migrate(conn)
    except sqlite3.Error as exc:
        try:
            pending = db.migration_status(conn, require_current=False).pending
        except (sqlite3.Error, ValueError):
            raise exc from None
        if not pending:
            raise
        refusal = _BY_LABEL["failed"]
        raise _refusal(
            exc,
            f"migration {pending[0]} failed: {_one_line(str(exc))} "
            f"(database {db_path}); " + render_remedy(refusal.remedy, db_path),
        ) from exc


def _dry_run_migration(db_path: Path) -> None:
    """Prove a migration and its final schema on a private copy first."""
    candidate = sqlite3.connect(":memory:")
    candidate.row_factory = sqlite3.Row
    try:
        if db_path.exists():
            source = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
            source.row_factory = sqlite3.Row
            try:
                source.backup(candidate)
            finally:
                source.close()
        candidate.execute("PRAGMA foreign_keys = ON")
        _migrate_naming_failure(candidate, db_path)
        validate_current_database(candidate)
    finally:
        candidate.close()


# A database Athena creates is private to the service user (H10): it holds
# password hashes, CSRF tokens and webhook secrets in the clear. SQLite gives its
# -wal and -shm files the database file's own permission bits.
DATA_DIRECTORY_MODE = 0o700
DATABASE_FILE_MODE = 0o600


def _create_private_database(db_path: Path) -> None:
    """Create a missing data directory 0700 and a missing database file 0600.

    Only on creation, and only the database's own directory (its parent must
    exist). An existing directory or file keeps its mode: changing what the
    operator set is theirs to do, and ``athena-doctor`` reports one that is
    wider. The modes are passed to ``mkdir``/``open``, so a process umask can
    only narrow them further."""
    directory = db_path.parent
    if not os.path.lexists(directory):
        directory.mkdir(mode=DATA_DIRECTORY_MODE)
    if os.path.islink(db_path) and not db_path.exists():
        # A dangling symlink: SQLite would follow it and create the target with
        # the umask's mode. Create the target private first.
        db_path = Path(os.path.realpath(db_path))
    elif os.path.lexists(db_path):
        return
    try:
        descriptor = os.open(
            db_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            DATABASE_FILE_MODE,
        )
    except FileExistsError:
        return
    os.close(descriptor)


def _shared_directory(directory: Path) -> str | None:
    """Why a ``chmod`` of a file in ``directory`` would not make it private, or
    None when the directory is the caller's own and not shared."""
    try:
        metadata = directory.stat()
    except OSError:
        return f"{directory} cannot be inspected"
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != os.geteuid():
        return f"{directory} belongs to another account"
    if mode & (stat.S_ISVTX | stat.S_IWOTH):
        return f"{directory} is shared (mode {mode:04o})"
    return None


def _athena_entries(database: Path, attach_dir: Path | None) -> set[str]:
    """The exact names Athena keeps beside its database: the database, its
    ``-wal``, ``-shm`` and ``-journal`` files, the copy a remedy keeps, the
    lock of the background loops, and the configured attachment directory when
    it lives there."""
    names = {database.name + suffix for suffix in ("", "-wal", "-shm", "-journal")}
    names.add(database.name + ".kept")
    names.add(database.name + background.LOCK_SUFFIX)
    if attach_dir is not None and attach_dir.absolute().parent == database.parent:
        names.add(attach_dir.name)
    return names


def _other_entries(directory: Path, database: Path, attach_dir: Path | None) -> bool:
    """Whether ``directory`` holds any entry but Athena's own (``lost+found``,
    a backups directory, another application's file): a ``chmod 700`` of it
    would then decide for them, so none is printed."""
    try:
        names = set(os.listdir(directory))
    except OSError:
        return True
    return bool(names - _athena_entries(database, attach_dir))


def _file_mode_warnings(db_path: Path, attach_dir: Path | None = None) -> list[str]:
    """One line per data file open to group or others.

    A ``chmod`` is printed only where running it as written is safe and does
    what the line says: for a regular file the caller owns, in a directory of
    the caller's own that is neither sticky nor world-writable, and that is not
    group- or world-writable itself (a service account may write it through its
    group); for that directory only when it holds nothing but Athena's entries
    and is not group- or world-writable either. A path holding a control
    character gets no command. Otherwise the line states the condition."""
    database = db_path.absolute()
    if database.is_dir():
        return [
            f"database path {database} is a directory, not a database file; name "
            "the database file itself"
        ]
    directory = database.parent
    holds = "the database holds password hashes, CSRF tokens and webhook secrets"
    warnings = []
    shared = _shared_directory(directory)
    try:
        mode = stat.S_IMODE(directory.stat().st_mode)
    except OSError:
        mode = 0
    if mode & 0o077:
        word = _shell_word(str(directory))
        if shared is not None:
            advice = f"{shared}: keep the database in a directory of its own"
        elif mode & 0o022:
            advice = (
                f"it is writable by its group or others (mode {mode:04o}), which an "
                "account Athena runs as may rely on, so no command is printed"
            )
        elif _other_entries(directory, database, attach_dir):
            advice = (
                f"{directory} holds other entries besides Athena's, so no command "
                "is printed for it"
            )
        elif word is None:
            advice = "its path holds a control character, so no command is printed"
        else:
            advice = f"run `chmod 700 {word}`"
        warnings.append(
            f"data directory {directory} is mode {mode:04o}, open to group or "
            f"others, and {holds}; {advice}"
        )
    for path in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        try:
            metadata = path.lstat()
        except OSError:
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if not mode & 0o077:
            continue
        word = _shell_word(str(path))
        if not stat.S_ISREG(metadata.st_mode):
            advice = f"{path} is not a regular file: replace it with the database file"
        elif shared is not None:
            advice = (
                f"its directory {shared}, so changing its mode would not keep it "
                "private: move it into a directory of its own"
            )
        elif mode & 0o022:
            advice = (
                f"it is writable by its group or others (mode {mode:04o}), which an "
                "account Athena runs as may rely on, so no command is printed"
            )
        elif metadata.st_uid != os.geteuid():
            advice = (
                "it belongs to another account, so no command is printed: run this "
                "check as the account Athena runs as"
            )
        elif word is None:
            advice = "its path holds a control character, so no command is printed"
        else:
            advice = f"run `chmod 600 {word}`"
        warnings.append(
            f"database file {path} is mode {mode:04o}, open to group or others, "
            f"and {holds}; {advice}"
        )
    return warnings


def _check_database(
    db_path: Path, *, migrate: bool, require_writable: bool = False
) -> str:
    with _database_refusals(db_path):
        return _check_database_state(
            db_path, migrate=migrate, require_writable=require_writable
        )


def _check_database_state(
    db_path: Path, *, migrate: bool, require_writable: bool
) -> str:
    if migrate:
        # db.migrate() is forward-only and commits each migration. Rehearse the
        # complete operation and exact packaged schema on an in-memory backup so
        # an incompatible-but-recognizable database fails before the real file
        # receives even the first pending migration.
        _dry_run_migration(db_path)
        _create_private_database(db_path)
        conn = db.connect(db_path)
        try:
            applied_now = _migrate_naming_failure(conn, db_path)
            status = validate_current_database(conn)
        finally:
            conn.close()
    else:
        if not db_path.exists():
            raise FileNotFoundError(f"database does not exist: {db_path}")
        if require_writable and db_path.is_file():
            # A start must be able to write: a read-only file or directory used
            # to pass preflight, answer /readyz, and fail every write with 500.
            problem = _not_writable(db_path)
            if problem is not None:
                raise DatabaseNotWritable(problem)
        conn = sqlite3.connect(_sqlite_uri(db_path, mode="rw"), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            status = validate_current_database(conn)
        finally:
            conn.close()
        applied_now = []

    action = (
        f"applied {len(applied_now)} migrations" if applied_now else "already current"
    )
    return (
        f"database: ok ({len(status.applied)} migrations, "
        f"latest {status.latest}, {action})"
    )


def _check_activity_chain(db_path: Path) -> str:
    """Walk the trail's hash chain (0072) and report the first break, if any.

    Read-only on purpose: a broken chain is a finding to investigate, never
    something doctor repairs. Bounded windows keep memory flat however long the
    trail is; the walk itself is the full verification an operator runs after a
    restore or before trusting a copied database file.
    """
    conn = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'activity_chain'"
        ).fetchone()
        if present is None:
            # A database that predates 0072 has nothing to verify — say so
            # rather than failing a check the schema cannot satisfy yet.
            return "activity chain: not present (database predates migration 0072)"
        report = activity_chain.verify_all(conn)
        chain_status = activity_chain.status(conn)
    finally:
        conn.close()

    if report["mismatch"] is not None:
        broken = report["mismatch"]
        raise ValueError(
            "activity chain verification failed: "
            f"event {broken['event_id']}: {broken['reason']} ({broken['detail']})"
        )
    head = chain_status["head"]
    if head is None:
        return (
            "activity chain: empty "
            f"({chain_status['unchained_count']} rows predate the chain)"
        )
    unchained = chain_status["unchained_count"]
    before = f"; {unchained} rows predate the chain" if unchained else ""
    return (
        f"activity chain: ok ({report['checked']} entries verified; "
        f"anchor event {chain_status['anchor_event_id']}; "
        f"head {head['entry_hash'][:12]}… at event {head['event_id']}{before})"
    )


def _check_attachment_dir(db_path: Path, attach_dir: Path) -> str:
    try:
        metadata = attach_dir.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"attachment directory does not exist: {attach_dir}"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"attachment path is not a directory: {attach_dir}")

    with tempfile.NamedTemporaryFile(
        dir=attach_dir,
        prefix=".athena-doctor-",
        delete=True,
    ) as probe:
        probe.write(b"ok")
        probe.flush()

    conn = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        blob_count = int(conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0])
        report = attachments.reconcile_storage(conn, attach_dir)
    finally:
        conn.close()

    if not report.ok:
        findings = {
            "missing": len(report.missing),
            "tampered": len(report.tampered),
            "size_mismatched": len(report.size_mismatched),
            "non_regular": len(report.non_regular),
            "unreadable": len(report.unreadable),
            "orphan_files": len(report.orphan_files),
        }
        detail = ", ".join(
            f"{name}={count}" for name, count in findings.items() if count
        )
        if report.storage_root_problem is not None:
            detail = (
                f"{detail}, " if detail else ""
            ) + f"storage_root={report.storage_root_problem}"
        raise ValueError(f"attachment integrity check failed: {detail}")

    return f"attachments: ok ({blob_count} blobs reconciled; {attach_dir})"


def _check_database_posture(
    db_path: Path,
    *,
    bootstrap: bool,
    enabled_oidc_issuer: str | None,
    max_request_body_bytes: int,
) -> str:
    conn = db.connect(db_path)
    try:
        with _database_refusals(db_path):
            return deployment.check_database_posture(
                conn,
                bootstrap=bootstrap,
                enabled_oidc_issuer=enabled_oidc_issuer,
                max_request_body_bytes=max_request_body_bytes,
            )
    finally:
        conn.close()


def _check_bootstrap_database_before_migration(db_path: Path) -> None:
    """Recognize an empty or existing Athena database without writing to it.

    A missing or structurally empty database is the normal bootstrap case. An
    existing database with objects must prove it is Athena through a valid
    immutable migration prefix and its stable users table. This prevents a path
    typo from migrating an unrelated SQLite database. The authoritative current
    schema and zero-user checks still run after migration.
    """
    if not db_path.exists():
        return
    conn = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        objects = conn.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        if not objects:
            return
        object_names = {str(row["name"]) for row in objects}
        if "schema_migrations" not in object_names:
            raise ValueError(
                "bootstrap refuses an existing database that is not recognized "
                "as Athena"
            )
        status = db.migration_status(conn, require_current=False)
        if not status.applied:
            # db.migrate() deliberately commits the canonical ledger before it
            # applies 0001. A power loss in that narrow window must remain
            # retryable, but only when the ledger is the database's sole
            # non-SQLite object and its schema passed migration_status().
            if object_names == {"schema_migrations"}:
                return
            raise ValueError(
                "bootstrap refuses an existing database without an applied "
                "Athena schema"
            )
        if "users" not in object_names:
            raise ValueError(
                "bootstrap refuses an existing database without an applied "
                "Athena schema"
            )
        if int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]) != 0:
            raise ValueError("bootstrap startup requires a database with zero users")
    finally:
        conn.close()


def _deployment_checks(
    db_path: Path,
    attach_dir: Path,
    *,
    bootstrap: bool,
    enabled_oidc_issuer: str | None,
    max_request_body_bytes: int,
    on_check: Callable[[str, str, Exception | None], None] | None = None,
    require_writable: bool = False,
) -> list[str]:
    if bootstrap:
        with _database_refusals(db_path):
            _check_bootstrap_database_before_migration(db_path)

    def run_check(name: str, action: Callable[[], str]) -> str:
        try:
            result = action()
        except (OSError, sqlite3.Error, ValueError) as exc:
            if on_check is not None:
                on_check(name, "failed", exc)
            raise
        if on_check is not None:
            on_check(name, "passed", None)
        return result

    return [
        run_check(
            "database",
            lambda: _check_database(
                db_path, migrate=bootstrap, require_writable=require_writable
            ),
        ),
        run_check("attachments", lambda: _check_attachment_dir(db_path, attach_dir)),
        run_check(
            "administrator-recovery",
            lambda: _check_database_posture(
                db_path,
                bootstrap=bootstrap,
                enabled_oidc_issuer=enabled_oidc_issuer,
                max_request_body_bytes=max_request_body_bytes,
            ),
        ),
    ]


def _warn_undeclared_variables(program: str) -> None:
    """Name, in one warning, every ``ATHENA_`` variable in the environment that
    no Athena setting declares: names only, since a value can be a credential."""
    try:
        from athena import config
    except ValueError:
        return  # a declared setting's value is invalid; that refusal comes first

    undeclared = config.undeclared_variables(os.environ)
    if not undeclared:
        return
    shown = ", ".join(
        name if name.isascii() and name.isprintable() else ascii(name)
        for name in undeclared
    )
    verb = (
        "is set but is not an Athena setting, so it has"
        if len(undeclared) == 1
        else "are set but are not Athena settings, so they have"
    )
    print(
        f"{program}: warning: {shown} {verb} no effect; check the spelling "
        "against the settings tables in docs/OPERATIONS.md",
        file=sys.stderr,
        flush=True,
    )


def _anonymous_reads_report(db_path: Path) -> str:
    """The doctor's line: the setting, the route count, the public containers."""
    from athena import config, exposure

    routes = exposure.ROUTE_COUNTS[config.ANONYMOUS_READS]
    conn = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        containers = exposure.public_containers(conn)
    finally:
        conn.close()
    return exposure.doctor_line(
        routes,
        containers,
        anonymous_reads=config.ANONYMOUS_READS,
        explicit=config.ANONYMOUS_READS_EXPLICIT,
    )


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _nonnegative_int_argument(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return result


def _recovery_point_age_seconds(capture_started_at: datetime) -> float:
    age = (datetime.now(timezone.utc) - capture_started_at).total_seconds()
    if age < 0:
        raise ValueError("recovery capture timestamp is in the future")
    return age


def _recovery_error_text(
    exc: BaseException,
    *,
    recovery_destination: Path | None = None,
) -> str:
    detail = str(exc) or ("interrupted" if isinstance(exc, KeyboardInterrupt) else "")
    published_path = getattr(exc, "recovery_published_path", None)
    if published_path is not None:
        return (
            f"{detail}; recovery artifact committed at {published_path}; "
            "preserve and verify it"
        )
    stage_path = getattr(exc, "recovery_stage_path", None)
    if stage_path is not None and "private stage retained at" not in detail:
        detail = (
            f"{detail}; private stage retained at {stage_path}; "
            "inspect it and remove it explicitly"
        )
    if (
        isinstance(exc, KeyboardInterrupt)
        and recovery_destination is not None
        and os.path.lexists(recovery_destination)
    ):
        detail = (
            f"{detail}; recovery destination exists at {recovery_destination}, but "
            "publication ownership and durability are unknown; preserve and verify "
            "it before retrying"
        )
    return detail


def _listener_address(fd: int) -> tuple[str, int]:
    if fd < 0:
        raise ValueError("listener file descriptor must be non-negative")
    try:
        duplicate = os.dup(fd)
    except OverflowError as exc:
        raise ValueError("listener file descriptor is out of range") from exc
    try:
        listener = socket.socket(fileno=duplicate)
    except BaseException:
        os.close(duplicate)
        raise
    try:
        if listener.family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError("listener file descriptor must be an IPv4 or IPv6 socket")
        if listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError("listener file descriptor must be a stream socket")
        if listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1:
            raise ValueError("listener file descriptor must already be listening")
        address = listener.getsockname()
    finally:
        listener.close()
    if (
        not isinstance(address, tuple)
        or len(address) < 2
        or not isinstance(address[0], str)
        or not isinstance(address[1], int)
    ):
        raise ValueError("listener file descriptor has no numeric IP address")
    return address[0], address[1]


def _answer_without_delay(fd: int) -> None:
    """Set ``TCP_NODELAY`` on an inherited listener, so every connection it
    accepts has it.

    A server that opens its own listener gets this from the event loop, which
    sets it on each TCP connection. uvicorn wraps an inherited descriptor as a
    UNIX-domain socket whatever its family, the loop then takes its
    connections for something other than TCP and leaves them alone, and each
    answer waits about 40 ms for the client's delayed acknowledgement of the
    response headers before the body is sent. A connection takes the option
    from its listener when it is accepted."""
    listener = socket.socket(fileno=os.dup(fd))
    try:
        listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    finally:
        listener.close()


def _not_an_app_error(record: logging.LogRecord) -> bool:
    return not record.getMessage().startswith("Exception in ASGI application")


def _serve_bound(application, **options) -> None:
    """Run ``application`` under uvicorn, starting its background loops only
    once the listener is bound (H13).

    uvicorn runs an app's startup before it binds. Loops started there also
    ran in a second ``athena-serve`` that was about to fail on the port, and
    delivered webhooks the first process delivered too."""
    import uvicorn
    from uvicorn.main import STARTUP_FAILURE

    class Server(uvicorn.Server):
        async def startup(self, sockets=None) -> None:
            # Returns only with the listener bound: a bind that fails exits
            # the process from inside, after the app's shutdown has run.
            await super().startup(sockets=sockets)
            await application.state.background.start()

    # Athena's request log has already logged an unhandled error, once and
    # with its request id (H15); the server's own copy of it is dropped.
    logging.getLogger("uvicorn.error").addFilter(_not_an_app_error)
    server = Server(uvicorn.Config(application, **options))
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl+C: the server has already stopped cleanly and raises the
        # signal again for its caller.
        pass
    if not server.started:
        sys.exit(STARTUP_FAILURE)


def _run_server(*, host: str, port: int, fd: int | None) -> None:
    from athena import config
    from athena.main import create_app

    application = create_app(
        network_mode=config.NETWORK_MODE,
        allowed_authorities=config.ALLOWED_AUTHORITIES,
        expected_server=(host, port),
        loops_after_bind=True,
    )
    options = {
        "workers": 1,
        "reload": False,
        "lifespan": "on",
        "proxy_headers": False,
        "server_header": False,
        # One line per request, and it is Athena's own (H15).
        "access_log": False,
        "limit_concurrency": 100,
        "timeout_graceful_shutdown": 30,
    }
    if fd is not None:
        _answer_without_delay(fd)
        _serve_bound(application, fd=fd, **options)
        return
    _serve_bound(application, host=host, port=port, **options)


def serve_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-serve",
        description="Start Athena through its supported fail-closed deployment gate.",
    )
    parser.add_argument(
        "--host",
        help="numeric bind address (defaults to 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port_argument,
        help="TCP port (defaults to 8000)",
    )
    parser.add_argument(
        "--fd",
        type=int,
        help="inherited listening IPv4/IPv6 socket descriptor",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="migrate an empty local database and enable first-admin bootstrap",
    )
    args = parser.parse_args(argv)
    if args.fd is not None and (args.host is not None or args.port is not None):
        parser.error("--fd cannot be combined with --host or --port")
    # What a remedy may carry to start the same listener again; none for --fd.
    global _START_ARGUMENTS
    _START_ARGUMENTS = None
    if args.fd is None:
        _START_ARGUMENTS = []
        if args.host is not None:
            _START_ARGUMENTS += ["--host", args.host]
        if args.port is not None:
            _START_ARGUMENTS += ["--port", str(args.port)]

    try:
        from athena import config

        # Named before any refusal of the start itself: a misspelling is often
        # why it is refused. (An invalid value of a declared setting is refused
        # while the settings load, before this can run.)
        _warn_undeclared_variables("athena-serve")

        if args.fd is None:
            bind_host, bind_port = deployment.validate_bind(
                args.host or "127.0.0.1",
                args.port or 8000,
                config.NETWORK_MODE,
            )
        else:
            inherited_host, inherited_port = _listener_address(args.fd)
            bind_host, bind_port = deployment.validate_bind(
                inherited_host,
                inherited_port,
                config.NETWORK_MODE,
            )
        authorities = deployment.validate_static_configuration(
            db_path=config.DB_PATH,
            attach_dir=config.ATTACH_DIR,
            network_mode=config.NETWORK_MODE,
            bind_host=bind_host,
            bind_port=bind_port,
            allowed_authorities=config.ALLOWED_AUTHORITIES,
            bootstrap=args.bootstrap,
            bootstrap_token=config.BOOTSTRAP_TOKEN,
            trust_actor_header=config.TRUST_ACTOR_HEADER,
            cookie_secure=config.COOKIE_SECURE,
            oidc_issuer=config.OIDC_ISSUER if config.oidc_enabled() else None,
            oidc_redirect_url=(
                config.OIDC_REDIRECT_URL if config.oidc_enabled() else None
            ),
            max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
            token_rate_limit_per_minute=config.TOKEN_RATE_LIMIT_PER_MINUTE,
            anon_rate_limit_per_minute=config.ANON_RATE_LIMIT_PER_MINUTE,
            login_rate_limit_per_minute=config.LOGIN_RATE_LIMIT_PER_MINUTE,
            login_account_rate_limit_per_minute=(
                config.LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE
            ),
            anonymous_reads_explicit=config.ANONYMOUS_READS_EXPLICIT,
        )
        checks = _deployment_checks(
            config.DB_PATH,
            config.ATTACH_DIR,
            bootstrap=args.bootstrap,
            enabled_oidc_issuer=(config.OIDC_ISSUER if config.oidc_enabled() else None),
            max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
            # A start must write; the doctor's caller may not be the service
            # account, so the doctor only warns (doctor_main).
            require_writable=not args.bootstrap,
        )
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(_printable(f"athena-serve: {exc}"), file=sys.stderr)
        return 1

    # The route layer reads these process-local bootstrap invariants. _run_server
    # then constructs a fresh app from the normalized policy and pins it to the
    # exact preflighted listener before handing the object to Uvicorn.
    config.ALLOWED_AUTHORITIES = tuple(authority.render() for authority in authorities)
    config.BOOTSTRAP_PASSWORD_REQUIRED = args.bootstrap
    config.SERVED_BY_ATHENA_SERVE = True
    for check in checks:
        print(_printable(check), flush=True)
    print(
        f"network: ok ({config.NETWORK_MODE}; {bind_host}:{bind_port}; "
        f"{len(authorities)} Host authorities)",
        flush=True,
    )
    if args.bootstrap:
        # The first start of an instance says, once, what anonymous reads are.
        from athena import exposure

        print(
            exposure.first_start_line(
                exposure.ROUTE_COUNTS[config.ANONYMOUS_READS],
                anonymous_reads=config.ANONYMOUS_READS,
                explicit=config.ANONYMOUS_READS_EXPLICIT,
            ),
            flush=True,
        )
    print("athena-serve: preflight ok", flush=True)
    _run_server(
        host=bind_host,
        port=bind_port,
        fd=args.fd,
    )
    return 0


def _validate_doctor_arguments(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.accept_broken_chain and not args.recovery_bundle:
        parser.error("--accept-broken-chain requires --recovery-bundle")
    if args.accept_broken_chain and args.drill_dir is None:
        parser.error("--accept-broken-chain requires --drill-dir")

    if args.recovery_bundle:
        if any(
            (
                args.attach_dir is not None,
                args.migrate,
                args.deployment,
                args.host is not None,
                args.port is not None,
            )
        ):
            parser.error(
                "--recovery-bundle cannot be combined with live database, "
                "attachment, migration, deployment, host, or port options"
            )
        if args.max_data_recovery_seconds is not None and args.drill_dir is None:
            parser.error("--max-data-recovery-seconds requires --drill-dir")
        return

    if any(
        (
            args.drill_dir is not None,
            args.max_recovery_point_age_seconds is not None,
            args.max_data_recovery_seconds is not None,
        )
    ):
        parser.error("recovery bundle options require --recovery-bundle")
    if not args.deployment and (args.host is not None or args.port is not None):
        parser.error("--host and --port require --deployment")
    if args.deployment and args.migrate:
        parser.error("--deployment cannot be combined with --migrate")
    if args.deployment and args.attach_dir is None:
        parser.error("--deployment requires --attach-dir")


@dataclass(slots=True)
class _DoctorCheck:
    name: str
    status: str
    reason: str


def _doctor_mode(args: argparse.Namespace) -> str:
    if args.recovery_bundle:
        return "recovery-bundle"
    if args.deployment:
        return "deployment"
    return "plain"


class _DoctorReceipt:
    def __init__(self, args: argparse.Namespace) -> None:
        self.mode = _doctor_mode(args)
        subject = Path(args.db_path).name or "subject"
        self.subject = subject if len(subject) <= 200 else subject[:197] + "..."
        self.checks: dict[str, _DoctorCheck] = {}
        self.accepted_broken_chain = False

        if self.mode == "plain":
            self._select("database", "database check did not complete")
            self._select("activity-chain", "activity-chain check did not complete")
            if args.attach_dir is not None:
                self._select("attachments", "attachment check did not complete")
            else:
                self._omit("attachments", "--attach-dir was not supplied")
            omitted = {
                "deployment-configuration": "--deployment was not selected",
                "network": "--deployment was not selected",
                "administrator-recovery": "--deployment was not selected",
                "recovery-bundle": "--recovery-bundle was not selected",
                "recovery-freshness": "recovery-bundle mode was not selected",
                "data-recovery-drill": "recovery-bundle mode was not selected",
                "data-recovery-duration": "recovery-bundle mode was not selected",
            }
        elif self.mode == "deployment":
            for name in (
                "deployment-configuration",
                "network",
                "database",
                "attachments",
                "administrator-recovery",
            ):
                self._select(name, f"{name} check did not complete")
            omitted = {
                "activity-chain": "deployment mode does not walk the full chain",
                "recovery-bundle": "--recovery-bundle was not selected",
                "recovery-freshness": "recovery-bundle mode was not selected",
                "data-recovery-drill": "recovery-bundle mode was not selected",
                "data-recovery-duration": "recovery-bundle mode was not selected",
            }
        else:
            for name in (
                "database",
                "activity-chain",
                "attachments",
                "recovery-bundle",
            ):
                self._select(name, f"{name} check did not complete")
            if args.max_recovery_point_age_seconds is not None:
                self._select(
                    "recovery-freshness", "recovery freshness check did not complete"
                )
            else:
                self._omit(
                    "recovery-freshness",
                    "--max-recovery-point-age-seconds was not supplied",
                )
            if args.drill_dir is not None:
                self._select(
                    "data-recovery-drill", "data-recovery drill did not complete"
                )
            else:
                self._omit("data-recovery-drill", "--drill-dir was not supplied")
            if args.max_data_recovery_seconds is not None:
                self._select(
                    "data-recovery-duration",
                    "data-recovery duration check did not complete",
                )
            else:
                self._omit(
                    "data-recovery-duration",
                    "--max-data-recovery-seconds was not supplied",
                )
            omitted = {
                "deployment-configuration": "--deployment was not selected",
                "network": "--deployment was not selected",
                "administrator-recovery": "--deployment was not selected",
            }
        for name, reason in omitted.items():
            self._omit(name, reason)

    def _select(self, name: str, reason: str) -> None:
        self._omit(name, reason)

    def _omit(self, name: str, reason: str) -> None:
        self.checks[name] = _DoctorCheck(name, "not evaluated", reason)

    def record(self, name: str, status: str, reason: str) -> None:
        bounded_reason = " ".join(reason.split())
        if len(bounded_reason) > 200:
            bounded_reason = bounded_reason[:197] + "..."
        self.checks[name] = _DoctorCheck(name, status, bounded_reason)


def _doctor_failure_reason(name: str, exc: Exception) -> str:
    if isinstance(exc, RecoveryChainVerificationError):
        chain = exc.verification
        anchor = "none" if chain.anchor_event_id is None else str(chain.anchor_event_id)
        through = (
            "none" if chain.through_event_id is None else str(chain.through_event_id)
        )
        event = (
            "unknown"
            if chain.mismatch_event_id is None
            else str(chain.mismatch_event_id)
        )
        reason = chain.mismatch_reason or "unknown_mismatch"
        return f"{reason} at event {event}; checked {chain.checked} entries from anchor {anchor} through {through}"

    cause = getattr(exc, "athena_refusal_cause", None)
    if isinstance(cause, Exception):
        # The receipt classifies the refusal as raised, not its remedy text.
        exc = cause
    message = str(exc).casefold()
    if message.startswith("database schema does not match"):
        return "schema validation failed"  # it names objects: before "migration"
    if name == "activity-chain":
        _, marker, remainder = message.partition("event ")
        if marker:
            event_id, separator, mismatch = remainder.partition(":")
            reason = mismatch.strip().split(maxsplit=1)[0].split("(", maxsplit=1)[0]
            if separator and event_id.isdigit() and reason.isidentifier():
                return f"{reason} at event {event_id}"
        return "full activity-chain verification failed"
    if "integrity check failed" in message:
        return "SQLite integrity check failed"
    if "foreign key" in message:
        return "foreign-key check failed"
    if "migration" in message:
        return "migration inventory or checksum validation failed"
    if "schema" in message:
        return "schema validation failed"
    if "attachment" in message or "blob" in message:
        return "attachment reconciliation failed"
    if "stale" in message or "age" in message:
        return "recovery-point age limit failed"
    if "time limit" in message or "exceeded its limit" in message:
        return "data-recovery drill time limit failed"
    if "database path" in message or "athena_db" in message:
        return "configured database path does not match"
    if (
        "attach_dir" in message
        or "attach-dir" in message
        or "athena_attach_dir" in message
        or "attachment path" in message
    ):
        return "configured attachment path does not match"
    if "network" in message or "host" in message or "port" in message:
        return "deployment network configuration failed"
    if "recovery bundle" in message:
        return "recovery-bundle validation failed"
    # Never a private class name in a receipt.
    return f"{name} check failed ({type(exc).__name__.lstrip('_')})"


def _run_doctor_check(
    report: _DoctorReceipt,
    name: str,
    passed_reason: str,
    action: Callable[[], str],
) -> str:
    try:
        result = action()
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        report.record(name, "failed", _doctor_failure_reason(name, exc))
        raise
    report.record(name, "passed", passed_reason)
    return result


def _recovery_chain_reason(
    chain: RecoveryActivityChainVerification,
) -> str:
    anchor = "none" if chain.anchor_event_id is None else str(chain.anchor_event_id)
    through = "none" if chain.through_event_id is None else str(chain.through_event_id)
    return (
        f"checked {chain.checked} entries from anchor {anchor} through {through}; "
        f"{chain.pre_adoption_count} pre-adoption rows excluded"
    )


def _recovery_doctor_checks(
    args: argparse.Namespace, report: _DoctorReceipt
) -> list[str]:
    recovery_started = time.monotonic()
    drill = None
    try:
        if args.drill_dir is None:
            bundle = verify_recovery_bundle(args.db_path)
        else:
            drill = materialize_recovery_bundle(
                args.db_path,
                args.drill_dir,
                accept_broken_chain=args.accept_broken_chain,
            )
            bundle = drill.source_bundle
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        phase_results: tuple[RecoveryPhaseResult, ...] = getattr(
            exc,
            "recovery_phase_results",
            getattr(exc, "phase_results", ()),
        )
        for phase in phase_results:
            report.record(phase.name, phase.status, phase.reason)
        failed = next(
            (phase.name for phase in phase_results if phase.status == "failed"),
            "bundle validation",
        )
        report.record(
            "recovery-bundle", "failed", f"bundle certification failed at {failed}"
        )
        raise

    phase_results = drill.phase_results if drill is not None else bundle.phase_results
    if not phase_results:
        phase_results = bundle.phase_results
    for phase in phase_results:
        reason = phase.reason
        if phase.name == "activity-chain":
            if phase.status == "passed":
                reason = _recovery_chain_reason(bundle.activity_chain)
            elif args.accept_broken_chain and drill is not None:
                reason += "; operator accepted for emergency drill"
        report.record(phase.name, phase.status, reason)
    if drill is not None and not any(
        phase.name == "data-recovery-drill" for phase in phase_results
    ):
        report.record(
            "data-recovery-drill", "passed", "private scratch copy materialized"
        )
    chain_broken = bundle.activity_chain.mismatch_reason is not None
    report.accepted_broken_chain = bool(
        args.accept_broken_chain and drill is not None and chain_broken
    )
    report.record(
        "recovery-bundle",
        "failed" if chain_broken else "passed",
        (
            "activity-chain mismatch explicitly accepted only for emergency drill"
            if report.accepted_broken_chain
            else "bundle checks completed"
            if chain_broken
            else "manifest, database, attachments and activity chain verified"
        ),
    )

    recovery_age = _recovery_point_age_seconds(bundle.capture_started_at)
    if args.max_recovery_point_age_seconds is not None:

        def check_recovery_age() -> str:
            if recovery_age > args.max_recovery_point_age_seconds:
                retained = f"; drill retained at {drill.path}" if drill else ""
                raise ValueError(
                    "recovery bundle is valid but stale: "
                    f"age {recovery_age:.3f}s exceeds "
                    f"{args.max_recovery_point_age_seconds}s{retained}"
                )
            return ""

        _run_doctor_check(
            report,
            "recovery-freshness",
            "capture age is within the configured limit",
            check_recovery_age,
        )

    if report.accepted_broken_chain:
        recovery_text = (
            "recovery bundle: activity-chain mismatch accepted for emergency drill"
        )
    else:
        recovery_text = (
            "recovery bundle: ok "
            f"({bundle.attachment_count} attachments, "
            f"{bundle.attachment_bytes} bytes; age {recovery_age:.3f}s; "
            "recorded Athena version "
            f"{json.dumps(bundle.athena_version, ensure_ascii=True)})"
        )
    checks = [recovery_text]
    if drill is None:
        return checks

    recovery_elapsed = time.monotonic() - recovery_started

    def check_drill_elapsed() -> str:
        if (
            args.max_data_recovery_seconds is not None
            and recovery_elapsed > args.max_data_recovery_seconds
        ):
            raise ValueError(
                "data-recovery drill completed but exceeded its limit: "
                f"{recovery_elapsed:.3f}s > "
                f"{args.max_data_recovery_seconds}s; retained at {drill.path}"
            )
        return ""

    if args.max_data_recovery_seconds is not None:
        _run_doctor_check(
            report,
            "data-recovery-duration",
            "materialization completed within the configured time limit",
            check_drill_elapsed,
        )
    checks.append(
        f"data-recovery drill: ok ({recovery_elapsed:.3f}s; retained at {drill.path})"
    )
    return checks


def _deployment_doctor_checks(
    args: argparse.Namespace, report: _DoctorReceipt
) -> list[str]:
    from athena import config

    assert args.attach_dir is not None

    def check_configuration() -> str:
        bind_host, bind_port = deployment.validate_bind(
            args.host or "127.0.0.1",
            args.port or 8000,
            config.NETWORK_MODE,
        )
        authorities = deployment.validate_static_configuration(
            db_path=args.db_path,
            attach_dir=args.attach_dir,
            network_mode=config.NETWORK_MODE,
            bind_host=bind_host,
            bind_port=bind_port,
            allowed_authorities=config.ALLOWED_AUTHORITIES,
            bootstrap=False,
            bootstrap_token=config.BOOTSTRAP_TOKEN,
            trust_actor_header=config.TRUST_ACTOR_HEADER,
            cookie_secure=config.COOKIE_SECURE,
            oidc_issuer=config.OIDC_ISSUER if config.oidc_enabled() else None,
            oidc_redirect_url=(
                config.OIDC_REDIRECT_URL if config.oidc_enabled() else None
            ),
            max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
            token_rate_limit_per_minute=config.TOKEN_RATE_LIMIT_PER_MINUTE,
            anon_rate_limit_per_minute=config.ANON_RATE_LIMIT_PER_MINUTE,
            login_rate_limit_per_minute=config.LOGIN_RATE_LIMIT_PER_MINUTE,
            login_account_rate_limit_per_minute=(
                config.LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE
            ),
            anonymous_reads_explicit=config.ANONYMOUS_READS_EXPLICIT,
        )
        report.record(
            "network", "passed", "network mode and listener binding validated"
        )
        if args.db_path != config.DB_PATH:
            raise ValueError("deployment db_path must exactly match ATHENA_DB")
        if args.attach_dir != config.ATTACH_DIR:
            raise ValueError(
                "deployment --attach-dir must exactly match ATHENA_ATTACH_DIR"
            )
        return f"{bind_host}|{bind_port}|{len(authorities)}"

    configuration = _run_doctor_check(
        report,
        "deployment-configuration",
        "configured paths, listener, Host policy and hardening matched",
        check_configuration,
    )
    bind_host, bind_port, authority_count = configuration.split("|")

    def observe_check(name: str, status: str, exc: Exception | None) -> None:
        if status == "passed":
            reasons = {
                "database": "integrity, foreign keys, migrations and schema passed",
                "attachments": "attachment directory reconciled with the database",
                "administrator-recovery": "active administrator recovery path passed",
            }
            report.record(name, status, reasons[name])
        else:
            assert exc is not None
            report.record(name, status, _doctor_failure_reason(name, exc))

    checks = _deployment_checks(
        args.db_path,
        args.attach_dir,
        bootstrap=False,
        enabled_oidc_issuer=(config.OIDC_ISSUER if config.oidc_enabled() else None),
        max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
        on_check=observe_check,
    )
    checks.append(
        f"network: ok ({config.NETWORK_MODE}; {bind_host}:{bind_port}; "
        f"{authority_count} Host authorities)"
    )
    return checks


def _doctor_checks(args: argparse.Namespace, report: _DoctorReceipt) -> list[str]:
    if args.recovery_bundle:
        return _recovery_doctor_checks(args, report)
    bundle_root = recovery_bundle_root_for_database(args.db_path)
    if bundle_root is not None:
        report.record(
            "database",
            "failed",
            "plain doctor refuses a recovery bundle database",
        )
        command = shlex.join(["athena-doctor", str(bundle_root), "--recovery-bundle"])
        raise ValueError(
            f"plain doctor refuses a recovery bundle database; run `{command}` instead"
        )
    if args.deployment:
        checks = _deployment_doctor_checks(args, report)
        checks.append(_anonymous_reads_report(args.db_path))
        return checks
    checks = [
        _run_doctor_check(
            report,
            "database",
            "integrity, foreign keys, migrations and schema passed",
            lambda: _check_database(args.db_path, migrate=args.migrate),
        ),
        _run_doctor_check(
            report,
            "activity-chain",
            "full bounded activity-chain walk passed",
            lambda: _check_activity_chain(args.db_path),
        ),
    ]
    if args.attach_dir is not None:
        checks.append(
            _run_doctor_check(
                report,
                "attachments",
                "attachment directory is writable and reconciles with the database",
                lambda: _check_attachment_dir(args.db_path, args.attach_dir),
            )
        )
    checks.append(_anonymous_reads_report(args.db_path))
    return checks


def _print_doctor_report(report: _DoctorReceipt, stream: TextIO) -> None:
    print(
        f"athena-doctor: mode={report.mode} subject={json.dumps(report.subject, ensure_ascii=True)}",
        file=stream,
    )
    for check in report.checks.values():
        print(
            _printable(
                f"athena-doctor: check {check.name}: {check.status} — {check.reason}"
            ),
            file=stream,
        )


def doctor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-doctor",
        description="Check Athena deployment prerequisites.",
    )
    parser.add_argument(
        "db_path",
        type=Path,
        help="Athena SQLite database path, or recovery bundle directory",
    )
    parser.add_argument(
        "--attach-dir",
        type=Path,
        help="attachment directory to verify for writable blob storage",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="create or migrate the database before checking it",
    )
    parser.add_argument(
        "--deployment",
        action="store_true",
        help="apply normal athena-serve deployment and recovery checks",
    )
    parser.add_argument(
        "--host",
        help="numeric deployment bind address (defaults to 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port_argument,
        help="deployment TCP port (defaults to 8000)",
    )
    parser.add_argument(
        "--recovery-bundle",
        action="store_true",
        help="strictly verify the positional recovery bundle without modifying it",
    )
    parser.add_argument(
        "--drill-dir",
        type=Path,
        help="materialize a verified bundle into this absent private scratch path",
    )
    parser.add_argument(
        "--accept-broken-chain",
        action="store_true",
        help=("accept an activity-chain mismatch only for an explicit recovery drill"),
    )
    parser.add_argument(
        "--max-recovery-point-age-seconds",
        type=_nonnegative_int_argument,
        metavar="N",
        help="fail when bundle capture start is more than N seconds old",
    )
    parser.add_argument(
        "--max-data-recovery-seconds",
        type=_nonnegative_int_argument,
        metavar="N",
        help="fail when verification plus scratch materialization exceeds N seconds",
    )
    args = parser.parse_args(argv)
    _validate_doctor_arguments(parser, args)

    report = _DoctorReceipt(args)
    global _START_ARGUMENTS
    _START_ARGUMENTS = None  # the doctor knows no listener to start again
    _warn_undeclared_variables("athena-doctor")
    if not args.recovery_bundle:
        attach_dir = args.attach_dir
        if attach_dir is None:
            try:
                from athena import config

                attach_dir = config.ATTACH_DIR
            except ValueError:
                attach_dir = None
        warnings = _file_mode_warnings(args.db_path, attach_dir)
        if args.db_path.is_file():
            # athena-serve refuses a database it cannot write; the doctor's
            # caller may be an operator account that only reads it.
            problem = _not_writable(args.db_path)
            if problem is not None:
                warnings.append(
                    f"database {args.db_path} is not writable by the account running "
                    f"this check ({problem}); run it as the account Athena runs as"
                )
        for warning in warnings:
            print(_printable(f"athena-doctor: warning: {warning}"), file=sys.stderr)
    try:
        checks = _doctor_checks(args, report)
    except KeyboardInterrupt as exc:
        report.record("doctor-execution", "failed", "interrupted by operator")
        _print_doctor_report(report, sys.stderr)
        detail = _recovery_error_text(
            exc,
            recovery_destination=args.drill_dir if args.recovery_bundle else None,
        )
        print(_printable(f"athena-doctor: {detail}"), file=sys.stderr)
        return 130
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        _print_doctor_report(report, sys.stderr)
        print(
            _printable(f"athena-doctor: {_recovery_error_text(exc)}"), file=sys.stderr
        )
        return 1

    for check in checks:
        print(_printable(check))
    _print_doctor_report(report, sys.stdout)
    if report.accepted_broken_chain:
        print("athena-doctor: completed with accepted broken activity chain")
    else:
        print("athena-doctor: ok")
    return 0


def _seat_doctor_checks(
    client: Any, *, expect_scopes: frozenset[str] | None
) -> list[str]:
    # ``client`` is an ``athena.mcp.client.AthenaClient``; the annotation is
    # ``Any`` because that module needs httpx, which the BASE install does not
    # carry — importing it at ops module level breaks ``athena-serve`` in a
    # server-only (container) install. The entrypoint imports it lazily.
    """Prove one seat's wiring end to end, or raise ValueError at the first
    broken link. Ordering is diagnostic: healthz before whoami means an auth
    failure is attributable to the token, not to an unreachable server; whoami
    before the desk means a desk failure is about the desk, not identity."""
    checks: list[str] = []

    client.healthz()
    checks.append("server: ok (/healthz answered)")

    # A paused or removed account is refused by the server itself at
    # /users/me (403 "account is paused" / 401 after removal revokes the
    # token), so those arrive as AthenaError with the reason in the detail —
    # the doctor translates rather than re-derives them.
    me = client.whoami()
    checks.append(
        "identity: ok "
        f"({me.get('name')!r}, role {me.get('role')}, "
        f"agent={bool(me.get('is_agent'))})"
    )

    scopes = me.get("scopes")
    if scopes is None:
        # A browser session or trusted-header path is not scope-limited. A SEAT
        # must be a bearer token: anything else cannot be rotated, revoked, or
        # budgeted per agent, so it fails the wiring check outright.
        raise ValueError(
            "authentication is not token-scoped — the seat must present a "
            "bearer token, not a session or actor header"
        )
    held = frozenset(scopes)
    if expect_scopes is not None and held != expect_scopes:
        raise ValueError(
            "token scopes do not match expectation: "
            f"held {sorted(held)}, expected {sorted(expect_scopes)} — "
            "narrower breaks the seat, wider is unreviewed authority"
        )
    checks.append(f"scopes: ok ({', '.join(sorted(held))})")

    budget = me.get("budget")
    gated = me.get("approval_required") or []
    checks.append(
        "limits: ok "
        f"(budget={'unlimited' if budget is None else budget}, "
        f"approval-gated kinds={sorted(gated) if gated else 'none'})"
    )

    desk = client.my_desk()
    for lane in ("identity", "asks", "work", "signals"):
        if lane not in desk:
            raise ValueError(f"desk answered without its {lane!r} lane")
    work = desk["work"]
    asks = desk["asks"]
    signals = desk["signals"]
    # Each lane is a bounded envelope ({"items": [...], ...}), so the counts
    # here are "what the desk showed", the same honesty the desk itself keeps.
    checks.append(
        "desk: ok "
        f"(delegations={len(work['delegations'].get('items') or [])}, "
        f"leases held={work['leases'].get('total', 0)}, "
        f"open asks={len(asks['run_controls'].get('items') or [])}, "
        f"unread={signals.get('unread_notifications', 0)})"
    )
    return checks


def seat_doctor_main(argv: list[str] | None = None) -> int:
    """Client-side sibling of athena-doctor: prove ONE SEAT's wiring — env,
    reachability, token, scopes, desk — with a re-runnable command instead of
    an investigation. Run it after onboarding, after any token rotation, and
    after a deploy move; docs/AGENT_BOOT.md names it as the wire-first proof."""
    # Lazy on purpose: the seat doctor is a CLIENT-side tool and needs httpx,
    # which ships with the [mcp] extra, not the base server install — see
    # docs/OPERATIONS.md. Base athena-serve must keep booting without it.
    import httpx

    from athena.mcp.client import AthenaClient, AthenaError

    parser = argparse.ArgumentParser(
        prog="athena-seat-doctor",
        description=(
            "Verify an agent seat's wiring against a running Athena: "
            "server reachable, bearer token live, scopes as expected, "
            "desk answering."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ATHENA_BASE_URL"),
        help="Athena base URL (default: $ATHENA_BASE_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ATHENA_TOKEN"),
        help="bearer token (default: $ATHENA_TOKEN; prefer the env variable "
        "so the secret stays out of shell history and process lists)",
    )
    parser.add_argument(
        "--expect-scopes",
        help="comma-separated exact scope set the token must hold "
        "(e.g. 'read,issue:write'); omitted = report without asserting",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-request timeout in seconds (default 10)",
    )
    args = parser.parse_args(argv)

    if not args.base_url:
        print(
            "athena-seat-doctor: no base URL — set ATHENA_BASE_URL or pass --base-url",
            file=sys.stderr,
        )
        return 1
    if not args.token:
        print(
            "athena-seat-doctor: no token — set ATHENA_TOKEN or pass --token "
            "(see docs/AGENT_BOOT.md for where a seat's token comes from)",
            file=sys.stderr,
        )
        return 1
    expect_scopes = (
        frozenset(
            part.strip() for part in args.expect_scopes.split(",") if part.strip()
        )
        if args.expect_scopes is not None
        else None
    )
    if args.expect_scopes is not None and not expect_scopes:
        print("athena-seat-doctor: --expect-scopes given but empty", file=sys.stderr)
        return 1

    client = AthenaClient(args.base_url, args.token, timeout=args.timeout)
    try:
        checks = _seat_doctor_checks(client, expect_scopes=expect_scopes)
    except AthenaError as exc:
        print(f"athena-seat-doctor: FAIL — {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(
            f"athena-seat-doctor: FAIL — cannot reach {args.base_url}: {exc}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"athena-seat-doctor: FAIL — {exc}", file=sys.stderr)
        return 1

    for check in checks:
        print(check)
    print("athena-seat-doctor: ok")
    return 0


def _validate_backup_arguments(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.recovery_bundle:
        if args.attach_dir is None:
            parser.error("--recovery-bundle requires --attach-dir")
        if args.overwrite or args.keep is not None or args.retention_glob is not None:
            parser.error(
                "--recovery-bundle cannot be combined with overwrite or retention"
            )
    elif args.attach_dir is not None:
        parser.error("--attach-dir requires --recovery-bundle")


def backup_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-backup",
        description="Back up an Athena SQLite database.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument("backup_path", type=Path, help="Destination backup path")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing backup path",
    )
    parser.add_argument(
        "--keep",
        type=int,
        metavar="N",
        help=(
            "after a successful backup, keep the newest N backups matching the "
            "retention glob in the destination directory"
        ),
    )
    parser.add_argument(
        "--retention-glob",
        help=("file-name glob used with --keep; defaults to <source-db-stem>-*.db"),
    )
    parser.add_argument(
        "--recovery-bundle",
        action="store_true",
        help="create a snapshot-bound database and attachment recovery bundle",
    )
    parser.add_argument(
        "--attach-dir",
        type=Path,
        help="live attachment directory required by --recovery-bundle",
    )
    args = parser.parse_args(argv)
    _validate_backup_arguments(parser, args)
    if args.keep is None and args.retention_glob is not None:
        print("athena-backup: --retention-glob requires --keep", file=sys.stderr)
        return 1
    retention_glob = args.retention_glob or f"{args.db_path.stem}-*.db"

    try:
        if args.recovery_bundle:
            assert args.attach_dir is not None
            recovery_bundle = create_recovery_bundle(
                args.db_path,
                args.attach_dir,
                args.backup_path,
            )
            backup = None
            pruned: list[Path] = []
        else:
            if args.keep is not None:
                validate_retention_plan(
                    args.backup_path,
                    retention_glob,
                    keep=args.keep,
                )
            backup = backup_database(
                args.db_path,
                args.backup_path,
                overwrite=args.overwrite,
            )
            pruned = []
            if args.keep is not None:
                pruned = prune_backup_directory(
                    backup.parent,
                    retention_glob,
                    keep=args.keep,
                    protected=(backup,),
                )
    except KeyboardInterrupt as exc:
        detail = _recovery_error_text(
            exc,
            recovery_destination=(args.backup_path if args.recovery_bundle else None),
        )
        print(_printable(f"athena-backup: {detail}"), file=sys.stderr)
        return 130
    except (
        FileNotFoundError,
        FileExistsError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(
            _printable(f"athena-backup: {_recovery_error_text(exc)}"), file=sys.stderr
        )
        return 1

    if args.recovery_bundle:
        assert recovery_bundle is not None
        if recovery_bundle.activity_chain.mismatch_reason is not None:
            event = recovery_bundle.activity_chain.mismatch_event_id
            event_label = "unknown" if event is None else str(event)
            reason = recovery_bundle.activity_chain.mismatch_reason
            follow_up = shlex.join(
                ["athena-doctor", str(recovery_bundle.path), "--recovery-bundle"]
            )
            print(
                _printable(
                    "athena-backup: warning: captured a bundle with activity-chain "
                    f"mismatch at event {event_label} ({reason}); follow up with: "
                    f"{follow_up}"
                ),
                file=sys.stderr,
            )
        print(
            _printable(
                "Created recovery bundle "
                f"at {recovery_bundle.path} "
                f"({recovery_bundle.attachment_count} attachments, "
                f"{recovery_bundle.attachment_bytes} bytes)"
            )
        )
    else:
        assert backup is not None
        print(_printable(f"Backed up {args.db_path} to {backup}"))
    if not args.recovery_bundle and args.keep is not None:
        print(
            "Pruned "
            f"{len(pruned)} old backup(s) matching {retention_glob!r}; "
            f"kept newest {args.keep}"
        )
    return 0


def export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-export",
        description="Export one Athena project or space to a portable JSON bundle.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument(
        "kind",
        choices=("project", "space"),
        help="container kind to export",
    )
    parser.add_argument("target_id", type=int, help="project or space id to export")
    parser.add_argument("bundle_path", type=Path, help="Destination JSON bundle path")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing bundle path",
    )
    args = parser.parse_args(argv)

    try:
        bundle = export_database(
            args.db_path,
            args.kind,
            args.target_id,
            args.bundle_path,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-export: {exc}", file=sys.stderr)
        return 1

    print(f"Exported {args.kind} {args.target_id} from {args.db_path} to {bundle}")
    return 0


def export_run_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-export-run",
        description="Export one tagged Athena run to a portable replay JSON artifact.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument("run_id", help="run id to export")
    parser.add_argument(
        "artifact_path", type=Path, help="Destination JSON artifact path"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing artifact path",
    )
    args = parser.parse_args(argv)

    try:
        artifact = export_run_replay_database(
            args.db_path,
            args.run_id,
            args.artifact_path,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-export-run: {exc}", file=sys.stderr)
        return 1

    print(f"Exported run {args.run_id} from {args.db_path} to {artifact}")
    return 0


def map_source_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-map-source",
        description="Map a supported source export into an Athena portability bundle.",
    )
    parser.add_argument("source", choices=SOURCE_KINDS, help="source export shape")
    parser.add_argument("source_path", type=Path, help="Source JSON export path")
    parser.add_argument("bundle_path", type=Path, help="Destination Athena bundle path")
    parser.add_argument("--project-key", help="override Jira project key")
    parser.add_argument("--project-name", help="override Jira project name")
    parser.add_argument("--space-key", help="override Confluence space key")
    parser.add_argument("--space-name", help="override Confluence space name")
    parser.add_argument(
        "--report-path",
        type=Path,
        help="optional destination for the source mapping report JSON",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing bundle path",
    )
    args = parser.parse_args(argv)

    try:
        bundle = write_source_bundle(
            args.source,
            args.source_path,
            args.bundle_path,
            project_key=args.project_key,
            project_name=args.project_name,
            space_key=args.space_key,
            space_name=args.space_name,
            report_path=args.report_path,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        ValueError,
    ) as exc:
        print(f"athena-map-source: {exc}", file=sys.stderr)
        return 1

    print(f"Mapped {args.source} export from {args.source_path} to {bundle}")
    if args.report_path is not None:
        print(f"Wrote source mapping report to {args.report_path}")
    return 0


def import_dry_run_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-import-dry-run",
        description="Validate an Athena portability bundle without importing it.",
    )
    parser.add_argument("db_path", type=Path, help="Target Athena SQLite database path")
    parser.add_argument("bundle_path", type=Path, help="Source JSON bundle path")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full dry-run report as JSON",
    )
    args = parser.parse_args(argv)

    try:
        report = dry_run_import_database(args.db_path, args.bundle_path)
    except (
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-import-dry-run: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_import_dry_run_report(report)
    return 0 if report["ok"] else 1


def _print_import_dry_run_report(report: dict) -> None:
    print(f"athena-import-dry-run: {report['status']}")
    print(f"bundle: {report['kind']} {report['root_id']} ({report['schema']})")
    _print_count_block("would create", report["would_create"])
    _print_count_block("would reuse", report["would_reuse"])
    if report["conflicts"]:
        print("conflicts:")
        for item in report["conflicts"]:
            print(f"  - {item['code']}: {item['message']}")
    if report["warnings"]:
        print("warnings:")
        for item in report["warnings"]:
            print(f"  - {item['code']}: {item['message']}")


def _print_count_block(title: str, counts: dict[str, int]) -> None:
    print(f"{title}:")
    if not counts:
        print("  none")
        return
    for key, value in counts.items():
        print(f"  {key}: {value}")


def import_manifest_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-import-manifest",
        description="Build a selective import replay manifest without importing it.",
    )
    parser.add_argument("db_path", type=Path, help="Target Athena SQLite database path")
    parser.add_argument("bundle_path", type=Path, help="Source JSON bundle path")
    parser.add_argument("manifest_path", type=Path, help="Destination manifest path")
    parser.add_argument(
        "--attachment-policy",
        choices=ATTACHMENT_POLICIES,
        default="skip",
        help="how a future replay should handle attachment manifests",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing manifest path",
    )
    args = parser.parse_args(argv)

    try:
        manifest = write_import_manifest_database(
            args.db_path,
            args.bundle_path,
            args.manifest_path,
            attachment_policy=args.attachment_policy,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-import-manifest: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote import manifest to {args.manifest_path}: {manifest['status']}")
    return 0 if manifest["ok"] else 1


def import_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-import",
        description="Replay a ready Athena selective import manifest.",
    )
    parser.add_argument("db_path", type=Path, help="Target Athena SQLite database path")
    parser.add_argument("bundle_path", type=Path, help="Source JSON bundle path")
    parser.add_argument("manifest_path", type=Path, help="Ready replay manifest path")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full import result as JSON",
    )
    args = parser.parse_args(argv)

    try:
        result = import_manifest_database(
            args.db_path,
            args.bundle_path,
            args.manifest_path,
        )
    except (
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-import: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_import_result(result)
    return 0


def _print_import_result(result: dict) -> None:
    print(f"athena-import: {result['status']}")
    print(f"bundle: {result['kind']} {result['root_id']} ({result['schema']})")
    _print_count_block("created", result["created"])
    _print_count_block("reused", result["reused"])
    _print_count_block("skipped", result["skipped"])
    if result["warnings"]:
        print("warnings:")
        for item in result["warnings"]:
            print(f"  - {item['code']}: {item['message']}")


def mint_host_token(db_path: Path, email: str, *, name: str, scopes: list[str]) -> dict:
    """Mint a token for the account with ``email`` in the database at
    ``db_path``, from the host. Returns the created token with its one-time
    raw secret.

    The database must be this release's (checked as ``athena-doctor`` checks
    it; nothing is migrated). The account must exist and be able to use a
    token: not paused. A removed account no longer has its email, so it is
    not found. The mint is the same audited command the
    browser form and the REST route call, with the host command named in the
    event, so the trail says a credential appeared and that it was handed out
    on the host, not asked for in a session."""
    _check_database(db_path, migrate=False, require_writable=True)
    conn = db.connect(db_path)
    try:
        account = users.get_user_by_email(conn, email)
        if account is None:
            raise ValueError(f"no account has the email {email}")
        if account.get("paused_at"):
            raise ValueError(
                f"the account {email} is paused; resume it before minting a token"
            )
        try:
            return token_commands.mint_token(
                conn,
                actor_id=int(account["id"]),
                name=name,
                scopes=scopes,
                via="athena-token on the host",
            )
        except token_commands.TokenCommandError as exc:
            raise ValueError(str(exc)) from exc
    finally:
        conn.close()


def token_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-token",
        description=(
            "Mint an API token for an existing account and print it once. For "
            "the first token of an instance, or an agent's, when no browser is "
            "at hand. Anyone who can run this can already read the database."
        ),
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument("email", help="email of the account the token is for")
    parser.add_argument(
        "--name", required=True, help="a label for the token, shown in its lists"
    )
    parser.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=tokens.SCOPES,
        help="a scope to grant; repeat for more. Give the narrowest that fits",
    )
    args = parser.parse_args(argv)

    try:
        created = mint_host_token(
            args.db_path, args.email, name=args.name, scopes=args.scope
        )
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"athena-token: {exc}", file=sys.stderr)
        return 1

    # The secret alone on stdout, so it can be captured; everything else on
    # stderr. It is stored only as a hash and cannot be shown again.
    print(
        f"Minted token {created['id']} ({created['name']}) for {args.email} with "
        f"scopes: {' '.join(created['scopes'])}. Shown once:",
        file=sys.stderr,
    )
    print(created["token"])
    return 0


def _days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        days = -1
    if days < 0:
        raise argparse.ArgumentTypeError("must be a whole number of days, 0 or more")
    return days


def maintain_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-maintain",
        description=(
            "Remove rows that have no further use: notifications read long ago, "
            "browser sessions past their expiry and idempotency receipts past "
            "theirs. Safe beside a running Athena. The activity trail is never "
            "pruned."
        ),
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument(
        "--read-notification-days",
        type=_days,
        default=maintenance_commands.READ_NOTIFICATION_DAYS,
        help=(
            "keep a read notification this many days after it was read "
            f"(default {maintenance_commands.READ_NOTIFICATION_DAYS})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be removed and remove nothing",
    )
    args = parser.parse_args(argv)

    try:
        _check_database(args.db_path, migrate=False, require_writable=not args.dry_run)
        conn = db.connect(args.db_path)
        try:
            counts = maintenance_commands.prune(
                conn,
                read_notification_days=args.read_notification_days,
                dry_run=args.dry_run,
            )
        finally:
            conn.close()
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"athena-maintain: {exc}", file=sys.stderr)
        return 1

    did = "would remove" if args.dry_run else "removed"
    print(
        f"{did} {counts['read_notifications']} notifications read more than "
        f"{args.read_notification_days} days ago"
    )
    print(f"{did} {counts['expired_sessions']} browser sessions past their expiry")
    print(
        f"{did} {counts['expired_idempotency_keys']} idempotency receipts past "
        "their expiry"
    )
    print("the activity trail is never pruned")
    return 0


def restore_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-restore",
        description="Restore an Athena SQLite database backup.",
    )
    parser.add_argument("backup_path", type=Path, help="Source backup path")
    parser.add_argument("db_path", type=Path, help="Target Athena database path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing target database",
    )
    args = parser.parse_args(argv)

    try:
        restored = restore_database(
            args.backup_path,
            args.db_path,
            force=args.force,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-restore: {exc}", file=sys.stderr)
        return 1

    print(f"Restored {args.backup_path} to {restored}")
    return 0


def field_guide_main(argv: list[str] | None = None) -> int:
    """Seed the Field Guide into an instance you keep.

    This belongs beside the other operator tools rather than inside
    ``athena-demo``: every command in this module operates on an EXISTING
    database, while the demo's whole contract is that it refuses one. Same
    seeding function underneath (``athena.guide``) — one implementation, two
    entry points, because "never a second seeder" is about the content, not
    about which CLI the operator happens to be holding.
    """
    # Imported HERE, not at module scope: importing this module must never pull in
    # athena.config, because config validates at import time and offline recovery
    # (athena-backup / athena-restore / athena-doctor) has to keep working on an
    # instance whose service configuration is broken — that is exactly when you
    # need it. `guide` reaches mentor, which reaches config, so a module-level
    # import would turn a bad ATHENA_NETWORK_MODE into a traceback from every
    # command in this file. Seeding the guide is not a recovery command and may
    # legitimately require a loadable app.
    from athena import guide

    parser = argparse.ArgumentParser(
        prog="athena-field-guide",
        description="Seed the agent Field Guide as pages in an existing workspace.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "report how the seeded guide compares to the content this install "
            "ships, and change nothing"
        ),
    )
    parser.add_argument(
        "--as",
        dest="author_email",
        help=(
            "email of the administrator to author the pages as "
            "(default: the earliest administrator)"
        ),
    )
    args = parser.parse_args(argv)

    if not args.db_path.exists():
        print(f"athena-field-guide: no such database: {args.db_path}", file=sys.stderr)
        return 1

    conn = db.connect(args.db_path)
    try:
        if args.check:
            return _print_field_guide_status(guide.guide_status(conn))
        author = _field_guide_author(conn, args.author_email)
        seeded = guide.seed_field_guide(conn, author_id=author["id"])
    except (guide.FieldGuideError, sqlite3.Error, ValueError) as exc:
        print(f"athena-field-guide: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # Attribution is printed, never silent: these pages now carry someone's name
    # on the trail, and the operator running the command should see whose.
    print(f"Field guide seeded into {args.db_path}")
    print(f"  Space:    {seeded['space']['key']} — {seeded['space']['name']}")
    print(f"  Pages:    {seeded['page_count']}")
    print(f"  Authored: {author['email']} (user {author['id']})")
    return 0


#: How each status reads to an operator. The wording carries the distinction the
#: report exists to make: "outdated" is about Athena's content, "edited" is about
#: theirs, and the two are never merged into one number.
_GUIDE_STATUS_LABELS = {
    "current": "current",
    "outdated": "outdated — Athena's guide changed since you seeded",
    "edited": "edited by you — left alone",
    "outdated_and_edited": "outdated AND edited by you — read both before changing",
    "missing": "not present — deleted, or added to the guide after you seeded",
}


def _print_field_guide_status(status: dict) -> int:
    """Render the comparison. Reports only: this command never rewrites a page.

    Returns 0 whether or not the guide is current — drift is information, not a
    failure, and an operator scripting this should not have to treat "your guide
    is a version behind" as an error.
    """
    space = status["space"]
    print(f"Field guide: {space['key']} — {space['name']}")
    print(f"  Athena version: {status['athena_version']}")
    for key, label in _GUIDE_STATUS_LABELS.items():
        count = status["counts"].get(key, 0)
        if count:
            print(f"  {count:>2}  {label}")
    drifted = [
        row
        for row in status["pages"]
        if row["status"] in ("outdated", "outdated_and_edited", "missing")
    ]
    if drifted:
        print()
        print("Pages where this install ships something different:")
        for row in drifted:
            print(f"  - {row['title']}  [{row['status']}]")
        print()
        print(
            "Nothing was changed. To take the shipped text for a page, read it in "
            "the wheel and edit the page yourself — these pages are yours now, and "
            "reseeding would overwrite work this command cannot see the value of."
        )
    else:
        print()
        print("Every page matches the content this install ships.")
    return 0


def _field_guide_author(conn: sqlite3.Connection, email: str | None) -> dict:
    """The administrator the guide's pages are authored as.

    Named explicitly with ``--as``, or the earliest administrator. Refuses rather
    than guessing when there is nobody to attribute to — an unattributed page is
    not a thing this workspace can hold, because every write is somebody's.
    """
    if email is not None:
        row = conn.execute(
            "SELECT id, email, role FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such user: {email}")
        if row["role"] != "admin":
            raise ValueError(f"{email} is not an administrator")
        return dict(row)
    row = conn.execute(
        "SELECT id, email, role FROM users "
        "WHERE role = 'admin' AND removed_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("this workspace has no administrator to author the guide as")
    return dict(row)
