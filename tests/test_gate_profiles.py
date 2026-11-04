"""Gate receipts must distinguish a complete product check from a partial run."""

import hashlib
import os
import subprocess
from pathlib import Path

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
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
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
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
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


def test_ci_workflow_change_requires_inventory_review():
    workflow = Path(".github/workflows/ci.yml").read_bytes()
    assert hashlib.sha256(workflow).hexdigest() == gate.CI_WORKFLOW_SHA256
    # Even a change outside the mapped coverage job cannot silently evade review.
    assert (
        hashlib.sha256(workflow + b"\n# changed\n").hexdigest()
        != gate.CI_WORKFLOW_SHA256
    )
