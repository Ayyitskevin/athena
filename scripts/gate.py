"""Local gate profiles and the CI checks each local stage represents.

A product pass is local evidence, not the container or release-candidate jobs.
"""

from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Stage:
    name: str
    command: tuple[str, ...]
    quick: bool
    ci_steps: tuple[str, ...]
    ci_fragments: tuple[str, ...]


PYTHON = sys.executable
STAGES = (
    Stage(
        "dependencies",
        (PYTHON, "-m", "pip", "check"),
        False,
        ("Verify pinned dependency set",),
        ("-m pip check", "-m pip freeze --exclude-editable"),
    ),
    Stage(
        "lint",
        (PYTHON, "-m", "ruff", "check", "."),
        True,
        ("Lint",),
        ("-m ruff check .",),
    ),
    Stage(
        "format",
        (PYTHON, "-m", "ruff", "format", "--check", "."),
        True,
        ("Check formatting",),
        ("-m ruff format --check .",),
    ),
    Stage(
        "typecheck",
        (PYTHON, "-m", "mypy", "src/athena"),
        False,
        ("Type-check every runtime module",),
        ("-m mypy src/athena",),
    ),
    Stage(
        "imports",
        (PYTHON, "scripts/check_import_contracts.py"),
        True,
        ("Check import contracts",),
        ("scripts/check_import_contracts.py",),
    ),
    Stage(
        "ownership",
        (PYTHON, "scripts/check_write_ownership.py"),
        True,
        ("Check write ownership",),
        ("scripts/check_write_ownership.py",),
    ),
    Stage(
        "imported-at",
        (PYTHON, "scripts/check_imported_at_guards.py"),
        True,
        ("Check imported_at guards",),
        ("scripts/check_imported_at_guards.py",),
    ),
    Stage(
        "styles",
        (PYTHON, "scripts/check_template_styles.py"),
        True,
        ("Check template styles",),
        ("scripts/check_template_styles.py",),
    ),
    Stage(
        "routes",
        (PYTHON, "scripts/check_template_routes.py"),
        True,
        ("Check template routes",),
        ("scripts/check_template_routes.py",),
    ),
    Stage(
        "coverage",
        ("bash", "scripts/coverage.sh"),
        False,
        ("Run assigned tests with full-source branch coverage",),
        ("scripts/coverage_shards.py run",),
    ),
)


def _run_stage(stage: Stage) -> bool:
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
    return subprocess.run(stage.command, cwd=ROOT, env=env, check=False).returncode == 0


def run_profile(profile: str, runner=_run_stage) -> int:
    if profile not in {"quick", "product"}:
        raise ValueError(f"Unknown profile: {profile}")
    selected = [stage for stage in STAGES if profile == "product" or stage.quick]
    ran: list[str] = []
    failed: str | None = None
    attempted: list[str] = []
    for stage in selected:
        print(f"==> {stage.name}: {' '.join(stage.command)}", flush=True)
        try:
            passed = runner(stage)
        except OSError as error:
            print(f"UNAVAILABLE: {stage.name}: {error}", flush=True)
            failed = stage.name
            break
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
    parser.add_argument("--profile", choices=("quick", "product"), default="quick")
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
