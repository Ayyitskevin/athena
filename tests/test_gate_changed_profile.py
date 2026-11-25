"""The changed profile's receipt can never be mistaken for product evidence.

`scripts/gate.sh --profile changed` is the inner loop: static stages, the type
check, and the tests selected from the diff, without coverage floors. Its
receipt must say PARTIAL, list what it selected and why, and keep the quick and
product receipts exactly as they were.
"""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "athena_gate_changed_under_test", ROOT / "scripts" / "gate.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)
Diff = gate.select_tests.Diff
Plan = gate.select_tests.Plan

CHANGED_STAGES = [
    "lint",
    "format",
    "typecheck",
    "imports",
    "ownership",
    "transactions",
    "imported-at",
    "styles",
    "routes",
    "surface-parity",
    "surface-counts",
    "selected-tests",
]
RULE = (
    "SELECTION RULE: imports are followed through src/athena except the "
    "composition roots athena.main, athena.web, athena.web.router; a test that "
    "reaches a module only through the app is selected only when the map names "
    "it, and the map matches test files by name"
)
COVERAGE = (
    "COVERAGE: not measured; selected tests run without the coverage floors, "
    "which a partial run cannot meet"
)


def selection(test_files=("tests/test_a.py", "tests/test_b.py"), full_suite=()):
    changed = tuple(f"src/athena/m{n:02}.py" for n in range(12))
    plan = Plan(
        changed,
        test_files,
        50,
        full_suite,
        (("graph", 2), ("map", 1), ("reference", 0)),
        tuple(f"{path}: graph 1, map 0, reference 0" for path in changed),
    )
    diff = Diff("origin/main", "origin/main", "abc123", changed, None)
    return gate.Selection(diff, plan, 0.5, lambda: (40, 900))


def test_changed_receipt_is_partial_and_lists_its_selection(capsys):
    called = []
    code = gate.run_profile(
        "changed",
        lambda stage: called.append(stage.name) or True,
        selection=selection(),
    )
    assert code == 0
    assert called == CHANGED_STAGES
    output = capsys.readouterr().out
    closing = output[output.index("STAGES RUN:") :].splitlines()
    assert closing == [
        "STAGES RUN: " + ", ".join(CHANGED_STAGES),
        "STAGES PASSED: " + ", ".join(CHANGED_STAGES),
        "NOT RUN: dependencies",
        "NOT RUN: coverage",
        "CI-ONLY JOBS NOT RUN: container, audit (release-candidate supply-chain evidence)",
        "BASE: origin/main (merge base abc123; changed paths run from it to the "
        "working tree, uncommitted and untracked files included)",
        "CHANGED PATHS: 12: "
        + ", ".join(f"src/athena/m{n:02}.py" for n in range(10))
        + " (and 2 more)",
        "SELECTED TEST FILES: 2 of 50 (by graph 2, map 1, reference 0; "
        "a file can be selected by more than one rule)",
        "SELECTED TESTS: 40 of 900",
        RULE,
        COVERAGE,
        "CHANGED PROFILE PASS (PARTIAL; selected tests only, without coverage "
        "floors; not product evidence)",
    ]
    assert "PRODUCT GATE PASS" not in output
    assert "==> selected-tests: " in output
    assert output.count("<2 selected test files>") == 1


def test_a_safety_valve_runs_the_whole_suite_and_names_the_path(capsys):
    commands = {}
    plan_tests = tuple(f"tests/test_{n}.py" for n in range(50))
    chosen = selection(
        plan_tests, ("pyproject.toml: safety valve (project configuration)",)
    )
    assert (
        gate.run_profile(
            "changed",
            lambda stage: commands.setdefault(stage.name, stage.command) or True,
            selection=chosen,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "SELECTED TEST FILES: 50 of 50 (FULL SUITE)\n" in output
    assert (
        "FULL SUITE: pyproject.toml: safety valve (project configuration)\n" in output
    )
    assert "(full suite)" in output
    # No file arguments: pytest runs its configured testpaths, the whole suite.
    assert not any(part.startswith("tests/") for part in commands["selected-tests"])


def test_nothing_selected_runs_no_tests_and_says_so(capsys):
    called = []
    assert (
        gate.run_profile(
            "changed",
            lambda stage: called.append(stage.name) or True,
            selection=selection(test_files=()),
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "selected-tests" not in called
    assert "NOT RUN: selected-tests\n" in output
    assert "SELECTED TEST FILES: 0 of 50" in output
    assert "CHANGED PROFILE PASS (PARTIAL" in output


def test_a_failed_selected_test_fails_the_gate(capsys):
    outcomes = {name: name != "selected-tests" for name in CHANGED_STAGES}
    assert (
        gate.run_profile(
            "changed", lambda stage: outcomes[stage.name], selection=selection()
        )
        == 1
    )
    output = capsys.readouterr().out
    assert output.rstrip().endswith("GATE FAILED: selected-tests")
    assert "PASS" not in output.replace("STAGES PASSED", "")


def test_uncounted_tests_are_reported_not_guessed(capsys):
    chosen = selection()
    chosen = gate.Selection(chosen.diff, chosen.plan, 0.5, lambda: (None, None))
    gate.run_profile("changed", lambda stage: True, selection=chosen)
    assert (
        "SELECTED TESTS: not counted (collecting the suite failed)"
        in capsys.readouterr().out
    )


def test_selected_tests_stage_uses_the_suite_worker_count(monkeypatch):
    monkeypatch.setenv(gate.WORKERS_VARIABLE, "2")
    stage = gate._selected_tests_stage(selection())
    assert stage.command == (
        gate.PYTHON,
        "-m",
        "pytest",
        "-q",
        "-n",
        "2",
        "-ra",
        "--strict-markers",
        "tests/test_a.py",
        "tests/test_b.py",
    )
    assert "--cov" not in " ".join(stage.command)
    assert stage.measure_memory


def test_the_changed_profile_requires_a_selection():
    with pytest.raises(ValueError):
        gate.run_profile("changed", lambda stage: True)


def test_quick_receipt_is_unchanged(capsys):
    assert gate.run_profile("quick", lambda stage: True) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[-7:] == [
        "STAGES RUN: lint, format, imports, ownership, transactions, imported-at, "
        "styles, routes, surface-parity, surface-counts",
        "STAGES PASSED: lint, format, imports, ownership, transactions, "
        "imported-at, styles, routes, surface-parity, surface-counts",
        "NOT RUN: dependencies",
        "NOT RUN: typecheck",
        "NOT RUN: coverage",
        "CI-ONLY JOBS NOT RUN: container, audit (release-candidate supply-chain evidence)",
        "QUICK PROFILE PASS (PARTIAL; product and CI checks not all run)",
    ]
    assert "selected-tests" not in output
    assert "SELECTION" not in output


def test_product_receipt_never_mentions_a_selection(capsys):
    assert gate.run_profile("product", lambda stage: True) == 0
    output = capsys.readouterr().out
    assert "selected-tests" not in output
    assert "SELECTED" not in output
    assert output.rstrip().endswith(
        "PRODUCT GATE PASS (local product stages only; CI-only jobs not run)"
    )


def test_base_is_refused_outside_the_changed_profile(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["scripts/gate.py", "--profile", "quick", "--base", "main"]
    )
    with pytest.raises(SystemExit) as raised:
        gate.main()
    assert raised.value.code == 2
    assert "--base applies only to --profile changed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "base"),
    [((), None), (("--base", "HEAD~1"), "HEAD~1")],
    ids=["default-base", "explicit-base"],
)
def test_cli_dispatches_the_changed_profile_with_its_selection(
    monkeypatch, capsys, arguments, base
):
    chosen = selection()
    asked = []
    ran = []
    monkeypatch.setattr(
        sys, "argv", ["scripts/gate.py", "--profile", "changed", *arguments]
    )
    monkeypatch.setattr(gate, "select_changed", lambda b: asked.append(b) or chosen)
    monkeypatch.setattr(
        gate,
        "run_profile",
        lambda profile, *, selection: ran.append((profile, selection)) or 0,
    )
    assert gate.main() == 0
    assert asked == [base]
    assert ran == [("changed", chosen)]
    output = capsys.readouterr().out
    assert "PROFILE: changed\n" in output
    assert "SELECTION: 2 of 50 test files from 12 changed paths" in output
    assert "SELECTION PATH: src/athena/m00.py: graph 1, map 0, reference 0" in output


def test_an_unusable_base_fails_the_gate_before_any_stage(monkeypatch, capsys):
    def refuse(base):
        raise gate.select_tests.SelectionError("base 'nope' is not a commit")

    monkeypatch.setattr(
        sys, "argv", ["scripts/gate.py", "--profile", "changed", "--base", "nope"]
    )
    monkeypatch.setattr(gate, "select_changed", refuse)
    assert gate.main() == 1
    output = capsys.readouterr().out
    assert "GATE FAILED: base 'nope' is not a commit\n" in output
    assert "NOT RUN: selected-tests\n" in output
    assert "NOT RUN: lint\n" in output
    assert "PASS" not in output


# --- end to end, on a miniature checkout ------------------------------------


def _git(repo, *arguments):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=gate test",
            "-c",
            "user.email=gate-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _child_env():
    # A nested run must not inherit this run's pytest options, xdist or coverage.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_", "COVERAGE_"))
    }


@pytest.fixture
def miniature(tmp_path):
    """A checkout with the real gate scripts and a two-module package."""
    repo = tmp_path / "checkout"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("gate.sh", "gate.py", "select_tests.py"):
        shutil.copy(ROOT / "scripts" / name, scripts / name)
    for stage in gate.STAGES:
        # The contract checkers belong to the real tree; here each just passes.
        if stage.command[1:2] and stage.command[1].startswith("scripts/check_"):
            (repo / stage.command[1]).write_text("print('synthetic check')\n")
    # The test interpreter, by way of the path the wrapper requires.
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    files = {
        ".gitignore": "/.venv/\n__pycache__/\n.*_cache/\n",
        "pyproject.toml": (
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n'
            # The real tree lints the copied gate scripts under its own rules.
            '[tool.ruff]\nextend-exclude = ["scripts"]\n'
        ),
        "src/athena/__init__.py": "",
        "src/athena/alpha.py": "def value() -> int:\n    return 1\n",
        "src/athena/beta.py": "def value() -> int:\n    return 2\n",
        "tests/test_alpha.py": (
            "from athena import alpha\n\n\n"
            "def test_alpha() -> None:\n    assert alpha.value() == 1\n\n\n"
            "def test_alpha_again() -> None:\n    assert alpha.value() == 1\n"
        ),
        # Fails if it runs: the selection must leave it out.
        "tests/test_beta.py": (
            "from athena import beta\n\n\n"
            "def test_beta() -> None:\n    assert beta.value() == 3\n"
        ),
    }
    for relative, text in files.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "miniature")
    return repo


def test_changed_profile_end_to_end_runs_only_the_selected_tests(miniature):
    (miniature / "src/athena/alpha.py").write_text(
        "def value() -> int:\n    return 0 + 1\n"
    )
    env = _child_env()
    env["PYTHONPATH"] = str(miniature / "src")
    env[gate.WORKERS_VARIABLE] = "1"
    result = subprocess.run(
        ["bash", "scripts/gate.sh", "--profile", "changed"],
        cwd=miniature,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout
    assert "PROFILE: changed\n" in output
    # The default base, origin/main, does not exist here: it falls back to main.
    assert "BASE FALLBACK: origin/main not found; using local main\n" in output
    assert "CHANGED PATHS: 1: src/athena/alpha.py\n" in output
    assert "SELECTED TEST FILES: 1 of 2 (by graph 1, map 0, reference 0;" in output
    assert "SELECTED TESTS: 2 of 3\n" in output
    assert "STAGES RUN: " + ", ".join(CHANGED_STAGES) + "\n" in output
    assert "2 passed" in output  # test_beta, which would fail, did not run.
    assert "PEAK MEMORY: selected-tests: " in output
    assert output.rstrip().endswith(
        "CHANGED PROFILE PASS (PARTIAL; selected tests only, without coverage "
        "floors; not product evidence)"
    )
