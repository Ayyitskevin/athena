"""Local gate profiles and the CI checks each local stage represents.

A product pass is local evidence, not the container or release-candidate jobs.
The changed profile is the inner loop: the quick stages, the type check and the
tests ``scripts/select_tests.py`` maps to the diff, without coverage floors.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKERS_VARIABLE = "ATHENA_COVERAGE_WORKERS"  # read by scripts/coverage.sh
# What pytest-xdist prints when a worker process dies mid-test (e.g. OOM-killed).
WORKER_LOST_MARKERS = ("node down: Not properly terminated", "crashed while running")
MEMORY_SAMPLE_SECONDS = 0.5
JSON_SCHEMA = "athena-gate-receipt/1"
# pytest's closing summary ("12 passed, 1 skipped in 3.4s"), with or without the
# "=" rule around it, and the coverage lines scripts/check_coverage.py prints.
_TEST_SUMMARY = re.compile(r"^=*\s*((?:\d+ [a-z]+(?:, )?)+) in [\d.]+s\b")
_TEST_COUNT = re.compile(r"(\d+) ([a-z]+)")
_COVERAGE_LINE = re.compile(
    r"^(Line|Branch|Combined) coverage: (\d+)/(\d+) = ([\d.]+)% \(floor ([\d.]+)%\)"
)
# A floor check_coverage.py failed: below it, or outgrown ("raise the floor to X").
_COVERAGE_FLOOR_FAILED = re.compile(r"^Coverage floor failed: (.+)$")
PROFILES = ("quick", "product", "changed")
DEFAULT_BASE = "origin/main"
CHANGED_PATHS_SHOWN = 10  # The receipt lists this many changed paths, then a count.


def _load_selector():
    path = ROOT / "scripts" / "select_tests.py"
    spec = importlib.util.spec_from_file_location("athena_select_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


select_tests = _load_selector()


@dataclass(frozen=True)
class Stage:
    name: str
    command: tuple[str, ...]
    quick: bool
    ci_steps: tuple[str, ...]
    ci_commands: tuple[str, ...]  # Complete run body for each ci_steps entry.
    measure_memory: bool = False  # Report peak memory; explain killed workers.
    summary: str | None = None  # Printed in place of a long command.


@dataclass(frozen=True)
class StageOutcome:
    """A stage result that adds lines to the receipt; plain bools add none."""

    passed: bool
    receipt: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()  # Output lines the JSON receipt reads numbers from.
    memory: dict[str, object] | None = None  # Peak memory, for the JSON receipt.


@dataclass(frozen=True)
class MeasuredRun:
    returncode: int
    peak_tree_bytes: int | None  # None where /proc is unavailable.
    peak_process_bytes: int | None
    worker_lost: bool
    facts: tuple[str, ...] = ()  # Test-summary and coverage lines it printed.


PYTHON = sys.executable
STAGES = (
    Stage(
        "dependencies",
        (PYTHON, "-m", "pip", "check"),
        False,
        ("Verify pinned dependency set",),
        (
            ".venv/bin/python -I -m pip check\n"
            ".venv/bin/python -I -m pip freeze --exclude-editable | "
            "diff -u constraints/ci-py312.txt -",
        ),
    ),
    Stage(
        "lint",
        (PYTHON, "-m", "ruff", "check", "."),
        True,
        ("Lint",),
        (".venv/bin/python -m ruff check .",),
    ),
    Stage(
        "format",
        (PYTHON, "-m", "ruff", "format", "--check", "."),
        True,
        ("Check formatting",),
        (".venv/bin/python -m ruff format --check .",),
    ),
    Stage(
        "typecheck",
        (PYTHON, "-m", "mypy", "src/athena"),
        False,
        ("Type-check every runtime module",),
        (".venv/bin/python -m mypy src/athena",),
    ),
    Stage(
        "imports",
        (PYTHON, "scripts/check_import_contracts.py"),
        True,
        ("Check import contracts",),
        (".venv/bin/python scripts/check_import_contracts.py",),
    ),
    Stage(
        "ownership",
        (PYTHON, "scripts/check_write_ownership.py"),
        True,
        ("Check write ownership",),
        (".venv/bin/python scripts/check_write_ownership.py",),
    ),
    Stage(
        "transactions",
        (PYTHON, "scripts/check_transaction_commits.py"),
        True,
        ("Check transaction commits",),
        (".venv/bin/python scripts/check_transaction_commits.py",),
    ),
    Stage(
        "imported-at",
        (PYTHON, "scripts/check_imported_at_guards.py"),
        True,
        ("Check imported_at guards",),
        (".venv/bin/python scripts/check_imported_at_guards.py",),
    ),
    Stage(
        "styles",
        (PYTHON, "scripts/check_template_styles.py"),
        True,
        ("Check template styles",),
        (".venv/bin/python scripts/check_template_styles.py",),
    ),
    Stage(
        "routes",
        (PYTHON, "scripts/check_template_routes.py"),
        True,
        ("Check template routes",),
        (".venv/bin/python scripts/check_template_routes.py",),
    ),
    Stage(
        "coverage",
        ("bash", "scripts/coverage.sh"),
        False,
        ("Run assigned tests with full-source branch coverage",),
        (
            '/usr/bin/time -v .venv/bin/python scripts/coverage_shards.py run "$SHARD" '
            '"$RUNNER_TEMP/athena-shard"',
        ),
        measure_memory=True,
    ),
)
# The changed profile adds these product stages to the quick ones.
CHANGED_EXTRA_STAGES = ("typecheck",)
SELECTED_TESTS = "selected-tests"
CHANGED_RULE = (
    "imports are followed through src/athena except the composition roots "
    + ", ".join(sorted(select_tests.COMPOSITION_ROOTS))
    + "; a test that reaches a module only through the app is selected only "
    "when the map names it, and the map matches test files by name"
)


@dataclass(frozen=True)
class Selection:
    """What the changed profile selected, and from which diff."""

    diff: select_tests.Diff
    plan: select_tests.Plan
    seconds: float  # Time taken to list the diff and select.
    # (selected, total) tests, from a collection of the suite that runs while the
    # stages do; (None, None) when the collection failed.
    count: Callable[[], tuple[int | None, int | None]]

    @property
    def tests_selected(self) -> int | None:
        return self.count()[0]

    @property
    def tests_total(self) -> int | None:
        return self.count()[1]


CI_COMBINE_COMMAND = (
    ".venv/bin/python scripts/coverage_shards.py combine "
    '"$RUNNER_TEMP/athena-shards" "$RUNNER_TEMP/athena-coverage"'
)


def _run_stage(stage: Stage) -> bool | StageOutcome:
    if stage.name == "dependencies":
        if subprocess.run(stage.command, cwd=ROOT, check=False).returncode:
            return False
        freeze = subprocess.run(
            (PYTHON, "-I", "-m", "pip", "freeze", "--exclude-editable"),
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        if freeze.returncode:
            print(f"pip freeze failed: {freeze.stderr}", file=sys.stderr)
            return False
        expected = (ROOT / "constraints/ci-py312.txt").read_text()
        if freeze.stdout != expected:
            print(
                "".join(
                    difflib.unified_diff(
                        expected.splitlines(keepends=True),
                        freeze.stdout.splitlines(keepends=True),
                        fromfile="constraints/ci-py312.txt",
                        tofile="installed dependencies",
                    )
                ),
                file=sys.stderr,
            )
            return False
        return True
    env = os.environ.copy()
    env["ATHENA_PYTHON"] = PYTHON
    if stage.measure_memory:
        run = _run_measured(stage.command, env)
        return StageOutcome(
            run.returncode == 0,
            _memory_receipt(stage.name, run) + _floor_receipt(run.facts),
            run.facts,
            {
                "tree_bytes": run.peak_tree_bytes,
                "largest_process_bytes": run.peak_process_bytes,
                "sample_seconds": MEMORY_SAMPLE_SECONDS,
                "worker_lost": run.worker_lost,
            },
        )
    return subprocess.run(stage.command, cwd=ROOT, env=env, check=False).returncode == 0


def _process_tree_rss(root: int) -> tuple[int, int]:
    """Return (sum, largest) resident bytes of `root` and all its descendants."""
    children: dict[int, list[int]] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            stat = Path(entry.path, "stat").read_text()
        except OSError:  # The process exited while we looked.
            continue
        parent = int(stat.rpartition(")")[2].split()[1])
        children.setdefault(parent, []).append(int(entry.name))
    page = os.sysconf("SC_PAGE_SIZE")
    total = largest = 0
    pending = [root]
    while pending:
        pid = pending.pop()
        pending.extend(children.get(pid, ()))
        try:
            resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * page
        except (OSError, IndexError, ValueError):
            continue
        total += resident
        largest = max(largest, resident)
    return total, largest


def _run_measured(
    command: tuple[str, ...],
    env: dict[str, str],
    interval: float = MEMORY_SAMPLE_SECONDS,
) -> MeasuredRun:
    """Run `command`, echoing its stdout, sampling its process tree's memory."""
    measurable = Path("/proc/self/statm").exists()
    peak_tree = peak_process = 0
    worker_lost = False
    facts: list[str] = []
    done = threading.Event()
    with subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        errors="replace",
    ) as process:

        def sample() -> None:
            nonlocal peak_tree, peak_process
            while True:
                tree, largest = _process_tree_rss(process.pid)
                peak_tree = max(peak_tree, tree)
                peak_process = max(peak_process, largest)
                if done.wait(interval):
                    return

        sampler = threading.Thread(target=sample, daemon=True)
        if measurable:
            sampler.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                if any(marker in line for marker in WORKER_LOST_MARKERS):
                    worker_lost = True
                if (
                    _TEST_SUMMARY.match(line)
                    or _COVERAGE_LINE.match(line)
                    or _COVERAGE_FLOOR_FAILED.match(line)
                ):
                    facts.append(line.rstrip("\n"))
            returncode = process.wait()
        finally:
            done.set()
            if measurable:
                sampler.join()
    return MeasuredRun(
        returncode,
        peak_tree if measurable else None,
        peak_process if measurable else None,
        worker_lost,
        tuple(facts),
    )


def _gib(size: int) -> str:
    return f"{size / 2**30:.2f} GiB"


def _memory_receipt(name: str, run: MeasuredRun) -> tuple[str, ...]:
    if run.peak_tree_bytes is None or run.peak_process_bytes is None:
        lines = [f"PEAK MEMORY: {name}: not measured (no /proc on this system)"]
    else:
        lines = [
            f"PEAK MEMORY: {name}: {_gib(run.peak_tree_bytes)} resident across its "
            f"process tree (largest process {_gib(run.peak_process_bytes)}; "
            f"sampled every {MEMORY_SAMPLE_SECONDS:g} s)"
        ]
    # bash reports a child killed by signal N as 128 + N; Popen reports -N.
    signal_number = -run.returncode if run.returncode < 0 else run.returncode - 128
    killed = 0 < signal_number < signal.NSIG
    if not run.returncode or not (run.worker_lost or killed):
        return tuple(lines)
    if run.worker_lost:
        what = (
            "WORKER TERMINATED: a test worker was terminated before it finished; "
            "tests reported as 'crashed while running' were running when it died "
            "and are not necessarily the cause."
        )
    else:
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"signal {signal_number}"
        what = f"STAGE TERMINATED: the {name} run was terminated by {signal_name}."
    lines.append(
        f"{what} This usually means the machine ran out of memory. Lower the "
        f"worker count (default 4), for example: {WORKERS_VARIABLE}=2 scripts/gate.sh"
    )
    return tuple(lines)


def _floor_receipt(facts: tuple[str, ...]) -> tuple[str, ...]:
    """Each coverage floor the checker failed, as a receipt line."""
    return tuple(
        f"COVERAGE FLOOR FAILED: {failed.group(1)}"
        for failed in map(_COVERAGE_FLOOR_FAILED.match, facts)
        if failed
    )


def _selected_tests_stage(selection: Selection) -> Stage:
    """Run the selected test files, or the whole suite when a valve fired."""
    files = () if selection.plan.full_suite else selection.plan.test_files
    workers = os.environ.get(WORKERS_VARIABLE, "4")
    command = (PYTHON, "-m", "pytest", "-q", "-n", workers, "-ra", "--strict-markers")
    summary = " ".join(command) + (
        f" <{len(files)} selected test files>" if files else " (full suite)"
    )
    return Stage(
        SELECTED_TESTS,
        (*command, *files),
        False,
        (),
        (),
        measure_memory=True,
        summary=summary,
    )


def _listed(items: tuple[str, ...]) -> str:
    shown = ", ".join(items[:CHANGED_PATHS_SHOWN])
    more = len(items) - CHANGED_PATHS_SHOWN
    return shown + (f" (and {more} more)" if more > 0 else "")


def _selection_receipt(selection: Selection) -> list[str]:
    diff, plan = selection.diff, selection.plan
    if diff.merge_base is None:
        lines = [f"BASE: {diff.requested_base}: no diff ({diff.note})"]
    else:
        lines = [
            f"BASE: {diff.base} (merge base {diff.merge_base}; changed paths run "
            "from it to the working tree, uncommitted and untracked files included)"
        ]
        if diff.note:
            lines.append(f"BASE FALLBACK: {diff.note}")
    lines.append(
        f"CHANGED PATHS: {len(plan.changed)}"
        + (f": {_listed(plan.changed)}" if plan.changed else "")
    )
    count = f"{len(plan.test_files)} of {plan.total_test_files}"
    if plan.full_suite:
        lines.append(f"SELECTED TEST FILES: {count} (FULL SUITE)")
        lines.extend(f"FULL SUITE: {reason}" for reason in plan.full_suite)
    else:
        rules = ", ".join(f"{rule} {files}" for rule, files in plan.by_rule)
        lines.append(
            f"SELECTED TEST FILES: {count} (by {rules}; "
            "a file can be selected by more than one rule)"
        )
    if selection.tests_total is None:
        lines.append("SELECTED TESTS: not counted (collecting the suite failed)")
    else:
        lines.append(
            f"SELECTED TESTS: {selection.tests_selected} of {selection.tests_total}"
        )
    lines.append(f"SELECTION RULE: {CHANGED_RULE}")
    lines.append(
        "COVERAGE: not measured; selected tests run without the coverage floors, "
        "which a partial run cannot meet"
    )
    return lines


def run_profile(
    profile: str,
    runner=_run_stage,
    *,
    selection: Selection | None = None,
    record: dict[str, object] | None = None,
) -> int:
    """Run a profile and print its receipt; fill ``record`` for the JSON receipt."""
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    inventory = STAGES
    if profile == "changed":
        if selection is None:
            raise ValueError("The changed profile needs a selection")
        tests_stage = _selected_tests_stage(selection)
        inventory = (*STAGES, tests_stage)
        selected = [
            stage
            for stage in STAGES
            if stage.quick or stage.name in CHANGED_EXTRA_STAGES
        ]
        if selection.plan.test_files:
            selected.append(tests_stage)
    else:
        selected = [stage for stage in STAGES if profile == "product" or stage.quick]
    ran: list[str] = []
    failed: str | None = None
    attempted: list[str] = []
    receipt: list[str] = []
    results: dict[str, dict[str, object]] = {}
    facts: list[str] = []
    memory: dict[str, object] = {}
    for stage in selected:
        print(
            f"==> {stage.name}: {stage.summary or ' '.join(stage.command)}", flush=True
        )
        started = time.monotonic()
        try:
            outcome = runner(stage)
        except OSError as error:
            print(f"UNAVAILABLE: {stage.name}: {error}", flush=True)
            results[stage.name] = {
                "result": "unavailable",
                "seconds": round(time.monotonic() - started, 3),
                "detail": str(error),
            }
            failed = stage.name
            break
        if isinstance(outcome, StageOutcome):
            passed = outcome.passed
            receipt.extend(outcome.receipt)
            facts.extend(outcome.facts)
            if outcome.memory is not None:
                memory[stage.name] = outcome.memory
        else:
            passed = bool(outcome)
        results[stage.name] = {
            "result": "passed" if passed else "failed",
            "seconds": round(time.monotonic() - started, 3),
        }
        attempted.append(stage.name)
        if not passed:
            failed = stage.name
            print(f"FAILED: {stage.name}", flush=True)
            break
        ran.append(stage.name)
    closing: list[str] = []

    def emit(line: str) -> None:
        print(line)
        closing.append(line)

    emit("STAGES RUN: " + (", ".join(attempted) if attempted else "none"))
    emit("STAGES PASSED: " + (", ".join(ran) if ran else "none"))
    not_run = [stage.name for stage in inventory if stage.name not in attempted]
    for name in not_run:
        emit(f"NOT RUN: {name}")
    emit(
        "CI-ONLY JOBS NOT RUN: container, audit (release-candidate supply-chain evidence)"
    )
    for line in receipt:
        emit(line)
    if selection is not None and profile == "changed":
        for line in _selection_receipt(selection):
            emit(line)
    if failed:
        emit(f"GATE FAILED: {failed}")
    elif profile == "product":
        emit("PRODUCT GATE PASS (local product stages only; CI-only jobs not run)")
    elif profile == "changed":
        emit(
            "CHANGED PROFILE PASS (PARTIAL; selected tests only, without coverage "
            "floors; not product evidence)"
        )
    else:
        emit("QUICK PROFILE PASS (PARTIAL; product and CI checks not all run)")
    if record is not None:
        record.update(
            {
                "stages": [
                    {"name": stage.name, **results.get(stage.name, _NOT_RUN)}
                    for stage in inventory
                ],
                "stages_run": attempted,
                "stages_passed": ran,
                "not_run": not_run,
                "ci_only_jobs_not_run": list(CI_ONLY_JOBS),
                "tests": _test_counts(facts),
                "coverage": _coverage(facts),
                "peak_memory": memory,
                "selection": None if selection is None else _selection_json(selection),
                "failed_stage": failed,
                "partial": profile != "product",
                "passed": failed is None,
                "exit_code": 1 if failed else 0,
                "verdict": closing[-1],
                "receipt": closing,
            }
        )
    return 1 if failed else 0


_NOT_RUN: dict[str, object] = {"result": "not_run", "seconds": None}
CI_ONLY_JOBS = ("container", "audit")


def _test_counts(facts: list[str]) -> dict[str, int] | None:
    """pytest's last closing summary, as counts by outcome."""
    for line in reversed(facts):
        summary = _TEST_SUMMARY.match(line)
        if summary:
            return {
                outcome: int(number)
                for number, outcome in _TEST_COUNT.findall(summary.group(1))
            }
    return None


def _coverage(facts: list[str]) -> dict[str, dict[str, object]] | None:
    found: dict[str, dict[str, object]] = {}
    for line in facts:
        measured = _COVERAGE_LINE.match(line)
        if measured:
            kind, covered, total, percent, floor = measured.groups()
            found[kind.lower()] = {
                "covered": int(covered),
                "total": int(total),
                "percent": float(percent),
                "floor": float(floor),
            }
    return found or None


def _selection_json(selection: Selection) -> dict[str, object]:
    diff, plan = selection.diff, selection.plan
    return {
        "requested_base": diff.requested_base,
        "base": diff.base,
        "merge_base": diff.merge_base,
        "base_note": diff.note,
        "changed_paths": list(plan.changed),
        "test_files": list(plan.test_files),
        "test_files_total": plan.total_test_files,
        "tests_selected": selection.tests_selected,
        "tests_total": selection.tests_total,
        "by_rule": dict(plan.by_rule),
        "full_suite": list(plan.full_suite),
        "explanations": list(plan.explanations),
        "rule": CHANGED_RULE,
        "coverage_floors": False,
        "seconds": round(selection.seconds, 3),
    }


def _start_count(files: tuple[str, ...]) -> Callable[[], tuple[int | None, int | None]]:
    """Collect the suite in the background; the result is (selected, total) tests."""
    output = tempfile.TemporaryFile("w+")
    process = subprocess.Popen(
        (PYTHON, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"),
        cwd=ROOT,
        stdout=output,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    @cache
    def count() -> tuple[int | None, int | None]:
        with output:
            if process.wait():
                return None, None
            output.seek(0)
            per_file: dict[str, int] = {}
            for line in output:
                if "::" in line:
                    path = line.split("::", 1)[0]
                    per_file[path] = per_file.get(path, 0) + 1
        chosen = set(files)
        selected = sum(n for path, n in per_file.items() if path in chosen)
        return selected, sum(per_file.values())

    return count


def select_changed(base: str | None) -> Selection:
    """Select the tests for the diff against ``base`` (default origin/main)."""
    started = time.monotonic()
    diff = select_tests.diff(ROOT, base or DEFAULT_BASE, explicit=base is not None)
    if diff.merge_base is None:
        plan = select_tests.everything(ROOT, f"no diff to select from: {diff.note}")
    else:
        plan = select_tests.select(ROOT, diff.paths)
    seconds = time.monotonic() - started
    return Selection(diff, plan, seconds, _start_count(plan.test_files))


def _print_selection(selection: Selection) -> None:
    plan = selection.plan
    print(
        f"SELECTION: {len(plan.test_files)} of {plan.total_test_files} test files "
        f"from {len(plan.changed)} changed paths, chosen in {selection.seconds:.1f} s"
        + (" (FULL SUITE)" if plan.full_suite else "")
    )
    for line in plan.explanations:
        print(f"SELECTION PATH: {line}")


def _refuse(message: str, inventory: list[str], record: dict[str, object]) -> int:
    """Fail before any stage runs: every stage is reported as not run."""
    print(f"GATE FAILED: {message}")
    for name in inventory:
        print(f"NOT RUN: {name}")
    record.update(
        {
            "stages": [{"name": name, **_NOT_RUN} for name in inventory],
            "stages_run": [],
            "stages_passed": [],
            "not_run": inventory,
            "failed_stage": None,
            "passed": False,
            "exit_code": 1,
            "verdict": f"GATE FAILED: {message}",
            "receipt": [
                f"GATE FAILED: {message}",
                *(f"NOT RUN: {name}" for name in inventory),
            ],
        }
    )
    return 1


def _gate(args: argparse.Namespace, record: dict[str, object]) -> int:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    print(f"COMMIT: {commit} (worktree {'dirty' if dirty else 'clean'})")
    print(f"PYTHON: {sys.executable} ({sys.version.split()[0]})")
    print(f"PROFILE: {args.profile}")
    record.update(
        {
            "schema": JSON_SCHEMA,
            "profile": args.profile,
            "commit": commit,
            "dirty": dirty,
            "python": {"executable": sys.executable, "version": sys.version.split()[0]},
            "import": None,
            "partial": args.profile != "product",
            "ci_only_jobs_not_run": list(CI_ONLY_JOBS),
            "tests": None,
            "coverage": None,
            "peak_memory": {},
            "selection": None,
        }
    )
    inventory = [stage.name for stage in STAGES]
    if args.profile == "changed":
        inventory.append(SELECTED_TESTS)
    try:
        import athena

        source = Path(athena.__file__).resolve()
        print(f"IMPORT: {source}")
        record["import"] = str(source)
        if not source.is_relative_to((ROOT / "src/athena").resolve()):
            return _refuse("Athena import is outside this checkout", inventory, record)
    except (ImportError, TypeError) as error:
        return _refuse(f"cannot identify Athena import: {error}", inventory, record)
    # Only a JSON receipt needs the record; without one the call is unchanged.
    extra = {} if args.json is None else {"record": record}
    if args.profile != "changed":
        return run_profile(args.profile, **extra)
    try:
        selection = select_changed(args.base)
    except select_tests.SelectionError as error:
        return _refuse(str(error), inventory, record)
    _print_selection(selection)
    return run_profile(args.profile, selection=selection, **extra)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="product")
    parser.add_argument(
        "--base",
        help=f"changed profile only: the ref to diff against (default {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="also write the receipt as JSON to PATH (relative to the checkout root)",
    )
    args = parser.parse_args()
    if args.base is not None and args.profile != "changed":
        parser.error("--base applies only to --profile changed")
    if args.json == "-":
        parser.error(
            "--json needs a file path: standard output carries the human receipt"
        )
    if args.json is not None and not (ROOT / args.json).parent.is_dir():
        parser.error(f"--json: no directory for {args.json}")
    record: dict[str, object] = {}
    code = _gate(args, record)
    if args.json is not None:
        (ROOT / args.json).write_text(json.dumps(record, indent=2) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
