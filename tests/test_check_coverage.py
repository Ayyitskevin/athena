"""The coverage ratchet must measure every runtime file and fail independently."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

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


def _round_counts(payload: dict) -> dict:
    """Measure exactly 90% line, 70% branch and 80% combined coverage.

    The default fixture's combined value (87.142857...%) has no two-decimal floor
    exactly half a point below it; these counts do.
    """
    payload["files"]["src/athena/__init__.py"]["summary"] = {
        "covered_lines": 90,
        "num_statements": 100,
        "missing_lines": 10,
        "excluded_lines": 0,
        "covered_branches": 70,
        "num_branches": 100,
        "missing_branches": 30,
    }
    payload["files"]["src/athena/demo.py"]["summary"] = {
        "covered_lines": 0,
        "num_statements": 0,
        "missing_lines": 0,
        "excluded_lines": 2,
        "covered_branches": 0,
        "num_branches": 0,
        "missing_branches": 0,
    }
    payload["totals"] = {
        "covered_lines": 90,
        "num_statements": 100,
        "missing_lines": 10,
        "excluded_lines": 2,
        "covered_branches": 70,
        "num_branches": 100,
        "missing_branches": 30,
    }
    return payload


_HALF_A_POINT_BELOW = {
    "line_floor": "89.50",
    "branch_floor": "69.50",
    "combined_floor": "79.50",
}


def test_ratchet_passes_exactly_half_a_point_above_each_floor(tmp_path):
    payload = _round_counts(_write_project(tmp_path, **_HALF_A_POINT_BELOW))

    metrics, floors, _ = _evaluate(tmp_path, payload)

    assert metrics.line_percent - floors.line == check_coverage.Decimal("0.5")
    assert metrics.branch_percent - floors.branch == check_coverage.Decimal("0.5")
    assert metrics.combined_percent - floors.combined == check_coverage.Decimal("0.5")


@pytest.mark.parametrize(
    ("floor", "value", "message"),
    [
        (
            "line_floor",
            "89.49",
            "line 90.00000% is more than 0.5 points above its floor 89.49%: "
            "raise the floor to 89.75 (tool.athena.coverage.line_floor in "
            "pyproject.toml)",
        ),
        (
            "branch_floor",
            "69.49",
            "branch 70.00000% is more than 0.5 points above its floor 69.49%: "
            "raise the floor to 69.75 (tool.athena.coverage.branch_floor in "
            "pyproject.toml)",
        ),
        (
            "combined_floor",
            "79.49",
            "combined 80.00000% is more than 0.5 points above its floor 79.49%: "
            "raise the floor to 79.75 (tool.athena.coverage.combined_floor in "
            "pyproject.toml)",
        ),
    ],
)
def test_ratchet_fails_each_outgrown_floor_with_the_floor_to_paste(
    tmp_path, floor, value, message
):
    # Gains are locked in: a floor more than half a point under the measurement
    # fails, and the message names the number to write instead of making the
    # contributor work it out.
    payload = _round_counts(
        _write_project(tmp_path, **{**_HALF_A_POINT_BELOW, floor: value})
    )

    with pytest.raises(check_coverage.CoverageFloorError) as failed:
        _evaluate(tmp_path, payload)

    assert failed.value.failures == (message,)


def test_ratchet_rounds_the_suggested_floor_down_to_two_decimals(tmp_path):
    # 87.142857...% combined: measured minus 0.25 is 86.892857..., so 86.89.
    payload = _write_project(tmp_path, combined_floor="86.00")

    with pytest.raises(
        check_coverage.CoverageFloorError, match=r"raise the floor to 86\.89 "
    ):
        _evaluate(tmp_path, payload)


def test_pasting_the_suggested_floor_passes_locally_and_on_ci(tmp_path):
    # The rule the message follows. A local run, and CI's four combined shards
    # (measured up to about 0.09 points higher), must both pass after one paste.
    for hundred_thousandths in range(9_000_000, 9_500_000, 997):
        measured = check_coverage.Decimal(hundred_thousandths) / 100_000
        floor = check_coverage.suggested_floor(measured)
        assert floor == floor.quantize(check_coverage.Decimal("0.01"))
        assert check_coverage.Decimal("0.25") <= measured - floor
        assert measured - floor < check_coverage.Decimal("0.26")
        ci = measured + check_coverage.Decimal("0.09")
        assert ci - floor <= check_coverage.RATCHET_SLACK

    measured = check_coverage.Decimal(80)
    payload = _round_counts(
        _write_project(
            tmp_path,
            line_floor=str(check_coverage.suggested_floor(measured + 10)),
            branch_floor=str(check_coverage.suggested_floor(measured - 10)),
            combined_floor=str(check_coverage.suggested_floor(measured)),
        )
    )
    _evaluate(tmp_path, payload)


def test_cli_reports_the_measurement_and_each_failed_floor(tmp_path, capsys):
    payload = _round_counts(
        _write_project(tmp_path, line_floor="89.49", branch_floor="70.01")
    )
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(payload), encoding="utf-8")

    result = check_coverage.main([str(report), "--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    # scripts/gate.py reads these lines into its receipt, pass or fail.
    assert "Line coverage: 90/100 = 90.00000% (floor 89.49%)" in captured.out
    assert "Branch coverage: 70/100 = 70.00000% (floor 70.01%)" in captured.out
    assert "Combined coverage: 160/200 = 80.00000% (floor 87.14%)" in captured.out
    assert [line for line in captured.out.splitlines() if "floor failed" in line] == [
        "Coverage floor failed: line 90.00000% is more than 0.5 points above its "
        "floor 89.49%: raise the floor to 89.75 "
        "(tool.athena.coverage.line_floor in pyproject.toml)",
        "Coverage floor failed: branch 70.00000% < 70.01%",
        "Coverage floor failed: combined 80.00000% < 87.14%",
    ]
    assert "coverage gate failed: coverage floor failed: line" in captured.err


def test_gate_reports_an_outgrown_floor_as_a_failed_coverage_stage(tmp_path, capsys):
    # The product gate's coverage stage runs this checker through coverage.sh; a
    # ratchet failure must fail that stage and put the paste-ready number in the
    # receipt, not only in scrolled-past output.
    gate_spec = importlib.util.spec_from_file_location(
        "athena_gate_for_coverage_ratchet", _SCRIPT.parent / "gate.py"
    )
    assert gate_spec is not None and gate_spec.loader is not None
    gate = importlib.util.module_from_spec(gate_spec)
    sys.modules[gate_spec.name] = gate
    gate_spec.loader.exec_module(gate)
    payload = _round_counts(
        _write_project(tmp_path, **{**_HALF_A_POINT_BELOW, "branch_floor": "60.25"})
    )
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    checker = gate.Stage(
        "coverage",
        (sys.executable, str(_SCRIPT), str(report), "--repo-root", str(tmp_path)),
        False,
        (),
        (),
        measure_memory=True,
    )
    outcome = gate._run_stage(checker)
    capsys.readouterr()

    record: dict = {}
    code = gate.run_profile(
        "product",
        lambda stage: outcome if stage.name == "coverage" else True,
        record=record,
    )

    capsys.readouterr()
    assert code == 1
    assert record["failed_stage"] == "coverage"
    assert record["verdict"] == "GATE FAILED: coverage"
    assert (
        "COVERAGE FLOOR FAILED: branch 70.00000% is more than 0.5 points above its "
        "floor 60.25%: raise the floor to 69.75 "
        "(tool.athena.coverage.branch_floor in pyproject.toml)"
    ) in record["receipt"]
    assert record["coverage"]["branch"] == {
        "covered": 70,
        "total": 100,
        "percent": 70.0,
        "floor": 60.25,
    }


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


def _record_pytest_arguments(tmp_path: Path) -> tuple[Path, Path]:
    # Stand in for the interpreter: record the pytest command line, write nothing.
    record = tmp_path / "arguments.txt"
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\n')
    python.chmod(0o755)
    return python, record


@pytest.mark.parametrize(
    ("workers", "expected"), [("", "4"), ("2", "2")], ids=["default", "override"]
)
def test_wrapper_takes_worker_count_from_environment(tmp_path, workers, expected):
    # Each xdist worker holds its own copy of the suite's memory; a smaller machine
    # must be able to run the same full-suite gate with fewer workers.
    python, record = _record_pytest_arguments(tmp_path)

    result = _run_wrapper(
        str(tmp_path / "evidence"),
        extra_env={"ATHENA_PYTHON": str(python), "ATHENA_COVERAGE_WORKERS": workers},
    )

    arguments = record.read_text().splitlines()
    assert arguments[:3] == ["-m", "pytest", "-q"]
    assert arguments[arguments.index("-n") + 1] == expected
    assert "--cov" in arguments
    # The wrapper still refuses to pass without complete evidence.
    assert result.returncode == 1
    assert "coverage evidence must be a non-empty regular file" in result.stderr


@pytest.mark.parametrize("workers", ["0", "-1", "two", "4 --no-cov", " 4"])
def test_wrapper_rejects_invalid_worker_count(tmp_path, workers):
    python, record = _record_pytest_arguments(tmp_path)
    report_dir = tmp_path / "evidence"

    result = _run_wrapper(
        str(report_dir),
        extra_env={"ATHENA_PYTHON": str(python), "ATHENA_COVERAGE_WORKERS": workers},
    )

    assert result.returncode == 2
    assert "ATHENA_COVERAGE_WORKERS must be a positive integer" in result.stderr
    assert not record.exists()
    assert not report_dir.exists()


# --- documents that restate a floor (G9) -----------------------------------------
#
# The floors live in pyproject.toml's [tool.athena.coverage] and only rise, so a
# figure copied into a document is wrong after the next raise; dated documents
# kept quoting 92.60/82.30/90.30 long after. A document that describes the
# present points at pyproject.toml instead. A dated record (a plan or report under
# docs/plans/, a decision record, a changelog entry, RELEASE_READINESS.md's
# dated evidence, which says so at its top) states what was true on its date
# and keeps its figures.

_REPO = _SCRIPT.parents[1]
_FLOOR_FIGURE = re.compile(
    r"floor[^\n]*?\b(\d{2}\.\d{1,2})\b|\b(\d{2}\.\d{1,2})\b[^\n]*?\bfloor",
    re.IGNORECASE,
)
_DATED = {"CHANGELOG.md", "docs/RELEASE_READINESS.md"}


def _present_tense_documents() -> list[Path]:
    paths = [*_REPO.glob("*.md"), *_REPO.glob(".github/*.md"), *_REPO.glob("docs/*.md")]
    return sorted(
        path for path in paths if path.relative_to(_REPO).as_posix() not in _DATED
    )


def _floor_figures(text: str) -> list[str]:
    """Each figure stated beside the word floor, and each current floor's value."""
    floors = tomllib.loads((_REPO / "pyproject.toml").read_text())["tool"]["athena"][
        "coverage"
    ]
    found = [a or b for a, b in _FLOOR_FIGURE.findall(text)]
    found += [
        f"{floors[key]:.2f}"
        for key in ("line_floor", "branch_floor", "combined_floor")
        if f"{floors[key]:.2f}" in text
    ]
    return found


def test_no_present_tense_document_restates_a_coverage_floor():
    stated = {
        path.relative_to(_REPO).as_posix(): figures
        for path in _present_tense_documents()
        if (figures := _floor_figures(path.read_text(encoding="utf-8")))
    }
    assert stated == {}, (
        "point at [tool.athena.coverage] in pyproject.toml instead of copying a "
        f"floor: {stated}"
    )


def test_the_floor_guard_would_catch_a_copied_floor():
    current = tomllib.loads((_REPO / "pyproject.toml").read_text())["tool"]["athena"][
        "coverage"
    ]["line_floor"]
    assert _floor_figures("Coverage floors never drop (line 92.60 / branch 82.30).")
    assert _floor_figures("Line coverage must stay above 93.86%, the floor.")
    assert _floor_figures(f"The suite measures well over {current:.2f} percent.")
    assert _floor_figures("The floors are in `pyproject.toml`.") == []
    assert any(path.name == "CONTRIBUTING.md" for path in _present_tense_documents())
