"""Native-only activity readers exclude imported rows — and the doc agrees."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


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


def test_project_brief_native_completion_guard_loss_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / "aegis" / "project_brief.py"
    source = target.read_text(encoding="utf-8")
    guard = "AND la.imported_at IS NULL"  # the latest completion's per-issue seek
    assert source.count(guard) == 1
    pristine = _run(package_root)
    assert pristine.returncode == 0, pristine.stderr

    target.write_text(source.replace(guard, ""), encoding="utf-8")
    result = _run(package_root)

    assert result.returncode == 1
    assert "1 violation" in result.stderr
    assert "aegis/project_brief.py::_build lost its guard" in result.stderr

    target.write_text(source, encoding="utf-8")
    restored = _run(package_root)
    assert restored.returncode == 0, restored.stderr


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


@pytest.mark.parametrize("mutation", ["condition", "return"])
def test_imported_watcher_guard_loss_fails(tmp_path, mutation):
    package_root = _tree_copy(tmp_path)
    target = package_root / "core" / "notifications.py"
    source = target.read_text(encoding="utf-8")
    guard = 'if event is not None and event["imported_at"] is not None:\n        return'
    assert source.count(guard) == 1
    broken = (
        guard.replace(
            'event["imported_at"] is not None', 'event["imported_at"] is None'
        )
        if mutation == "condition"
        else guard.replace("return", "pass")
    )
    target.write_text(source.replace(guard, broken), encoding="utf-8")

    result = _run(package_root)

    assert result.returncode == 1
    assert "core/notifications.py::notify_watchers lost its guard" in result.stderr


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


# --- the declarations live beside their modules (B8) ---------------------------

AEGIS_FILE = Path("aegis") / "imported_at_guards.toml"
CORE_FILE = Path("core") / "imported_at_guards.toml"
WEBHOOKS_ENTRY = '''[[exempt]]
module = "webhooks.py"
function = "current_tip"
why = """\\
    the delivery cursor spans the whole firehose; webhook subscribers to the \\
    trail receive foreign history too, honestly marked"""
'''
LIFECYCLE_ENTRY = """[[mechanism]]
label = "Lifecycle facts (0055)"
module = "migrations/0055_issue_lifecycle_facts.sql"
pattern = 'imported_at IS NULL'
"""


def _details(package_root: Path) -> list[str]:
    return [violation.detail for violation in guards.find_violations(package_root)]


def _edit(package_root: Path, relative: Path, old: str, new: str) -> None:
    target = package_root / relative
    source = target.read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    target.write_text(source.replace(old, new), encoding="utf-8")


def test_every_package_with_a_reader_has_its_own_guard_file():
    package_root = ROOT / "src" / "athena"
    files = sorted(
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob(guards.GUARD_FILE)
    )
    assert files == ["aegis/imported_at_guards.toml", "core/imported_at_guards.toml"]
    registry = guards.load_registry(package_root)
    assert registry.problems == ()
    # Each entry sits in the file of the package that holds its module.
    for relative, _function, _why in registry.exempt_readers:
        assert guards._home(package_root, relative) == (
            package_root / relative.split("/")[0] / guards.GUARD_FILE
        ), relative


def test_the_views_are_the_gathered_registry():
    registry = guards.load_registry(ROOT / "src" / "athena")
    assert guards.MECHANISMS == registry.mechanisms
    assert guards.EXTRA_GUARDED == registry.extra_guarded
    assert guards.EXEMPT_READERS == registry.exempt_readers
    assert [label for label, _g, _a in guards.MECHANISMS] == [
        label for label, _guard in guards.MECHANISM_ROWS
    ]
    with pytest.raises(AttributeError):
        guards.NOT_A_LIST  # noqa: B018


def test_an_entry_declared_in_the_wrong_packages_file_fails(tmp_path):
    # core/webhooks.py's exemption, moved into the aegis file: the aegis file
    # names a module it does not hold, and the core reader is now undeclared.
    package_root = _tree_copy(tmp_path)
    _edit(package_root, CORE_FILE, WEBHOOKS_ENTRY, "")
    target = package_root / AEGIS_FILE
    target.write_text(
        target.read_text(encoding="utf-8") + "\n" + WEBHOOKS_ENTRY, encoding="utf-8"
    )

    details = _details(package_root)

    assert any(
        "declared in the wrong home: aegis/webhooks.py does not exist" in d
        and "core/webhooks.py defines there" in d
        for d in details
    ), details
    assert any(
        "new activity reader core/webhooks.py::current_tip" in d for d in details
    )


def test_an_entry_above_a_nearer_guard_file_is_in_the_wrong_home(tmp_path):
    # A guard file nearer to a module takes its entries: once core/migrations/
    # has one, the migration entries left in core's file are misplaced, and the
    # one moved there is fine.
    package_root = _tree_copy(tmp_path)
    _edit(package_root, CORE_FILE, LIFECYCLE_ENTRY, "")
    (package_root / "core" / "migrations" / guards.GUARD_FILE).write_text(
        LIFECYCLE_ENTRY.replace("migrations/0055", "0055"), encoding="utf-8"
    )

    misplaced = [d for d in _details(package_root) if "wrong home" in d]

    assert len(misplaced) == 3, misplaced
    for number in ("0058", "0068", "0082"):
        assert any(f"core/migrations/{number}_" in d for d in misplaced), number
    assert all("core/migrations/imported_at_guards.toml" in d for d in misplaced)
    assert not any("0055" in d for d in _details(package_root))


def test_an_exemption_naming_a_function_that_does_not_exist_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    _edit(
        package_root,
        CORE_FILE,
        'function = "current_tip"',
        'function = "current_tips"',
    )

    details = _details(package_root)

    assert any(
        "exempt entry names core/webhooks.py::current_tips, a function that "
        "module does not define" in d
        for d in details
    ), details
    assert any(
        "new activity reader core/webhooks.py::current_tip" in d for d in details
    )


def test_a_guarded_entry_naming_a_function_that_does_not_exist_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    _edit(
        package_root,
        AEGIS_FILE,
        'function = "_schedule_target_query"',
        'function = "_schedule_target"',
    )

    details = _details(package_root)

    assert "aegis/automation.py has no function _schedule_target" in details
    assert any(
        "new activity reader aegis/automation.py::_schedule_target_query" in d
        for d in details
    ), details


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'label = "Undo"',
            'label = "Undoing"',
            "mechanism 'Undoing' is not a row of MECHANISM_ROWS",
        ),
        ('label = "Undo"', 'label = "Undo"\nowner = "core"', "unknown keys ['owner']"),
        ('module = "undo.py"\n', "", "missing keys ['module']"),
        (
            'module = "webhooks.py"',
            'module = "../core/webhooks.py"',
            "must be a path relative to",
        ),
        (
            'function = "current_tip"',
            'function = "<module>"',
            "stale exemption: core/webhooks.py::<module>",
        ),
        (
            'label = "Undo"\nmodule = "undo.py"\nfunction = "undo"',
            'label = "Undo"\nmodule = "undo.py"',
            "a Python module's entry names the function",
        ),
        (
            'module = "migrations/0055_issue_lifecycle_facts.sql"\n',
            'module = "migrations/0055_issue_lifecycle_facts.sql"\n'
            'function = "trigger"\n',
            "a migration is declared only as a [[mechanism]] without a function",
        ),
    ],
)
def test_a_malformed_entry_fails(tmp_path, old, new, message):
    package_root = _tree_copy(tmp_path)
    _edit(package_root, CORE_FILE, old, new)

    details = _details(package_root)

    assert any(message in d for d in details), details


def test_a_row_with_no_mechanism_entry_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    undo_entry = """[[mechanism]]
label = "Undo"
module = "undo.py"
function = "undo"
pattern = 'event\\["imported_at"\\] is not None'
"""
    _edit(package_root, CORE_FILE, undo_entry, "")

    details = _details(package_root)

    assert (
        "Undo: no [[mechanism]] entry carries this row of docs/FORGE.md's "
        "guard table" in details
    )


def test_an_entry_declared_twice_or_both_ways_fails(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / CORE_FILE
    guarded_twin = WEBHOOKS_ENTRY.replace("[[exempt]]", "[[guarded]]").replace(
        'why = """', 'pattern = \'FROM activity\'\nwhy = """'
    )
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n"
        + WEBHOOKS_ENTRY
        + "\n"
        + guarded_twin,
        encoding="utf-8",
    )

    details = _details(package_root)

    assert any(
        "core/webhooks.py::current_tip is declared twice as exempt" in d
        for d in details
    ), details
    assert any(
        "core/webhooks.py::current_tip is declared both guarded and exempt" in d
        for d in details
    ), details


def test_unknown_kinds_and_unparsable_files_fail(tmp_path):
    package_root = _tree_copy(tmp_path)
    target = package_root / CORE_FILE
    pristine = target.read_text(encoding="utf-8")
    target.write_text(pristine + '\n[[exemption]]\nmodule = "undo.py"\n', "utf-8")
    assert any("unknown entry kinds ['exemption']" in d for d in _details(package_root))

    target.write_text(pristine, "utf-8")
    # A new package's file is gathered like the others.
    (package_root / "mentor" / guards.GUARD_FILE).write_text("exempt = 3\n", "utf-8")
    assert any(
        "exempt must be written as [[exempt]] tables" in d
        for d in _details(package_root)
    )

    target.write_text(pristine + "\n[[exempt]\n", "utf-8")
    result = _run(package_root)
    assert result.returncode == 1
    assert "cannot parse" in result.stderr
    assert "core/imported_at_guards.toml" in result.stderr
