"""The built wheel must carry every package-owned runtime file, exactly once."""

import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_wheel.py"
_SPEC = importlib.util.spec_from_file_location("athena_verify_wheel", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
verify_wheel = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify_wheel)


def _source_tree(root: Path) -> set[str]:
    files = {
        "core/migrations/0001_initial.sql",
        "static/styles.css",
        "templates/home.html",
        "templates/aegis/issues.html",
    }
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    return files


def _wheel(
    path: Path,
    files: set[str],
    *,
    entry_points: str | None = (
        "[console_scripts]\nathena-serve = athena.ops:serve_main\n"
    ),
) -> None:
    with ZipFile(path, "w") as archive:
        for relative in files:
            archive.writestr(f"athena/{relative}", relative)
        archive.writestr("athena/__init__.py", "")
        archive.writestr("athena-0.0.1.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        if entry_points is not None:
            archive.writestr(
                "athena-0.0.1.dist-info/entry_points.txt",
                entry_points,
            )


def test_verify_runtime_manifest_accepts_an_exact_wheel(tmp_path):
    package_root = tmp_path / "src" / "athena"
    files = _source_tree(package_root)
    wheel_path = tmp_path / "athena.whl"
    _wheel(wheel_path, files)

    assert verify_wheel.verify_runtime_manifest(
        wheel_path, package_root=package_root
    ) == {
        "core/migrations": 1,
        "static": 1,
        "templates": 2,
    }


def test_verify_runtime_manifest_reports_missing_and_unexpected_files(tmp_path):
    package_root = tmp_path / "src" / "athena"
    files = _source_tree(package_root)
    wheel_path = tmp_path / "athena.whl"
    _wheel(
        wheel_path,
        (files - {"templates/aegis/issues.html"}) | {"static/stale.js"},
    )

    with pytest.raises(RuntimeError) as exc_info:
        verify_wheel.verify_runtime_manifest(wheel_path, package_root=package_root)

    message = str(exc_info.value)
    assert "missing:\n  templates/aegis/issues.html" in message
    assert "unexpected:\n  static/stale.js" in message


def test_verify_runtime_manifest_rejects_duplicate_wheel_members(tmp_path):
    package_root = tmp_path / "src" / "athena"
    files = _source_tree(package_root)
    wheel_path = tmp_path / "athena.whl"
    _wheel(wheel_path, files)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with ZipFile(wheel_path, "a") as archive:
            archive.writestr("athena/static/styles.css", "tampered duplicate")

    with pytest.raises(RuntimeError) as exc_info:
        verify_wheel.verify_runtime_manifest(wheel_path, package_root=package_root)

    assert "duplicates:\n  static/styles.css" in str(exc_info.value)


def test_verify_console_scripts_accepts_exact_safe_launcher_mapping(tmp_path):
    wheel_path = tmp_path / "athena.whl"
    _wheel(wheel_path, set())

    verify_wheel.verify_console_scripts(wheel_path)


@pytest.mark.parametrize(
    ("entry_points", "message"),
    [
        (None, "exactly one"),
        ("[other]\nathena-serve = athena.ops:serve_main\n", "console_scripts"),
        ("[console_scripts]\n", "got missing"),
        (
            "[console_scripts]\nathena-serve = athena.main:app\n",
            "athena.ops:serve_main",
        ),
    ],
)
def test_verify_console_scripts_rejects_missing_or_wrong_mapping(
    tmp_path, entry_points, message
):
    wheel_path = tmp_path / "athena.whl"
    _wheel(wheel_path, set(), entry_points=entry_points)

    with pytest.raises(RuntimeError, match=message):
        verify_wheel.verify_console_scripts(wheel_path)


def test_verify_console_scripts_rejects_ambiguous_metadata(tmp_path):
    wheel_path = tmp_path / "athena.whl"
    _wheel(wheel_path, set())
    with ZipFile(wheel_path, "a") as archive:
        archive.writestr(
            "other-0.0.1.dist-info/entry_points.txt",
            "[console_scripts]\nathena-serve = athena.ops:serve_main\n",
        )

    with pytest.raises(RuntimeError, match="exactly one"):
        verify_wheel.verify_console_scripts(wheel_path)
