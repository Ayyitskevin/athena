"""Gate receipts must distinguish a complete product check from a partial run."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gate.py"
_SPEC = importlib.util.spec_from_file_location("athena_gate", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)


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


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ((), "product"),
        (("--profile", "quick"), "quick"),
        (("--profile", "product"), "product"),
    ],
    ids=["bare-default", "explicit-quick", "explicit-product"],
)
def test_cli_defaults_to_product_with_explicit_quick_option(
    monkeypatch, capsys, arguments, expected
):
    # Inspect CLI dispatch without executing the real product/full-suite stages.
    called = []
    monkeypatch.setattr(sys, "argv", ["scripts/gate.py", *arguments])
    monkeypatch.setattr(
        gate, "run_profile", lambda profile: called.append(profile) or 0
    )
    assert gate.main() == 0
    assert called == [expected]
    assert f"PROFILE: {expected}" in capsys.readouterr().out


def test_every_profile_runs_the_command_debt_ratchet():
    # G1: the two command-migration debts only go down, and only a check the gate
    # runs can say so. The stage is quick, so the quick and changed profiles run
    # it as well as the product profile, beside the other static checks.
    stage = next(stage for stage in gate.STAGES if stage.name == "command-debts")
    assert stage.command[1:] == ("scripts/generate_command_debtors.py", "--check")
    assert stage.ci_commands == (
        ".venv/bin/python scripts/generate_command_debtors.py --check",
    )
    for profile in ("quick", "product"):
        called = []
        gate.run_profile(profile, lambda stage: called.append(stage.name) or True)
        assert "command-debts" in called, profile


def test_ci_check_inventory_cannot_drift():
    _assert_ci_check_inventory(Path(".github/workflows/ci.yml").read_text())


def _assert_ci_check_inventory(workflow_text):
    workflow = yaml.safe_load(workflow_text)
    for job_name, job in workflow["jobs"].items():
        named = [step["name"] for step in job.get("steps", []) if "name" in step]
        assert len(named) == len(set(named)), (job_name, "duplicate step names")
        for step in job.get("steps", []):
            if _executable_lines(step.get("run", "")):
                assert step.get("name"), (job_name, "unnamed run step")
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


def _executable_lines(run_text):
    # Compare physical command lines; do not interpret shell syntax or quoting.
    commands = []
    for line in run_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            commands.append(line)
    return tuple(commands)


def _assert_ci_stage_commands(workflow_text):
    workflow = yaml.safe_load(workflow_text)
    steps = {
        step["name"]: step
        for step in workflow["jobs"]["coverage"]["steps"]
        if "name" in step
    }
    for stage in gate.STAGES:
        for name, expected in zip(stage.ci_steps, stage.ci_commands, strict=True):
            assert _executable_lines(steps[name].get("run", "")) == _executable_lines(
                expected
            ), (stage.name, name)
    combine = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name")
        == "Verify exact test inventory and combined coverage floors"
    )
    assert _executable_lines(combine["run"]) == _executable_lines(
        gate.CI_COMBINE_COMMAND
    )


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


@pytest.mark.parametrize(
    "arguments", [(), ("--profile", "quick"), ("--profile", "product")]
)
def test_wrapper_uses_checkout_python_and_forwards_arguments(tmp_path, arguments):
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
        ["bash", str(scripts / "gate.sh"), *arguments],
        cwd=tmp_path.parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout
        == "synthetic gate:" + (" " + " ".join(arguments) if arguments else "") + "\n"
    )


@pytest.fixture
def synthetic_ci_workflow(monkeypatch):
    # Exercise the real assertions without editing or copying the real workflow.
    lint = next(stage for stage in gate.STAGES if stage.name == "lint")
    monkeypatch.setattr(gate, "STAGES", (lint,))
    return """\
jobs:
  coverage:
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
      - name: Lint
        run: .venv/bin/python -m ruff check .
  test:
    steps:
      - name: Verify exact test inventory and combined coverage floors
        run: .venv/bin/python scripts/coverage_shards.py combine "$RUNNER_TEMP/athena-shards" "$RUNNER_TEMP/athena-coverage"
  container: {}
  audit: {}
"""


def test_synthetic_ci_action_bumps_and_comment_lines_preserve_commands(
    synthetic_ci_workflow,
):
    workflow = synthetic_ci_workflow
    _assert_ci_check_inventory(workflow)
    _assert_ci_stage_commands(workflow)

    bumped = workflow.replace("1" * 40, "2" * 40)
    assert bumped != workflow
    _assert_ci_check_inventory(bumped)
    _assert_ci_stage_commands(bumped)

    formatted = bumped.replace(
        "run: .venv/bin/python -m ruff check .",
        "run: |\n          # Run lint\n\n          .venv/bin/python -m ruff check .\n",
    )
    assert formatted != bumped
    _assert_ci_check_inventory(formatted)
    _assert_ci_stage_commands(formatted)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "# formerly: .venv/bin/python -m ruff check .",
        ".venv/bin/python -m ruff --version",
        "# formerly: .venv/bin/python -m ruff check .\n"
        ".venv/bin/python -m ruff --version",
        ".venv/bin/python -m ruff check .\necho added-command",
    ],
    ids=["removed", "commented-out", "replaced", "commented-plus-version", "added"],
)
def test_synthetic_ci_rejects_mapped_command_drift(synthetic_ci_workflow, body):
    workflow = yaml.safe_load(synthetic_ci_workflow)
    workflow["jobs"]["coverage"]["steps"][1]["run"] = body
    changed = yaml.safe_dump(workflow)
    _assert_ci_check_inventory(changed)
    with pytest.raises(AssertionError):
        _assert_ci_stage_commands(changed)


@pytest.mark.parametrize("job", ["coverage", "test", "container", "audit"])
def test_synthetic_ci_rejects_added_unnamed_command(synthetic_ci_workflow, job):
    workflow = yaml.safe_load(synthetic_ci_workflow)
    workflow["jobs"][job].setdefault("steps", []).append({"run": "echo added-command"})
    with pytest.raises(AssertionError):
        _assert_ci_check_inventory(yaml.safe_dump(workflow))


def test_synthetic_ci_rejects_added_named_step(synthetic_ci_workflow):
    workflow = yaml.safe_load(synthetic_ci_workflow)
    workflow["jobs"]["coverage"]["steps"].append(
        {"name": "Extra check", "run": "echo added-command"}
    )
    with pytest.raises(AssertionError):
        _assert_ci_check_inventory(yaml.safe_dump(workflow))


def test_synthetic_ci_rejects_duplicate_mapped_step(synthetic_ci_workflow):
    workflow = yaml.safe_load(synthetic_ci_workflow)
    workflow["jobs"]["coverage"]["steps"].insert(
        1, {"name": "Lint", "run": ".venv/bin/python -m ruff --version"}
    )
    with pytest.raises(AssertionError):
        _assert_ci_check_inventory(yaml.safe_dump(workflow))


def test_synthetic_ci_rejects_added_combine_command(synthetic_ci_workflow):
    workflow = yaml.safe_load(synthetic_ci_workflow)
    workflow["jobs"]["test"]["steps"][0]["run"] += "\necho added-command"
    with pytest.raises(AssertionError):
        _assert_ci_stage_commands(yaml.safe_dump(workflow))


def _child_env():
    # A nested run must not inherit this run's pytest options, xdist or coverage.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_", "COVERAGE_"))
    }


def test_only_the_coverage_stage_is_memory_measured():
    # The full suite is the stage that can exhaust memory; its receipt must say how
    # much it used so "does the gate fit this machine?" has a measured answer.
    assert [stage.name for stage in gate.STAGES if stage.measure_memory] == ["coverage"]


def test_process_tree_memory_counts_every_descendant(tmp_path):
    # The coverage stage is bash -> pytest -> xdist workers; the memory that kills a
    # machine is the sum over that tree, not the wrapper's own small footprint.
    grandchild = (
        "import sys\n"
        "held = b'x' * (80 << 20)\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
    )
    child = (
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, '-c', {grandchild!r}], check=True)\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", child],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout.readline() == "ready\n"
        total, largest = gate._process_tree_rss(process.pid)
        process.stdin.close()
    assert largest >= 80 << 20
    assert total > largest


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        (
            "print('[gw1] node down: Not properly terminated')\n"
            "print(\"worker 'gw1' crashed while running 'tests/test_x.py::test_y'\")\n"
            "raise SystemExit(1)\n",
            "WORKER TERMINATED: a test worker was terminated",
        ),
        (
            "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n",
            "STAGE TERMINATED: the measured run was terminated by SIGKILL.",
        ),
        (
            # bash reports a child pytest killed by SIGKILL as exit status 137.
            "raise SystemExit(137)\n",
            "STAGE TERMINATED: the measured run was terminated by SIGKILL.",
        ),
    ],
    ids=["xdist-worker-lost", "killed", "bash-reported-kill"],
)
def test_terminated_stage_names_memory_and_keeps_failure(
    monkeypatch, capsys, script, reason
):
    stage = gate.Stage(
        "measured", (sys.executable, "-c", script), True, (), (), measure_memory=True
    )
    monkeypatch.setattr(gate, "STAGES", (stage,))
    assert gate.run_profile("product", gate._run_stage) == 1
    output = capsys.readouterr().out
    assert reason in output
    assert "usually means the machine ran out of memory" in output
    assert "ATHENA_COVERAGE_WORKERS=2 scripts/gate.sh" in output
    assert "PEAK MEMORY: measured: " in output
    assert "FAILED: measured" in output
    assert output.rstrip().endswith("GATE FAILED: measured")
    assert "PRODUCT GATE PASS" not in output


@pytest.mark.parametrize("returncode", [0, 1], ids=["passed", "ordinary-failure"])
def test_measured_stage_reports_peak_without_blaming_memory(
    monkeypatch, capsys, returncode
):
    stage = gate.Stage(
        "measured",
        (
            sys.executable,
            "-c",
            f"print('stage output')\nraise SystemExit({returncode})",
        ),
        True,
        (),
        (),
        measure_memory=True,
    )
    monkeypatch.setattr(gate, "STAGES", (stage,))
    assert gate.run_profile("product", gate._run_stage) == returncode
    output = capsys.readouterr().out
    assert "stage output\n" in output  # The stage's own output is still shown.
    assert "PEAK MEMORY: measured: " in output
    assert "resident across its process tree" in output
    assert "TERMINATED" not in output
    assert ("PRODUCT GATE PASS" in output) == (returncode == 0)


def test_real_xdist_worker_death_is_recognized(tmp_path):
    # Pins the markers to what the installed pytest-xdist actually prints, so an
    # upgrade that rewords them fails here instead of silently dropping the hint.
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_dies.py").write_text(
        "import os, signal\n"
        "def test_ok():\n    pass\n"
        "def test_killed():\n    os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n",
        "2",
        "-p",
        "no:cacheprovider",
        "-c",
        str(tmp_path / "pytest.ini"),
        "--rootdir",
        str(tmp_path),
        str(tmp_path / "test_dies.py"),
    )
    run = gate._run_measured(command, _child_env())
    assert run.returncode == 1
    assert run.worker_lost
    assert "WORKER TERMINATED" in "\n".join(gate._memory_receipt("coverage", run))
