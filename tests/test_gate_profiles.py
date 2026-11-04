"""Gate receipts must distinguish a complete product check from a partial run."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import gate


def test_required_failure_never_claims_full_pass(capsys):
    outcomes = {stage.name: True for stage in gate.STAGES}
    outcomes[gate.STAGES[1].name] = False
    assert gate.run_profile("product", lambda stage: outcomes[stage.name]) == 1
    output = capsys.readouterr().out
    assert "PRODUCT GATE PASS" not in output
    assert f"FAILED: {gate.STAGES[1].name}" in output
    assert "NOT RUN:" in output
    assert f"NOT RUN: {gate.STAGES[1].name}\n" not in output


def test_partial_profile_reports_absent_stages(capsys):
    assert gate.run_profile("quick", lambda stage: True) == 0
    output = capsys.readouterr().out
    assert "PRODUCT GATE PASS" not in output
    assert "PARTIAL" in output
    assert "NOT RUN: typecheck" in output
    assert "NOT RUN: coverage" in output


def test_product_pass_requires_every_stage(capsys):
    called = []
    assert (
        gate.run_profile("product", lambda stage: called.append(stage.name) or True)
        == 0
    )
    assert called == [stage.name for stage in gate.STAGES]
    assert "PRODUCT GATE PASS" in capsys.readouterr().out


def test_ci_check_inventory_cannot_drift():
    _assert_ci_check_inventory(Path(".github/workflows/ci.yml").read_text())


def _assert_ci_check_inventory(workflow_text):
    workflow = yaml.safe_load(workflow_text)
    coverage_steps = workflow["jobs"]["coverage"]["steps"]
    # Setup and runner telemetry are not checks; every other named coverage step
    # must be represented in the gate inventory or this test fails.
    setup = {
        "Create isolated test environment",
        "Install hash-verified package installer",
        "Install test extras (including MCP)",
        "Record runner resources",
        "Upload complete shard evidence",
    }
    actual = {step["name"] for step in coverage_steps if "name" in step} - setup
    assert actual == {name for stage in gate.STAGES for name in stage.ci_steps}
    combine = workflow["jobs"]["test"]["steps"]
    assert any(
        step.get("name") == "Verify exact test inventory and combined coverage floors"
        for step in combine
    )
    assert set(workflow["jobs"]) == {"coverage", "test", "container", "audit"}


def test_missing_executable_is_not_reported_as_run(capsys):
    def missing(stage):
        raise FileNotFoundError("synthetic missing stage")

    assert gate.run_profile("product", missing) == 1
    output = capsys.readouterr().out
    assert "STAGES RUN: none" in output
    assert f"NOT RUN: {gate.STAGES[0].name}" in output
    assert "PRODUCT GATE PASS" not in output


def test_ci_stage_commands_match_inventory():
    _assert_ci_stage_commands(Path(".github/workflows/ci.yml").read_text())


def _assert_ci_stage_commands(workflow_text):
    workflow = yaml.safe_load(workflow_text)
    steps = {
        step["name"]: step
        for step in workflow["jobs"]["coverage"]["steps"]
        if "name" in step
    }
    for stage in gate.STAGES:
        commands = "\n".join(steps[name].get("run", "") for name in stage.ci_steps)
        for fragment in stage.ci_fragments:
            assert fragment in commands, (stage.name, fragment)
    combine = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name")
        == "Verify exact test inventory and combined coverage floors"
    )
    assert "scripts/coverage_shards.py combine" in combine["run"]


def test_real_stage_runner_handles_failure_and_missing_command(
    monkeypatch, tmp_path, capsys
):
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.write_text("#!/bin/sh\nexit 0\n")
    bad.write_text("#!/bin/sh\nexit 7\n")
    good.chmod(0o755)
    bad.chmod(0o755)
    stages = (
        gate.Stage("good", (str(good),), True, (), ()),
        gate.Stage("bad", (str(bad),), True, (), ()),
        gate.Stage("missing", (str(tmp_path / "absent"),), True, (), ()),
    )
    monkeypatch.setattr(gate, "STAGES", stages)
    assert gate.run_profile("product", gate._run_stage) == 1
    output = capsys.readouterr().out
    assert "FAILED: bad" in output
    assert "NOT RUN: missing" in output
    assert "PRODUCT GATE PASS" not in output

    monkeypatch.setattr(gate, "STAGES", (stages[0], stages[2]))
    assert gate.run_profile("product", gate._run_stage) == 1
    output = capsys.readouterr().out
    assert "STAGES RUN: good\n" in output
    assert "NOT RUN: missing" in output
    assert "PRODUCT GATE PASS" not in output

    monkeypatch.setattr(gate, "STAGES", (stages[0],))
    assert gate.run_profile("product", gate._run_stage) == 0
    assert "PRODUCT GATE PASS" in capsys.readouterr().out


def test_wrapper_refuses_import_from_another_checkout(tmp_path):
    foreign = tmp_path / "athena"
    foreign.mkdir()
    (foreign / "__init__.py").write_text("# foreign package\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        ["bash", "scripts/gate.sh"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "GATE FAILED: Athena import is outside this checkout" in result.stdout
    assert "NOT RUN: coverage" in result.stdout
    assert "GATE PASS" not in result.stdout


@pytest.mark.parametrize("broken_python", [False, True], ids=["absent", "broken"])
def test_wrapper_explains_missing_development_environment(tmp_path, broken_python):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gate.sh").write_text(Path("scripts/gate.sh").read_text())
    if broken_python:
        python = tmp_path / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(tmp_path / "missing-python")
    result = subprocess.run(
        ["bash", str(scripts / "gate.sh")],
        cwd=tmp_path.parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "GATE FAILED: .venv/bin/python is unavailable" in result.stderr
    assert "CONTRIBUTING.md" in result.stderr
    assert "No such file" not in result.stderr
    assert "GATE PASS" not in result.stdout


def test_wrapper_uses_checkout_python_and_forwards_arguments(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gate.sh").write_text(Path("scripts/gate.sh").read_text())
    (scripts / "gate.py").write_text(
        "import sys\nprint('synthetic gate:', *sys.argv[1:])\n"
    )
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    result = subprocess.run(
        ["bash", str(scripts / "gate.sh"), "--profile", "quick"],
        cwd=tmp_path.parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "synthetic gate: --profile quick\n"


def test_synthetic_ci_allows_action_bumps_but_rejects_command_drift(monkeypatch):
    # Exercise the same assertions as the real workflow checks without editing CI.
    lint = next(stage for stage in gate.STAGES if stage.name == "lint")
    monkeypatch.setattr(gate, "STAGES", (lint,))
    workflow = """\
jobs:
  coverage:
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
      - name: Lint
        run: .venv/bin/python -m ruff check .
  test:
    steps:
      - name: Verify exact test inventory and combined coverage floors
        run: .venv/bin/python scripts/coverage_shards.py combine synthetic
  container: {}
  audit: {}
"""
    _assert_ci_check_inventory(workflow)
    _assert_ci_stage_commands(workflow)

    bumped = workflow.replace("1" * 40, "2" * 40)
    assert bumped != workflow
    _assert_ci_check_inventory(bumped)
    _assert_ci_stage_commands(bumped)

    changed_command = bumped.replace("-m ruff check .", "-m ruff --version")
    assert changed_command != bumped
    _assert_ci_check_inventory(changed_command)
    with pytest.raises(AssertionError, match="lint"):
        _assert_ci_stage_commands(changed_command)
