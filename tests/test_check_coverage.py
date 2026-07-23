"""The coverage ratchet must measure every runtime file and fail independently."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage.py"
_WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "coverage.sh"
_SPEC = importlib.util.spec_from_file_location("athena_check_coverage", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_coverage = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check_coverage
_SPEC.loader.exec_module(check_coverage)


def _write_project(
    root: Path,
    *,
    line_floor: str = "90.00",
    branch_floor: str = "80.00",
    combined_floor: str = "87.14",
    extra_run_config: str = "",
    extra_report_config: str = "",
    extra_pragma: bool = False,
    extra_branch_pragma: bool = False,
) -> dict:
    package = root / "src" / "athena"
    package.mkdir(parents=True)
    init_source = "VALUE = 1\n"
    if extra_pragma:
        init_source += "VALUE = 2  # pragma: no cover\n"
    if extra_branch_pragma:
        init_source += "if VALUE:  # pragma no branch\n    VALUE = 2\n"
    (package / "__init__.py").write_text(init_source, encoding="utf-8")
    (package / "demo.py").write_text(
        'if __name__ == "__main__":  # pragma: no cover - console-script parity\n'
        "    raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[tool.coverage.run]\n"
        "branch = true\n"
        "relative_files = true\n"
        'source = ["src/athena"]\n'
        f"{extra_run_config}"
        "\n[tool.coverage.report]\n"
        "precision = 2\n"
        "show_missing = true\n"
        "skip_empty = false\n"
        f"{extra_report_config}"
        "\n[tool.athena.coverage]\n"
        f"line_floor = {line_floor}\n"
        f"branch_floor = {branch_floor}\n"
        f"combined_floor = {combined_floor}\n"
        "excluded_line_count = 2\n",
        encoding="utf-8",
    )

    files = {
        "src/athena/__init__.py": {
            "summary": {
                "covered_lines": 45,
                "num_statements": 50,
                "missing_lines": 5,
                "excluded_lines": 0,
                "covered_branches": 16,
                "num_branches": 20,
                "missing_branches": 4,
            }
        },
        "src/athena/demo.py": {
            "summary": {
                "covered_lines": 45,
                "num_statements": 50,
                "missing_lines": 5,
                "excluded_lines": 2,
                "covered_branches": 16,
                "num_branches": 20,
                "missing_branches": 4,
            }
        },
    }
    return {
        "meta": {"branch_coverage": True},
        "files": files,
        "totals": {
            "covered_lines": 90,
            "num_statements": 100,
            "missing_lines": 10,
            "excluded_lines": 2,
            "covered_branches": 32,
            "num_branches": 40,
            "missing_branches": 8,
        },
    }


def _evaluate(tmp_path: Path, payload: dict):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    return check_coverage.evaluate(report, repo_root=tmp_path)


def test_accepts_complete_branch_report_at_all_floors(tmp_path):
    payload = _write_project(tmp_path)

    metrics, floors, file_count = _evaluate(tmp_path, payload)

    assert file_count == 2
    assert metrics.line_percent == check_coverage.Decimal("90")
    assert metrics.branch_percent == check_coverage.Decimal("80")
    assert metrics.combined_percent > floors.combined


@pytest.mark.parametrize(
    ("floor", "value", "message"),
    [
        ("line_floor", "90.01", "line 90.00000% < 90.01%"),
        ("branch_floor", "80.01", "branch 80.00000% < 80.01%"),
        ("combined_floor", "87.15", "combined 87.14286% < 87.15%"),
    ],
)
def test_fails_each_floor_independently(tmp_path, floor, value, message):
    kwargs = {floor: value}
    payload = _write_project(tmp_path, **kwargs)

    with pytest.raises(check_coverage.CoverageGateError, match=message):
        _evaluate(tmp_path, payload)


def test_rejects_missing_runtime_file(tmp_path):
    payload = _write_project(tmp_path)
    payload["files"].pop("src/athena/demo.py")
    payload["totals"] = payload["files"]["src/athena/__init__.py"]["summary"]

    with pytest.raises(check_coverage.CoverageGateError, match="inventory mismatch"):
        _evaluate(tmp_path, payload)


def test_rejects_unexpected_reported_file(tmp_path):
    payload = _write_project(tmp_path)
    payload["files"]["tests/not_runtime.py"] = {
        "summary": {
            "covered_lines": 1,
            "num_statements": 1,
            "missing_lines": 0,
            "excluded_lines": 0,
            "covered_branches": 0,
            "num_branches": 0,
            "missing_branches": 0,
        }
    }

    with pytest.raises(check_coverage.CoverageGateError, match="unexpected"):
        _evaluate(tmp_path, payload)


def test_rejects_non_branch_report(tmp_path):
    payload = _write_project(tmp_path)
    payload["meta"]["branch_coverage"] = False

    with pytest.raises(
        check_coverage.CoverageGateError, match="did not measure branches"
    ):
        _evaluate(tmp_path, payload)


@pytest.mark.parametrize(
    ("extra_run_config", "extra_report_config", "setting"),
    [
        ('omit = ["src/athena/demo.py"]\n', "", "run.omit"),
        ("", 'partial_also = ["if debug"]\n', "report.partial_also"),
        (
            "",
            'partial_branches_always = ["if debug"]\n',
            "report.partial_branches_always",
        ),
    ],
)
def test_rejects_narrowing_configuration(
    tmp_path, extra_run_config, extra_report_config, setting
):
    payload = _write_project(
        tmp_path,
        extra_run_config=extra_run_config,
        extra_report_config=extra_report_config,
    )

    with pytest.raises(
        check_coverage.CoverageGateError,
        match=rf"settings are forbidden: {setting}",
    ):
        _evaluate(tmp_path, payload)


def test_rejects_new_coverage_pragma(tmp_path):
    payload = _write_project(tmp_path, extra_pragma=True)

    with pytest.raises(check_coverage.CoverageGateError, match="pragma policy changed"):
        _evaluate(tmp_path, payload)


def test_rejects_alternate_no_branch_pragma(tmp_path):
    payload = _write_project(tmp_path, extra_branch_pragma=True)

    with pytest.raises(check_coverage.CoverageGateError, match="pragma policy changed"):
        _evaluate(tmp_path, payload)


def test_rejects_excluded_line_count_drift(tmp_path):
    payload = _write_project(tmp_path)
    payload["files"]["src/athena/demo.py"]["summary"]["excluded_lines"] = 3
    payload["totals"]["excluded_lines"] = 3

    with pytest.raises(check_coverage.CoverageGateError, match="expected 2"):
        _evaluate(tmp_path, payload)


def test_rejects_totals_that_do_not_match_file_summaries(tmp_path):
    payload = _write_project(tmp_path)
    payload["totals"]["covered_lines"] += 1

    with pytest.raises(check_coverage.CoverageGateError, match="per-file summaries"):
        _evaluate(tmp_path, payload)


def test_cli_reports_validation_failure_without_traceback(tmp_path, capsys):
    payload = _write_project(tmp_path)
    payload["meta"]["branch_coverage"] = False
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(payload), encoding="utf-8")

    result = check_coverage.main([str(report), "--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert (
        "coverage gate failed: coverage report did not measure branches" in captured.err
    )
    assert "Traceback" not in captured.err


def _run_wrapper(
    *arguments: str,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ATHENA_PYTHON"] = "/usr/bin/true"
    env.update(extra_env or {})
    return subprocess.run(
        [str(_WRAPPER), *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_wrapper_rejects_any_preexisting_output(tmp_path):
    report_dir = tmp_path / "evidence"
    report_dir.mkdir()
    (report_dir / "unrelated.txt").write_text("preserve me", encoding="utf-8")

    result = _run_wrapper(str(report_dir))

    assert result.returncode == 2
    assert f"coverage report directory must be empty: {report_dir}" in result.stderr


def test_wrapper_fails_when_pytest_produces_no_evidence(tmp_path):
    report_dir = tmp_path / "evidence"

    result = _run_wrapper(str(report_dir))

    assert result.returncode == 1
    assert (
        f"coverage evidence must be a non-empty regular file: {report_dir}/.coverage"
        in result.stderr
    )


def test_wrapper_canonicalizes_relative_tmpdir_before_chdir(tmp_path):
    working_dir = tmp_path / "outside"
    tmp_dir = working_dir / "reports"
    tmp_dir.mkdir(parents=True)

    result = _run_wrapper(cwd=working_dir, extra_env={"TMPDIR": "reports"})

    assert result.returncode == 1
    assert "coverage evidence must be a non-empty regular file" in result.stderr
    assert str(tmp_dir.resolve()) in result.stderr
