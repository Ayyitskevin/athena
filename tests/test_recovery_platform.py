"""Recovery is Linux-only by decision, and the doctor says so at install time.

docs/OPERATIONS.md ("Supported platform") records the decision; these tests pin
the half of it that is code: a platform lacking either primitive recovery needs
is named by ``athena-doctor`` in every mode, as a warning that does not fail it.
"""

from __future__ import annotations

from pathlib import Path

from athena import ops
from athena.core import db, recovery


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "athena.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


def _platform_warnings(err: str) -> list[str]:
    return [
        line
        for line in err.splitlines()
        if line.startswith("athena-doctor: warning: backup bundles and restore")
    ]


def test_this_linux_host_supports_recovery():
    assert recovery.recovery_platform_problem() is None


def test_a_platform_that_is_not_linux_is_named(monkeypatch):
    monkeypatch.setattr(recovery.sys, "platform", "darwin")
    assert recovery.recovery_platform_problem() == "this platform is darwin, not Linux"


def test_linux_without_procfs_is_named(monkeypatch, tmp_path):
    monkeypatch.setattr(recovery, "Path", lambda _: tmp_path / "no-proc")
    assert recovery.recovery_platform_problem() == "procfs is not mounted at /proc"


def test_a_c_library_without_renameat2_is_named(monkeypatch):
    class _OldLibc:
        pass

    monkeypatch.setattr(recovery.ctypes, "CDLL", lambda *a, **k: _OldLibc())
    assert recovery.recovery_platform_problem() == "the C library has no renameat2"


def test_the_doctor_warns_off_linux_without_failing(monkeypatch, tmp_path, capsys):
    database = _database(tmp_path)
    monkeypatch.setattr(
        ops, "recovery_platform_problem", lambda: "this platform is darwin, not Linux"
    )

    assert ops.doctor_main([str(database)]) == 0

    (line,) = _platform_warnings(capsys.readouterr().err)
    assert "Linux only (this platform is darwin, not Linux)" in line
    assert "docs/OPERATIONS.md" in line


def test_the_doctor_warns_in_recovery_bundle_mode_too(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        ops, "recovery_platform_problem", lambda: "procfs is not mounted at /proc"
    )

    # The bundle is absent, so the check fails; the platform note still comes
    # first, which is the point: it explains the failure an operator is about
    # to read.
    assert ops.doctor_main([str(tmp_path / "absent"), "--recovery-bundle"]) != 0

    err = capsys.readouterr().err
    (line,) = _platform_warnings(err)
    assert err.index(line) < err.index("athena-doctor: mode=recovery-bundle")


def test_the_doctor_is_silent_about_platform_on_linux(tmp_path, capsys):
    assert ops.doctor_main([str(_database(tmp_path))]) == 0
    assert _platform_warnings(capsys.readouterr().err) == []
