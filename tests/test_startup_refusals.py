"""Each database refusal at start names the file or object and what to do (H12).

WHY (OPS-4): a database whose schema differed from the packaged one refused to
start with "database schema does not match the packaged Athena schema" and named
no object; the migration-state refusals (missing, future, checksum mismatch,
read-only, a failing migration) stated the condition and not the remedy — for
missing migrations the remedy, ``athena-doctor <db> --migrate``, was nowhere —
and no document listed them.

One rule holds every remedy (two reviews of the first builds): a printed command
runs exactly as printed and does what its line says, in the state the line was
printed for; where that cannot be guaranteed, no command is printed, the line
states the condition and names the runbook section. ``test_every_printed_command
_runs_as_printed`` holds the rule by running every command each refusal prints,
under POSIX ``sh``, in a scratch copy of that state, and checking its effect.
So: a restore is never one bare step (stop Athena, keep a copy with its ``-wal``
and ``-shm`` files, then the runbook); a bootstrap is two commands run in the
operator's own shell, so that shell holds the token and can create the first
user; a start carries the listener arguments of the start that was refused; a
``chmod u+w`` names only what a write would fail on; a wrong ``ATHENA_DB`` is
named first and no path is invented; a path holding a control character gets no
command; and nothing a database or a path holds reaches the terminal unescaped.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import subprocess
import sys

import pytest

from athena import config, ops
from athena.core import db, users

REPO = Path(__file__).resolve().parent.parent
PACKAGED = sorted(path.name for path in db.MIGRATIONS_DIR.glob("*.sql"))
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
BIN = Path(sys.executable).parent


def _database(path: Path, *, admin: bool = True) -> Path:
    conn = db.connect(path)
    db.migrate(conn)
    if admin:
        users.create_user(
            conn,
            email="admin@example.com",
            name="Admin",
            password="password",
            role="admin",
            commit=True,
        )
    conn.close()
    return path


def _raw(path: Path, *statements: str) -> None:
    conn = sqlite3.connect(path)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def start(tmp_path, monkeypatch, capsys):
    """Run ``athena-serve [args]`` against ``<tmp>/data/athena.db``; return its
    one stderr line after checking that it refused and launched nothing."""
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    db_path = data / "athena.db"
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))

    def run(*argv: str, path: Path | None = None) -> str:
        monkeypatch.setattr(config, "DB_PATH", path or db_path)
        bootstrap = "--bootstrap" in argv
        monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "b" * 32 if bootstrap else "")
        capsys.readouterr()
        assert ops.serve_main(list(argv)) == 1
        assert launched == []
        err = capsys.readouterr().err
        assert err.endswith("\n") and err.count("\n") == 1, err
        assert err.startswith("athena-serve: ")
        assert not _CONTROL.search(err.rstrip("\n")), err
        return err.rstrip("\n")

    run.db_path = db_path  # type: ignore[attr-defined]
    run.attach_dir = attach_dir  # type: ignore[attr-defined]
    return run


def _q(path: Path) -> str:
    return shlex.quote(str(path))


def _commands(line: str) -> list[str]:
    return re.findall(r"`([^`]+)`", line)


def _sh(command: str, db_path: Path, attach_dir: Path, **extra: str):
    env = {
        "PATH": f"{BIN}:/usr/bin:/bin",
        "HOME": str(db_path.parent),
        "ATHENA_DB": str(db_path),
        "ATHENA_ATTACH_DIR": str(attach_dir),
        "ATHENA_WEBHOOK_DELIVERY": "0",
        "ATHENA_AUTOMATION": "0",
        **extra,
    }
    return subprocess.run(
        ["sh", "-c", command],
        cwd=db_path.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_keeps_a_copy(line: str, db_path: Path, attach_dir: Path) -> None:
    """The restore lead: stop Athena first, copy the database with its -wal
    and -shm files; the command runs and leaves the copy."""
    assert "--force" not in line and "athena-restore" not in line
    assert "stop Athena first" in line
    (keep,) = [c for c in _commands(line) if c.startswith("mkdir -m 700")]
    done = _sh(keep, db_path, attach_dir)
    assert done.returncode == 0, done.stderr
    kept = Path(f"{db_path}.kept")
    assert (kept / db_path.name).read_bytes() == db_path.read_bytes()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        assert (kept / sidecar.name).exists() == sidecar.exists()


def test_a_copy_kept_earlier_is_named_and_no_keep_command_is_printed(start):
    """The keep command stops at `mkdir` when an earlier copy is there, so it
    would not run as printed: the line names the copy and prints none, and
    prints the command again once the copy is moved."""
    launcher = start
    _state_unexpected(start.db_path, None, None)
    first = launcher()
    _assert_keeps_a_copy(first, launcher.db_path, launcher.attach_dir)
    kept = Path(f"{launcher.db_path}.kept")
    before = sorted(path.name for path in kept.iterdir())

    second = launcher()
    assert not [c for c in _commands(second) if c.startswith("mkdir")], second
    assert str(kept) in second and "never overwritten" in second
    assert "stop Athena first" in second
    assert sorted(path.name for path in kept.iterdir()) == before

    kept.rename(kept.with_name("moved-away"))
    third = launcher()
    assert [c for c in _commands(third) if c.startswith("mkdir -m 700")], third


def _integrity_failure(path: Path) -> None:
    """Pages no table owns: PRAGMA integrity_check reports them."""
    _raw(
        path,
        "CREATE TABLE pad (x)",
        "INSERT INTO pad SELECT hex(randomblob(200)) FROM "
        "(WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
        "WHERE i < 200) SELECT i FROM n)",
        "CREATE INDEX pad_x ON pad (x)",
    )
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("DELETE FROM sqlite_master WHERE name IN ('pad', 'pad_x')")
    conn.commit()
    conn.close()


# --- the rule: every printed command runs as printed -------------------------


def _state_missing_migrations(path, monkeypatch, through):
    source = db.MIGRATIONS_DIR
    through(PACKAGED[-3])
    _database(path)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", source)


def _state_legacy_ledger(path, monkeypatch, through):
    _database(path)
    _raw(path, "ALTER TABLE schema_migrations DROP COLUMN checksum")


def _state_future(path, monkeypatch, through):
    _database(path)
    _raw(
        path,
        "INSERT INTO schema_migrations (version, checksum) "
        f"VALUES ('9999_from_a_newer_release.sql', '{'0' * 64}')",
    )


def _state_checksum(path, monkeypatch, through):
    _database(path)
    _raw(
        path,
        f"UPDATE schema_migrations SET checksum = '{'f' * 64}' "
        f"WHERE version = '{PACKAGED[10]}'",
    )


def _state_drift(path, monkeypatch, through):
    _database(path)
    _raw(path, "DROP TABLE sessions")


def _state_unexpected(path, monkeypatch, through):
    _database(path)
    _raw(path, "CREATE INDEX operator_added_index ON users (name)")


def _state_integrity(path, monkeypatch, through):
    _database(path)
    _integrity_failure(path)


def _state_foreign_key(path, monkeypatch, through):
    _database(path)
    _raw(
        path,
        "INSERT INTO api_tokens (user_id, name, token_hash, scopes) "
        f"VALUES (999, 'orphan', '{'0' * 64}', 'admin')",
    )


def _state_malformed(path, monkeypatch, through):
    _database(path)
    data = bytearray(path.read_bytes())
    data[4096:8192] = b"\xff" * 4096
    path.write_bytes(bytes(data))


def _state_gap(path, monkeypatch, through):
    _database(path)
    _raw(path, f"DELETE FROM schema_migrations WHERE version = '{PACKAGED[2]}'")


STATES = {
    "missing migrations": (_state_missing_migrations, {"keep", "migrate"}),
    "ledger without checksums": (_state_legacy_ledger, {"keep", "migrate"}),
    "future migration": (_state_future, {"keep"}),
    "checksum mismatch": (_state_checksum, {"keep"}),
    "schema drift": (_state_drift, {"keep"}),
    "unexpected object": (_state_unexpected, {"keep"}),
    "integrity failure": (_state_integrity, {"keep"}),
    "foreign key failure": (_state_foreign_key, {"keep"}),
    "malformed file": (_state_malformed, {"keep"}),
    "ledger gap": (_state_gap, {"keep"}),
}


@pytest.mark.parametrize("state", sorted(STATES))
def test_every_printed_command_runs_as_printed(
    state, start, monkeypatch, migration_inventory_through
):
    build, expected = STATES[state]
    build(start.db_path, monkeypatch, migration_inventory_through)

    line = start()

    commands = _commands(line)
    kinds = {"keep" if c.startswith("mkdir -m 700") else "migrate" for c in commands}
    assert kinds == expected, line
    _assert_keeps_a_copy(line, start.db_path, start.attach_dir)
    for command in commands:
        if command.startswith("athena-doctor"):
            assert command == f"athena-doctor {_q(start.db_path)} --migrate"
            done = _sh(command, start.db_path, start.attach_dir)
            assert done.returncode == 0, done.stdout + done.stderr
            # What the line says it does: the database is current again.
            assert ops._check_database(start.db_path, migrate=False).startswith(
                "database: ok"
            )


def test_a_bootstrap_remedy_lets_the_operators_shell_create_the_first_user(
    start, tmp_path
):
    """The token is generated into the operator's shell, so that shell can
    present it; the start carries the refused start's listener arguments."""
    port = _free_port()

    line = start("--port", str(port))

    assert f"database does not exist: {start.db_path}" in line
    export, serve = _commands(line)
    assert export.startswith('export ATHENA_BOOTSTRAP_TOKEN="$(python3 -c ')
    assert serve == f"athena-serve --port {port} --bootstrap"
    script = """
eval "$EXPORT"
eval "$SERVE" > "$HOME/serve.log" 2>&1 &
pid=$!
n=0
until python3 -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1)" "$BASE/healthz" 2>/dev/null; do
  n=$((n + 1)); if [ $n -gt 300 ] || ! kill -0 $pid 2>/dev/null; then echo NOT-SERVING; exit 9; fi
  sleep 0.1
done
python3 - "$BASE/users" <<'PY'
import json, os, sys, urllib.error, urllib.request
body = json.dumps({"email": "a@example.com", "name": "A", "password": "Review-Passw0rd-long-enough!"})
request = urllib.request.Request(sys.argv[1], data=body.encode(), method="POST", headers={
    "Content-Type": "application/json",
    "X-Athena-Bootstrap-Token": os.environ.get("ATHENA_BOOTSTRAP_TOKEN", ""),
})
try:
    print("USERS", urllib.request.urlopen(request).status)
except urllib.error.HTTPError as exc:
    print("USERS", exc.code)
PY
# A background job ignores SIGINT in a non-interactive sh: stop it with TERM.
pkill -TERM -P $pid; kill -TERM $pid 2>/dev/null; wait $pid
"""
    done = _sh(
        script,
        start.db_path,
        start.attach_dir,
        EXPORT=export,
        SERVE=serve,
        BASE=f"http://127.0.0.1:{port}",
    )
    assert "USERS 201" in done.stdout, done.stdout + done.stderr
    conn = db.connect(start.db_path)
    assert users.count_users(conn) == 1
    conn.close()


def test_a_has_users_remedy_starts_the_same_listener_without_the_token(start):
    _database(start.db_path)
    port = _free_port()

    line = start("--bootstrap", "--port", str(port))

    assert "bootstrap startup requires a database with zero users" in line
    (command,) = _commands(line)
    assert command == f"env -u ATHENA_BOOTSTRAP_TOKEN athena-serve --port {port}"
    script = """
eval "$SERVE" > "$HOME/serve.log" 2>&1 &
pid=$!
n=0
until python3 -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1)" "$BASE/healthz" 2>/dev/null; do
  n=$((n + 1)); if [ $n -gt 300 ] || ! kill -0 $pid 2>/dev/null; then echo NOT-SERVING; cat "$HOME/serve.log"; exit 9; fi
  sleep 0.1
done
echo SERVING
# A background job ignores SIGINT in a non-interactive sh: stop it with TERM.
pkill -TERM -P $pid; kill -TERM $pid 2>/dev/null; wait $pid
"""
    done = _sh(
        script,
        start.db_path,
        start.attach_dir,
        SERVE=command,
        BASE=f"http://127.0.0.1:{port}",
        ATHENA_BOOTSTRAP_TOKEN="b" * 40,  # still set in the shell that was refused
    )
    assert "SERVING" in done.stdout, done.stdout + done.stderr


def test_an_inherited_listener_gets_no_start_command(start):
    _database(start.db_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        line = start("--bootstrap", "--fd", str(listener.fileno()))
    assert "`" not in line
    assert "start athena-serve again with the same arguments" in line


# --- each state's line --------------------------------------------------------


def test_missing_migrations_name_them(start, monkeypatch, migration_inventory_through):
    _state_missing_migrations(start.db_path, monkeypatch, migration_inventory_through)

    line = start()

    assert f"database is missing migrations: {PACKAGED[-2]}, {PACKAGED[-1]}" in line
    assert f"database {start.db_path}" in line


def test_a_future_migration_names_it_and_the_runbook(start, monkeypatch):
    _state_future(start.db_path, monkeypatch, None)

    line = start()

    assert "database has future migration: 9999_from_a_newer_release.sql" in line
    assert "newer Athena release" in line
    assert 'docs/OPERATIONS.md "Restore a matched pair"' in line


def test_a_schema_difference_names_each_object(start):
    _database(start.db_path)
    _raw(
        start.db_path,
        "DROP TABLE sessions",
        "CREATE INDEX operator_added_index ON users (name)",
    )

    line = start()

    assert "missing table sessions" in line
    assert "unexpected index operator_added_index" in line
    assert "Restore a matched pair" in line


def test_an_unexpected_object_alone_is_named_not_answered_with_a_restore(start):
    _state_unexpected(start.db_path, None, None)

    line = start()

    assert "index operator_added_index is not part of this release's schema" in line
    assert "Restore a matched pair" not in line


def test_a_schema_object_name_cannot_write_to_the_terminal(start):
    _database(start.db_path)
    _raw(start.db_path, 'CREATE INDEX "evil\x1b[2J\nname" ON users (name)')

    line = start()  # the fixture refuses any control character

    assert "unexpected index evil\\x1b[2J\\nname" in line


def test_a_path_with_a_control_character_gets_no_command(tmp_path, start):
    """bash's $'...' form runs embedded text when pasted into POSIX sh, and no
    other quoting is safe in every shell, so such a path gets no command."""
    data = tmp_path / "odd\ndir"
    data.mkdir(mode=0o700)
    db_path = data / "athena.db"
    _database(db_path)
    _raw(db_path, "DROP TABLE sessions")

    line = start(path=db_path)

    assert "odd\\ndir" in line
    assert "`" not in line
    assert "control character, so no command is printed" in line


def test_a_root_run_instance_that_main_started_still_starts(tmp_path, monkeypatch):
    """No instance that started on main refuses now (D7's tailnet rule aside):
    a root-run database or directory with its owner-write bit cleared is
    written by root all the same, so only what a write meets refuses."""
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))
    modes = [
        (0o600, 0o700),
        (0o400, 0o700),
        (0o600, 0o500),
        (0o444, 0o555),
        (0o664, 0o775),
        (0o644, 0o1777),
    ]
    for file_mode, dir_mode in modes:
        data = tmp_path / f"data-{file_mode:o}-{dir_mode:o}"
        data.mkdir()
        db_path = _database(data / "athena.db")
        os.chmod(db_path, file_mode)
        os.chmod(data, dir_mode)
        monkeypatch.setattr(config, "DB_PATH", db_path)
        writable = os.access(db_path, os.W_OK) and os.access(data, os.W_OK | os.X_OK)
        assert (ops.serve_main([]) == 0) is writable, (file_mode, dir_mode)
        os.chmod(data, 0o700)
    if os.geteuid() == 0:
        assert len(launched) == len(modes)


def _not_writable(monkeypatch, *paths: Path) -> None:
    """Make ``os.access`` answer as it does for an account a write would fail
    for (an operator account, or root without CAP_DAC_OVERRIDE)."""
    real = os.access
    blocked = {str(path) for path in paths}

    def access(path, mode, *args, **kwargs):
        if str(path) in blocked and mode & os.W_OK:
            return False
        return real(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", access)


def test_a_file_a_write_would_fail_on_is_refused_with_chmod_for_the_file(
    start, monkeypatch
):
    _database(start.db_path)
    os.chmod(start.db_path, 0o400)
    _not_writable(monkeypatch, start.db_path)

    line = start()

    assert f"database {start.db_path} cannot be written" in line
    (command,) = _commands(line)
    assert command == f"chmod u+w {_q(start.db_path)}"
    assert _sh(command, start.db_path, start.attach_dir).returncode == 0
    assert os.stat(start.db_path).st_mode & 0o200


def test_a_directory_a_write_would_fail_on_names_only_the_directory(start, monkeypatch):
    """A 0500 directory with a 0600 file: the line names the directory and
    prints nothing about the file, which is already writable."""
    _database(start.db_path)
    os.chmod(start.db_path, 0o600)
    os.chmod(start.db_path.parent, 0o500)
    _not_writable(monkeypatch, start.db_path.parent)

    line = start()

    (command,) = _commands(line)
    assert command == f"chmod u+w {_q(start.db_path.parent)}"
    assert "database file" not in line.split("cannot be written", 1)[1]
    assert _sh(command, start.db_path, start.attach_dir).returncode == 0
    assert os.stat(start.db_path.parent).st_mode & 0o200


def test_a_shared_directory_gets_no_command(tmp_path, start, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)
    db_path = _database(shared / "athena.db")
    _not_writable(monkeypatch, shared)

    line = start(path=db_path)

    assert "cannot be written" in line and f"{shared} is shared" in line
    assert "`" not in line


def test_a_write_failure_outside_the_modes_gets_no_command(start, monkeypatch):
    """SQLite refuses although the modes allow this account to write (here a
    read-only open stands in for a mount, an immutable file or a missing
    capability): no chmod would help, so none is printed."""
    _database(start.db_path)
    _raw(start.db_path, "PRAGMA journal_mode = DELETE")
    real_uri = ops._sqlite_uri
    monkeypatch.setattr(
        ops, "_sqlite_uri", lambda path, *, mode: real_uri(path, mode="ro")
    )

    line = start()

    assert "attempt to write a readonly database" in line
    assert "cause is outside their modes" in line and "`" not in line


def test_a_database_path_that_names_a_directory_says_so(tmp_path, start):
    directory = tmp_path / "lib"
    (directory / "athena").mkdir(parents=True)

    line = start(path=directory / "athena")

    assert "is a directory, not a database file" in line
    assert "`" not in line


@pytest.mark.parametrize("bootstrap", [False, True])
def test_a_foreign_file_leads_with_a_wrong_athena_db_and_invents_no_path(
    start, bootstrap
):
    _raw(start.db_path, "CREATE TABLE dashboard (id INTEGER PRIMARY KEY)")

    line = start(*(["--bootstrap"] if bootstrap else []))

    remedy = line.split("; ", 1)[1]
    assert remedy.startswith("ATHENA_DB is likely wrong: set it to the Athena database")
    assert "`" not in line and "-new.db" not in line


def test_a_file_that_is_not_a_database_leads_with_a_wrong_athena_db(start):
    start.db_path.write_text("these are not the pages you are looking for\n" * 20)

    line = start()

    assert "file is not a database" in line
    assert "ATHENA_DB is likely wrong" in line and "`" not in line


def test_bootstrap_under_a_missing_ancestor_names_no_command(tmp_path, start):
    db_path = tmp_path / "missing" / "data" / "athena.db"

    line = start("--bootstrap", path=db_path)

    assert f"{tmp_path / 'missing'} does not exist" in line
    assert "`" not in line and "First User Bootstrap" in line


def test_bootstrap_under_a_file_says_it_is_not_a_directory(tmp_path, start):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")

    line = start("--bootstrap", path=blocker / "data" / "athena.db")

    assert "not a directory" in line and "`" not in line


def test_a_dangling_symlink_into_a_missing_directory_has_its_own_line(tmp_path, start):
    link = tmp_path / "data" / "athena.db"
    link.symlink_to(tmp_path / "gone" / "athena.db")

    line = start("--bootstrap", path=link)

    assert f"is a symbolic link to {tmp_path / 'gone' / 'athena.db'}" in line
    assert "[Errno" not in line and "`" not in line


def test_a_failing_migration_names_its_file_and_prints_only_the_copy(
    tmp_path, monkeypatch, capsys
):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_first.sql").write_text("CREATE TABLE first (id INTEGER);")
    (migrations / "0002_second.sql").write_text("CREATE TABLE second (id INTEGER);")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    db_path = data / "athena.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.close()
    (migrations / "0003_third.sql").write_text("CREATE TABLE third (id INTEGER);")
    _raw(db_path, "CREATE TABLE third (id INTEGER)")

    assert ops.doctor_main([str(db_path), "--migrate"]) == 1

    err = capsys.readouterr().err.splitlines()[-1]
    assert err.startswith("athena-doctor: migration 0003_third.sql failed")
    assert "table third already exists" in err and "not applied" in err
    # --migrate would fail again in this state, so it is not printed.
    assert [c for c in _commands(err) if not c.startswith("mkdir")] == []
    _assert_keeps_a_copy(err, db_path, tmp_path)
    conn = sqlite3.connect(db_path)
    applied = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    conn.close()
    assert applied == ["0001_first.sql", "0002_second.sql"]


def test_the_doctor_warns_an_operator_who_cannot_write_and_exits_as_before(
    tmp_path, monkeypatch, capsys
):
    """The doctor's caller may be an operator account that reads the database
    but does not write it; athena-serve refuses that, the doctor only warns,
    and no receipt names a private class."""
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    db_path = _database(data / "athena.db")
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    _not_writable(monkeypatch, db_path)

    argv = [str(db_path), "--attach-dir", str(attach_dir), "--deployment"]
    assert ops.doctor_main(argv) == 0

    output = capsys.readouterr()
    assert "not writable by the account running this check" in output.err
    assert "run it as the account Athena runs as" in output.err
    assert "_NotWritable" not in output.out + output.err
    assert "athena-doctor: ok" in output.out


def test_operations_lists_every_refusal(monkeypatch):
    """``OPERATIONS.md`` has a section naming each refusal by the words that
    start its condition, beside every command its remedy prints (for a start
    without ``--host`` or ``--port``)."""
    monkeypatch.setattr(ops, "_START_ARGUMENTS", [])
    text = (REPO / "docs" / "OPERATIONS.md").read_text()
    match = re.search(r"^## Startup Refusals\n(?P<body>.*?)(?=^## )", text, re.M | re.S)
    assert match is not None, "docs/OPERATIONS.md has no Startup Refusals section"
    body = match["body"]
    assert "athena-restore <backup>" not in body and "--force`" not in body
    assert "-new.db" not in body
    for refusal in ops.DATABASE_REFUSALS:
        assert refusal.label in body, refusal.label
        rendered = ops.render_remedy(refusal.remedy, ops.DOCUMENTED_PATH)
        for command in _commands(rendered):
            assert f"`{command}`" in body, command


def test_athena_backup_prints_paths_escaped(tmp_path, capsys):
    data = tmp_path / "odd\x1b[2Jdir"
    data.mkdir()
    db_path = _database(data / "athena.db")
    assert ops.backup_main([str(db_path), str(data / "copy.db")]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "odd\\x1b[2Jdir" in out
