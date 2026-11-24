"""The database and its directory are private to the service user (H10).

WHY (OPS-2): the documented bootstrap left the data directory 0755 and the
database 0644 under the usual umask, so any local account could read it — and
the database holds webhook secrets in the clear, CSRF tokens and password
hashes — while attachments were 0700 and backups 0600. The systemd unit's
``UMask=0077`` does not cover a bootstrap run by hand.

So a database Athena creates is created 0600, in a data directory it creates
0700, whatever the process umask. It never changes the mode of a directory or
file that already exists (that is the operator's to change): ``athena-doctor``
warns on one that is open to group or others and names the ``chmod`` that
fixes it. SQLite creates ``-wal`` and ``-shm`` with the database file's own
mode, which the last test pins.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import os
from pathlib import Path
import shlex
import stat

import pytest

from athena import config, ops
from athena.core import db, users


@contextlib.contextmanager
def _umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """``athena-serve --bootstrap`` against ``<tmp>/data/athena.db``, not run."""
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir(mode=0o700)
    db_path = tmp_path / "data" / "athena.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "b" * 32)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: None)
    return db_path


@pytest.mark.parametrize("umask", [0o022, 0o002, 0o000], ids=["022", "002", "000"])
def test_bootstrap_creates_the_data_directory_700_and_the_database_600(launcher, umask):
    with _umask(umask):
        assert ops.serve_main(["--bootstrap"]) == 0

    assert _mode(launcher.parent) == 0o700
    assert _mode(launcher) == 0o600


def test_sqlite_sidecars_follow_the_database_mode(launcher):
    """``-wal`` and ``-shm`` exist only while a connection is open; SQLite
    creates them with the main file's permission bits, not the umask's."""
    with _umask(0o022):
        assert ops.serve_main(["--bootstrap"]) == 0
        conn = db.connect(launcher)
        try:
            conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
            conn.commit()
            wal = launcher.with_name(launcher.name + "-wal")
            shm = launcher.with_name(launcher.name + "-shm")
            assert (_mode(wal), _mode(shm)) == (0o600, 0o600)
        finally:
            conn.close()


def test_an_existing_wider_database_is_reported_not_changed(launcher, capsys):
    """Bootstrap leaves an existing directory and file as the operator made
    them; the doctor is what says they are too wide."""
    launcher.parent.mkdir(mode=0o755)
    os.chmod(launcher.parent, 0o755)
    launcher.touch()
    os.chmod(launcher, 0o644)

    assert ops.serve_main(["--bootstrap"]) == 0
    assert (_mode(launcher.parent), _mode(launcher)) == (0o755, 0o644)

    capsys.readouterr()
    assert ops.doctor_main([str(launcher)]) == 0
    found = _warnings(capsys.readouterr().err)
    assert any("is mode 0755" in line and "chmod 700" in line for line in found)
    assert any("is mode 0644" in line and "chmod 600" in line for line in found)


def _migrated(path: Path) -> Path:
    path.parent.mkdir(exist_ok=True)
    conn = db.connect(path)
    db.migrate(conn)
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


def _warnings(err: str) -> list[str]:
    return [
        line for line in err.splitlines() if line.startswith("athena-doctor: warning:")
    ]


def test_doctor_warns_on_a_wider_mode_and_names_the_command(tmp_path, capsys):
    data = tmp_path / "data dir"
    database = _migrated(data / "athena.db")
    os.chmod(data, 0o755)
    os.chmod(database, 0o644)
    # A sidecar left by an open connection is as readable as the database.
    holder = db.connect(database)
    holder.execute("UPDATE users SET name = name")
    holder.commit()
    try:
        assert ops.doctor_main([str(database)]) == 0
    finally:
        holder.close()

    output = capsys.readouterr()
    found = _warnings(output.err)
    assert len(found) == 4, output.err
    assert any(
        f"data directory {data} is mode 0755" in line
        and f"run `chmod 700 {shlex.quote(str(data))}`" in line
        for line in found
    ), found
    assert any(
        f"database file {database} is mode 0644" in line
        and f"run `chmod 600 {shlex.quote(str(database))}`" in line
        for line in found
    ), found
    assert (
        sum(
            "-wal is mode 0644" in line or "-shm is mode 0644" in line for line in found
        )
        == 2
    )
    assert "athena-doctor: ok" in output.out  # a warning, not a failure

    # Not silently fixed, and quiet once the operator runs the commands.
    assert (_mode(data), _mode(database)) == (0o755, 0o644)
    os.chmod(data, 0o700)
    os.chmod(database, 0o600)
    assert ops.doctor_main([str(database)]) == 0
    assert _warnings(capsys.readouterr().err) == []


def test_deployment_doctor_warns_too(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    database = _migrated(data / "athena.db")
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    os.chmod(data, 0o750)
    os.chmod(database, 0o640)
    monkeypatch.setattr(config, "DB_PATH", database)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)

    argv = [str(database), "--attach-dir", str(attach_dir), "--deployment"]
    assert ops.doctor_main(argv) == 0

    found = _warnings(capsys.readouterr().err)
    assert any("is mode 0750" in line and "chmod 700" in line for line in found)
    assert any("is mode 0640" in line and "chmod 600" in line for line in found)


# --- a remedy is only printed where it is safe to run (review of the first build)


def test_no_chmod_for_a_database_in_a_shared_directory(tmp_path, capsys):
    """``/tmp`` (sticky, world-writable) is no data directory to ``chmod 700``,
    and a ``chmod 600`` there does not make the database private either."""
    shared = tmp_path / "shared"
    database = _migrated(shared / "athena.db")
    os.chmod(shared, 0o1777)
    os.chmod(database, 0o644)

    assert ops.doctor_main([str(database)]) == 0

    found = _warnings(capsys.readouterr().err)
    assert found and not any("`chmod" in line for line in found), found
    assert any(f"{shared} is shared" in line for line in found), found


def test_no_directory_chmod_for_a_directory_that_holds_other_files(tmp_path, capsys):
    """A database straight in ``/var/lib`` must not be answered with
    ``chmod 700 /var/lib``: only a directory of Athena's own gets one."""
    data = tmp_path / "lib"
    database = _migrated(data / "athena.db")
    (data / "someone-elses.conf").write_text("x")
    os.chmod(data, 0o755)
    os.chmod(database, 0o644)

    assert ops.doctor_main([str(database)]) == 0

    found = _warnings(capsys.readouterr().err)
    directory = [line for line in found if "data directory" in line]
    assert directory and "`chmod" not in directory[0], found
    assert "holds other entries besides Athena's" in directory[0]
    assert any(f"`chmod 600 {shlex.quote(str(database))}`" in line for line in found)


def test_a_database_path_that_is_a_directory_is_named_as_one(tmp_path, capsys):
    directory = tmp_path / "lib" / "athena"
    directory.mkdir(parents=True)
    os.chmod(directory, 0o755)

    ops.doctor_main([str(directory)])

    found = _warnings(capsys.readouterr().err)
    assert any("is a directory, not a database file" in line for line in found)
    assert not any("`chmod" in line for line in found), found


def test_a_doctor_line_never_carries_a_raw_control_character(tmp_path, capsys):
    data = tmp_path / "odd\x1b[2Jdir"
    database = _migrated(data / "athena.db")
    os.chmod(data, 0o755)

    assert ops.doctor_main([str(database)]) == 0

    output = capsys.readouterr()
    for line in (output.out + output.err).splitlines():
        assert "\x1b" not in line
    assert any("odd\\x1b[2Jdir" in line for line in _warnings(output.err))


def test_a_dangling_symlink_at_the_database_path_gets_a_private_target(launcher):
    launcher.parent.mkdir(mode=0o700)
    target = launcher.with_name("real.db")
    launcher.symlink_to(target)

    with _umask(0o022):
        assert ops.serve_main(["--bootstrap"]) == 0

    assert _mode(target) == 0o600


# --- second review: exact names, group access, control characters ------------


@pytest.mark.parametrize(
    "intruder",
    ["athena.db_someone_elses_secret_copy", "attachments-of-another-app", "lost+found"],
)
def test_a_directory_with_any_other_entry_gets_no_directory_chmod(
    tmp_path, capsys, intruder
):
    data = tmp_path / "data"
    database = _migrated(data / "athena.db")
    if intruder.startswith("athena"):
        (data / intruder).write_text("x")
    else:
        (data / intruder).mkdir()
    os.chmod(data, 0o755)

    assert ops.doctor_main([str(database)]) == 0

    (line,) = [w for w in _warnings(capsys.readouterr().err) if "data directory" in w]
    assert "`chmod" not in line
    assert "holds other entries besides Athena's" in line
    assert "not Athena's" not in line.replace("besides Athena's", "")


def test_the_configured_attachment_directory_is_athenas(tmp_path, capsys):
    data = tmp_path / "data"
    database = _migrated(data / "athena.db")
    (data / "attachments").mkdir()
    os.chmod(data, 0o755)

    argv = [str(database), "--attach-dir", str(data / "attachments")]
    assert ops.doctor_main(argv) == 0

    found = _warnings(capsys.readouterr().err)
    assert any(f"`chmod 700 {shlex.quote(str(data))}`" in line for line in found)


def test_a_group_writable_directory_or_file_gets_no_chmod(tmp_path, capsys):
    """A service account may reach the database through the group: a
    ``chmod 700``/``600`` would cut it off, so only the mode is stated."""
    data = tmp_path / "data"
    database = _migrated(data / "athena.db")
    os.chmod(data, 0o775)
    os.chmod(database, 0o664)

    assert ops.doctor_main([str(database)]) == 0

    found = _warnings(capsys.readouterr().err)
    assert len(found) == 2 and not any("`chmod" in line for line in found), found
    assert all("writable by its group or others" in line for line in found)


def test_a_path_with_a_control_character_gets_no_command(tmp_path, capsys):
    data = tmp_path / "odd\ndir"
    database = _migrated(data / "athena.db")
    os.chmod(data, 0o755)
    os.chmod(database, 0o644)

    assert ops.doctor_main([str(database)]) == 0

    found = _warnings(capsys.readouterr().err)
    assert len(found) == 2 and not any("`" in line for line in found), found
    assert all("control character, so no command is printed" in line for line in found)
