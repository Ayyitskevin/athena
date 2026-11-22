"""Local gate profiles and the CI checks each local stage represents.

A product pass is local evidence, not the container or release-candidate jobs.
"""

from __future__ import annotations

import argparse
import difflib
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKERS_VARIABLE = "ATHENA_COVERAGE_WORKERS"  # read by scripts/coverage.sh
# What pytest-xdist prints when a worker process dies mid-test (e.g. OOM-killed).
WORKER_LOST_MARKERS = ("node down: Not properly terminated", "crashed while running")
MEMORY_SAMPLE_SECONDS = 0.5


@dataclass(frozen=True)
class Stage:
    name: str
    command: tuple[str, ...]
    quick: bool
    ci_steps: tuple[str, ...]
    ci_commands: tuple[str, ...]  # Complete run body for each ci_steps entry.
    measure_memory: bool = False  # Report peak memory; explain killed workers.


@dataclass(frozen=True)
class StageOutcome:
    """A stage result that adds lines to the receipt; plain bools add none."""

    passed: bool
    receipt: tuple[str, ...] = ()


@dataclass(frozen=True)
class MeasuredRun:
    returncode: int
    peak_tree_bytes: int | None  # None where /proc is unavailable.
    peak_process_bytes: int | None
    worker_lost: bool


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
        return StageOutcome(run.returncode == 0, _memory_receipt(stage.name, run))
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


def run_profile(profile: str, runner=_run_stage) -> int:
    if profile not in {"quick", "product"}:
        raise ValueError(f"Unknown profile: {profile}")
    selected = [stage for stage in STAGES if profile == "product" or stage.quick]
    ran: list[str] = []
    failed: str | None = None
    attempted: list[str] = []
    receipt: list[str] = []
    for stage in selected:
        print(f"==> {stage.name}: {' '.join(stage.command)}", flush=True)
        try:
            outcome = runner(stage)
        except OSError as error:
            print(f"UNAVAILABLE: {stage.name}: {error}", flush=True)
            failed = stage.name
            break
        if isinstance(outcome, StageOutcome):
            passed = outcome.passed
            receipt.extend(outcome.receipt)
        else:
            passed = bool(outcome)
        attempted.append(stage.name)
        if not passed:
            failed = stage.name
            print(f"FAILED: {stage.name}", flush=True)
            break
        ran.append(stage.name)
    print("STAGES RUN: " + (", ".join(attempted) if attempted else "none"))
    print("STAGES PASSED: " + (", ".join(ran) if ran else "none"))
    for stage in STAGES:
        if stage.name not in attempted:
            print(f"NOT RUN: {stage.name}")
    print(
        "CI-ONLY JOBS NOT RUN: container, audit (release-candidate supply-chain evidence)"
    )
    for line in receipt:
        print(line)
    if failed:
        print(f"GATE FAILED: {failed}")
        return 1
    if profile == "product":
        print("PRODUCT GATE PASS (local product stages only; CI-only jobs not run)")
    else:
        print("QUICK PROFILE PASS (PARTIAL; product and CI checks not all run)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("quick", "product"), default="product")
    args = parser.parse_args()
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
    try:
        import athena

        source = Path(athena.__file__).resolve()
        print(f"IMPORT: {source}")
        if not source.is_relative_to((ROOT / "src/athena").resolve()):
            print("GATE FAILED: Athena import is outside this checkout")
            for stage in STAGES:
                print(f"NOT RUN: {stage.name}")
            return 1
    except (ImportError, TypeError) as error:
        print(f"GATE FAILED: cannot identify Athena import: {error}")
        for stage in STAGES:
            print(f"NOT RUN: {stage.name}")
        return 1
    return run_profile(args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
