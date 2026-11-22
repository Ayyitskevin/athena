"""The gate's JSON receipt says exactly what its printed receipt says.

A pull request carries the receipt as evidence. The JSON form exists so a tool
or a reviewer can read it without parsing prose; if it ever disagreed with the
printed lines, one of them would be lying about what ran.
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "athena_gate_json_under_test", ROOT / "scripts" / "gate.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)

KEYS = {
    "schema",
    "profile",
    "commit",
    "dirty",
    "python",
    "import",
    "stages",
    "stages_run",
    "stages_passed",
    "not_run",
    "ci_only_jobs_not_run",
    "tests",
    "coverage",
    "peak_memory",
    "selection",
    "failed_stage",
    "partial",
    "passed",
    "exit_code",
    "verdict",
    "receipt",
}
RUN_KEYS = KEYS - {"schema", "profile", "commit", "dirty", "python", "import"}
COVERAGE_OUTPUT = (
    "....",
    "5510 passed, 2 warnings in 612.40s (0:10:12)",
    "Coverage inventory: 245 runtime Python files",
    "Line coverage: 9300/10000 = 93.00000% (floor 92.60%)",
    "Branch coverage: 2480/3000 = 82.66667% (floor 82.30%)",
    "Combined coverage: 11780/13000 = 90.61538% (floor 90.30%)",
)


def _measured_outcome(stage):
    if stage.name != "coverage":
        return True
    return gate.StageOutcome(
        True,
        ("PEAK MEMORY: coverage: 1.40 GiB resident across its process tree",),
        tuple(line for line in COVERAGE_OUTPUT if line != "...."),
        {
            "tree_bytes": 1503238553,
            "largest_process_bytes": 268435456,
            "sample_seconds": 0.5,
            "worker_lost": False,
        },
    )


def _closing(output, record):
    # The receipt's closing block is the last len(receipt) printed lines.
    return output.splitlines()[-len(record["receipt"]) :]


def test_product_json_carries_every_fact_the_receipt_prints(capsys):
    record = {}
    assert gate.run_profile("product", _measured_outcome, record=record) == 0
    output = capsys.readouterr().out
    assert set(record) == RUN_KEYS
    assert _closing(output, record) == record["receipt"]
    assert record["receipt"][0] == "STAGES RUN: " + ", ".join(record["stages_run"])
    assert record["receipt"][1] == "STAGES PASSED: " + ", ".join(
        record["stages_passed"]
    )
    assert record["verdict"] == record["receipt"][-1]
    assert record["verdict"].startswith("PRODUCT GATE PASS")
    assert record["stages_run"] == [stage.name for stage in gate.STAGES]
    assert record["not_run"] == []
    assert [stage["name"] for stage in record["stages"]] == record["stages_run"]
    assert all(stage["result"] == "passed" for stage in record["stages"])
    assert all(isinstance(stage["seconds"], float) for stage in record["stages"])
    assert record["tests"] == {"passed": 5510, "warnings": 2}
    assert record["coverage"] == {
        "line": {"covered": 9300, "total": 10000, "percent": 93.0, "floor": 92.6},
        "branch": {
            "covered": 2480,
            "total": 3000,
            "percent": 82.66667,
            "floor": 82.3,
        },
        "combined": {
            "covered": 11780,
            "total": 13000,
            "percent": 90.61538,
            "floor": 90.3,
        },
    }
    assert record["peak_memory"]["coverage"]["tree_bytes"] == 1503238553
    assert "PEAK MEMORY: coverage: 1.40 GiB" in "\n".join(record["receipt"])
    assert record["partial"] is False
    assert (record["passed"], record["exit_code"], record["failed_stage"]) == (
        True,
        0,
        None,
    )
    assert record["selection"] is None


def test_a_failed_stage_is_the_same_failure_in_both_forms(capsys):
    record = {}
    outcomes = {stage.name: stage.name != "typecheck" for stage in gate.STAGES}
    assert gate.run_profile("product", lambda s: outcomes[s.name], record=record) == 1
    output = capsys.readouterr().out
    assert _closing(output, record) == record["receipt"]
    assert record["verdict"] == "GATE FAILED: typecheck"
    assert (record["passed"], record["exit_code"], record["failed_stage"]) == (
        False,
        1,
        "typecheck",
    )
    by_name = {stage["name"]: stage for stage in record["stages"]}
    assert by_name["typecheck"]["result"] == "failed"
    assert by_name["coverage"] == {
        "name": "coverage",
        "result": "not_run",
        "seconds": None,
    }
    assert [f"NOT RUN: {name}" for name in record["not_run"]] == [
        line for line in record["receipt"] if line.startswith("NOT RUN: ")
    ]
    assert record["tests"] is None
    assert record["coverage"] is None


def test_an_unavailable_stage_is_reported_as_unavailable(capsys):
    def missing(stage):
        raise FileNotFoundError("synthetic missing stage")

    record = {}
    assert gate.run_profile("quick", missing, record=record) == 1
    capsys.readouterr()
    lint = next(stage for stage in record["stages"] if stage["name"] == "lint")
    assert (lint["result"], lint["detail"]) == (
        "unavailable",
        "synthetic missing stage",
    )
    assert isinstance(lint["seconds"], float)
    assert record["stages_run"] == []


def test_changed_json_carries_the_selection(capsys):
    plan = gate.select_tests.Plan(
        ("src/athena/core/etag.py",),
        ("tests/test_etag.py",),
        360,
        (),
        (("graph", 1), ("map", 0), ("reference", 0)),
        ("src/athena/core/etag.py: graph 1, map 0, reference 0",),
    )
    diff = gate.select_tests.Diff(
        "origin/main", "origin/main", "abc123", plan.changed, None
    )
    selection = gate.Selection(diff, plan, 1.25, lambda: (12, 5510))
    facts = ("12 passed in 3.10s",)
    record = {}

    def runner(stage):
        if stage.name == "selected-tests":
            return gate.StageOutcome(True, (), facts)
        return True

    assert gate.run_profile("changed", runner, selection=selection, record=record) == 0
    output = capsys.readouterr().out
    assert _closing(output, record) == record["receipt"]
    assert record["partial"] is True
    assert record["verdict"].startswith("CHANGED PROFILE PASS (PARTIAL;")
    assert record["tests"] == {"passed": 12}
    assert record["coverage"] is None
    assert record["not_run"] == ["dependencies", "coverage"]
    assert record["selection"] == {
        "requested_base": "origin/main",
        "base": "origin/main",
        "merge_base": "abc123",
        "base_note": None,
        "changed_paths": ["src/athena/core/etag.py"],
        "test_files": ["tests/test_etag.py"],
        "test_files_total": 360,
        "tests_selected": 12,
        "tests_total": 5510,
        "by_rule": {"graph": 1, "map": 0, "reference": 0},
        "full_suite": [],
        "explanations": ["src/athena/core/etag.py: graph 1, map 0, reference 0"],
        "rule": gate.CHANGED_RULE,
        "coverage_floors": False,
        "seconds": 1.25,
    }
    receipt = "\n".join(record["receipt"])
    assert "SELECTED TESTS: 12 of 5510" in receipt
    assert "CHANGED PATHS: 1: src/athena/core/etag.py" in receipt


@pytest.mark.parametrize(
    ("line", "counts"),
    [
        ("5510 passed in 600.00s (0:10:00)", {"passed": 5510}),
        (
            "===== 3 failed, 5213 passed, 1 skipped in 640.12s (0:10:40) =====",
            {"failed": 3, "passed": 5213, "skipped": 1},
        ),
        ("1 passed, 2 warnings in 0.50s", {"passed": 1, "warnings": 2}),
    ],
)
def test_pytest_summaries_become_counts(line, counts):
    assert gate._test_counts([line]) == counts


def test_measured_stage_output_keeps_the_numbers_the_json_needs():
    script = "\n".join(f"print({line!r})" for line in COVERAGE_OUTPUT)
    run = gate._run_measured((sys.executable, "-c", script), dict(os.environ))
    assert run.returncode == 0
    assert run.facts == COVERAGE_OUTPUT[1:2] + COVERAGE_OUTPUT[3:]


def test_cli_writes_the_json_receipt_beside_the_printed_one(
    monkeypatch, capsys, tmp_path
):
    # Real main(), real stage runner, synthetic stages: nothing slow runs.
    stages = (
        gate.Stage("fast", (sys.executable, "-c", "pass"), True, (), ()),
        gate.Stage(
            "coverage",
            (
                sys.executable,
                "-c",
                "\n".join(f"print({line!r})" for line in COVERAGE_OUTPUT),
            ),
            False,
            (),
            (),
            measure_memory=True,
        ),
    )
    monkeypatch.setattr(gate, "STAGES", stages)
    target = tmp_path / "receipt.json"
    monkeypatch.setattr(sys, "argv", ["scripts/gate.py", "--json", str(target)])
    assert gate.main() == 0
    output = capsys.readouterr().out
    record = json.loads(target.read_text())
    assert set(record) == KEYS
    assert record["schema"] == "athena-gate-receipt/1"
    assert record["profile"] == "product"
    assert f"COMMIT: {record['commit']} (worktree " in output
    assert ("worktree dirty" in output) == record["dirty"]
    assert f"PYTHON: {record['python']['executable']} (" in output
    assert f"IMPORT: {record['import']}\n" in output
    assert _closing(output, record) == record["receipt"]
    assert record["tests"] == {"passed": 5510, "warnings": 2}
    assert record["coverage"]["combined"]["percent"] == 90.61538
    assert set(record["peak_memory"]) == {"coverage"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("-", "--json needs a file path"),
        ("no-such-directory/receipt.json", "--json: no directory for"),
    ],
)
def test_cli_refuses_a_json_target_it_cannot_write(monkeypatch, capsys, value, message):
    monkeypatch.setattr(sys, "argv", ["scripts/gate.py", "--json", value])
    with pytest.raises(SystemExit) as raised:
        gate.main()
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_a_refused_gate_still_writes_a_failing_json_receipt(tmp_path):
    foreign = tmp_path / "athena"
    foreign.mkdir()
    (foreign / "__init__.py").write_text("# foreign package\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    target = tmp_path / "receipt.json"
    result = subprocess.run(
        ["bash", "scripts/gate.sh", "--profile", "quick", "--json", str(target)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    record = json.loads(target.read_text())
    assert set(record) == KEYS
    assert record["verdict"] == "GATE FAILED: Athena import is outside this checkout"
    assert record["passed"] is False
    assert record["stages_run"] == []
    assert record["not_run"] == [stage.name for stage in gate.STAGES]
    assert result.stdout.splitlines()[-len(record["receipt"]) :] == record["receipt"]


def test_pull_request_template_asks_for_the_gate_receipt():
    template = (ROOT / ".github" / "pull_request_template.md").read_text()
    headings = [line for line in template.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Outcome",
        "## Why",
        "## Boundaries",
        "## Verification",
        "## Risk and rollback",
        "## AI assistance",
    ]
    verification = template.split("## Verification", 1)[1].split("## ", 1)[0]
    assert "scripts/gate.sh" in verification
    assert "--json" in verification
    for asked in ("profile", "commit", "closing lines", "PRODUCT GATE PASS", "PARTIAL"):
        assert asked in verification
    # Running the app is still part of done; the gate does not do it.
    assert "Ran it" in verification
    # The gate replaced these separate asks; the template must not bring them back.
    for retired in ("`ruff check .`", "`pytest -q`", "`python scripts/smoke_app.py`"):
        assert retired not in template
