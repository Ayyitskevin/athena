"""Native-only activity readers exclude imported rows — and the doc agrees."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_imported_at_guards.py"
FORGE = ROOT / "docs" / "FORGE.md"

_SPEC = importlib.util.spec_from_file_location("athena_check_imported_at", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
guards = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = guards
_SPEC.loader.exec_module(guards)


def _run(package_root: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT)]
    if package_root is not None:
        command.extend(("--package-root", str(package_root)))
    return subprocess.run(
        command, cwd=ROOT, check=False, capture_output=True, text=True
    )


def _tree_copy(tmp_path: Path) -> Path:
    package_root = tmp_path / "athena"
    shutil.copytree(ROOT / "src" / "athena", package_root)
    return package_root


def test_current_source_carries_every_guard():
    result = _run()

    assert result.returncode == 0, result.stderr


def test_dropping_a_guard_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / "aegis" / "fleet_attention.py"
    source = target.read_text(encoding="utf-8")
    assert "imported_at IS NULL" in source
    target.write_text(source.replace("imported_at IS NULL", "1 = 1"), encoding="utf-8")

    result = _run(package_root)

    assert result.returncode == 1
    assert "Attention rollup" in result.stderr
    assert "lost its guard" in result.stderr


def test_dropping_a_module_level_room_guard_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / "aegis" / "rooms.py"
    source = target.read_text(encoding="utf-8")
    assert "WHERE a.imported_at IS NULL" in source
    target.write_text(
        source.replace("WHERE a.imported_at IS NULL", "WHERE 1 = 1"),
        encoding="utf-8",
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "aegis/rooms.py::<module> lost its guard" in result.stderr


def test_a_new_activity_reader_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / "aegis" / "dashboard.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + '\n\ndef count_events(conn):\n    return conn.execute("SELECT COUNT(*) FROM activity").fetchone()\n',
        encoding="utf-8",
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "new activity reader" in result.stderr
    assert "dashboard.py::count_events" in result.stderr


def test_an_exemption_that_stops_reading_the_trail_fails_as_stale(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / "core" / "webhooks.py"
    source = target.read_text(encoding="utf-8")
    assert "FROM activity" in source
    target.write_text(
        source.replace("FROM activity", "FROM webhooks"),
        encoding="utf-8",
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "stale exemption" in result.stderr
    assert "webhooks.py::current_tip" in result.stderr


def test_automation_scan_without_native_only_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / "aegis" / "automation.py"
    source = target.read_text(encoding="utf-8")
    assert "native_only=True" in source
    target.write_text(
        source.replace("native_only=True", "native_only=False"), encoding="utf-8"
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "Automation scan" in result.stderr


def _forge_guard_table() -> list[tuple[str, str]]:
    """The (mechanism, guard) rows of FORGE.md's native-only guard table."""
    rows: list[tuple[str, str]] = []
    in_table = False
    for line in FORGE.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Mechanism | Guard |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            if set(line) <= set("|- "):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            rows.append((cells[0], cells[1]))
    return rows


def test_forge_guard_table_is_pinned_to_the_checker():
    """The H-0.4 failure was a doc claiming a guard the code did not carry; the
    table and the checker must name the same mechanisms in the same order."""
    table = _forge_guard_table()
    mechanisms = [(label, guard) for label, guard, _assertions in guards.MECHANISMS]

    assert [mechanism for mechanism, _guard in table] == [
        mechanism for mechanism, _guard in mechanisms
    ]
    for (_mechanism, cell), (_label, guard) in zip(table, mechanisms):
        assert guard in cell
