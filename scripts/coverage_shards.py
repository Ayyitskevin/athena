"""Finite hosted coverage shards; the required fan-in conserves every test ID.

Run from the repository root with the pinned test interpreter. Ordinary local
coverage.sh and gate.sh remain unchanged. No shard may substitute for the full
source/branch/floor check performed after all raw data has been combined.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SHARDS = 4


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing/non-regular/empty evidence: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def partition(ids: list[str], index: int) -> list[str]:
    if not ids or len(set(ids)) != len(ids) or ids != sorted(ids):
        raise ValueError("inventory must be nonempty, sorted, and unique")
    if not 0 <= index < SHARDS:
        raise ValueError("invalid shard index")
    selected = ids[index::SHARDS]
    if not selected:
        raise ValueError("empty shard")
    return selected


class Inventory:
    def __init__(self) -> None:
        self.ids: list[str] = []

    def pytest_collection_finish(self, session) -> None:
        self.ids = sorted(item.nodeid for item in session.items)


class Outcomes:
    def __init__(self) -> None:
        self.reports: list[dict[str, str]] = []

    def pytest_runtest_logreport(self, report) -> None:
        self.reports.append(
            {"id": report.nodeid, "phase": report.when, "outcome": report.outcome}
        )


def verify_outcomes(selected: list[str], reports: list[dict[str, str]]) -> None:
    expected = Counter(
        (node, phase, "passed")
        for node in selected
        for phase in ("setup", "call", "teardown")
    )
    observed = Counter((r["id"], r["phase"], r["outcome"]) for r in reports)
    if observed != expected:
        raise ValueError("executed test IDs/phases differ, or a test failed/skipped")


def fresh_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("output directory must not be a symlink")
    path = path.resolve()
    if path.is_relative_to(Path.cwd().resolve()):
        raise ValueError("evidence must be outside the checkout")
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError("evidence directory must be empty")
    return path


def collect(path: Path) -> list[str]:
    # A separate process prevents collection imports from evading coverage in
    # the subsequent test run. The plugin records IDs, never parses console text.
    result = subprocess.run(
        [sys.executable, __file__, "inventory", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print(result.stdout + result.stderr, file=sys.stderr)
        result.check_returncode()
    ids = json.loads(path.read_text())
    partition(ids, 0)
    print(f"Collected {len(ids)} test IDs", flush=True)
    return ids


def head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def run(index: int, output: Path) -> None:
    output = fresh_directory(output)
    ids = collect(output / "inventory.json")
    selected = partition(ids, index)
    data = output / ".coverage"
    os.environ["COVERAGE_FILE"] = str(data)
    outcomes = Outcomes()
    print(
        f"Coverage shard {index}/{SHARDS}: {len(selected)}/{len(ids)} test IDs",
        flush=True,
    )
    result = pytest.main(
        [
            "-q",
            "-n",
            "4",
            "-ra",
            "--strict-markers",
            "--durations=10",
            "--cov",
            "--cov-config=pyproject.toml",
            "--cov-report=",
            *selected,
        ],
        plugins=[outcomes],
    )
    if result != 0:
        raise ValueError(f"pytest failed: exit {result}")
    verify_outcomes(selected, outcomes.reports)
    receipt = {
        "head": head(),
        "index": index,
        "shards": SHARDS,
        "inventory": ids,
        "selected": selected,
        "reports": outcomes.reports,
        "policy_sha256": digest(Path("pyproject.toml")),
        "data_sha256": digest(data),
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def verify_receipts(
    root: Path, ids: list[str], revision: str, policy: str
) -> list[Path]:
    expected_dirs = {f"coverage-shard-{i}" for i in range(SHARDS)}
    if root.is_symlink() or {p.name for p in root.iterdir()} != expected_dirs:
        raise ValueError("missing or extra shard directories")
    paths = []
    for i in range(SHARDS):
        directory = root / f"coverage-shard-{i}"
        if directory.is_symlink():
            raise ValueError("shard directory must not be a symlink")
        receipt_path = directory / "receipt.json"
        digest(receipt_path)
        receipt = json.loads(receipt_path.read_text())
        if (
            receipt["head"] != revision
            or receipt["policy_sha256"] != policy
            or type(receipt["index"]) is not int
            or receipt["index"] != i
            or type(receipt["shards"]) is not int
            or receipt["shards"] != SHARDS
            or receipt["inventory"] != ids
            or receipt["selected"] != partition(ids, i)
        ):
            raise ValueError("shard revision/policy/inventory/assignment mismatch")
        verify_outcomes(receipt["selected"], receipt["reports"])
        data = directory / ".coverage"
        if digest(data) != receipt["data_sha256"]:
            raise ValueError("coverage data digest mismatch")
        paths.append(data)
    return paths


def combine(root: Path, output: Path) -> None:
    from coverage import Coverage, CoverageData

    output = fresh_directory(output)
    ids = collect(output / "inventory.json")
    paths = verify_receipts(root, ids, head(), digest(Path("pyproject.toml")))
    # Read every explicit file first: coverage.combine warns/skips unreadable
    # inputs by design; here missing, corrupt, line-only, or empty data is fatal.
    for path in paths:
        data = CoverageData(basename=str(path))
        data.read()
        if not data.has_arcs() or not data.measured_files():
            raise ValueError(f"empty or non-branch data: {path}")
    coverage = Coverage(
        data_file=str(output / ".coverage"), config_file="pyproject.toml"
    )
    coverage.combine(data_paths=[str(p) for p in paths], strict=True, keep=True)
    coverage.save()
    coverage.json_report(outfile=str(output / "coverage.json"))
    coverage.xml_report(outfile=str(output / "coverage.xml"))
    subprocess.run(
        [sys.executable, "scripts/check_coverage.py", str(output / "coverage.json")],
        check=True,
    )
    print(
        f"Coverage fan-in PASS: {len(ids)} exact test IDs, {SHARDS} shards", flush=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("output", type=Path)
    runner = commands.add_parser("run")
    runner.add_argument("index", type=int, choices=range(SHARDS))
    runner.add_argument("output", type=Path)
    merger = commands.add_parser("combine")
    merger.add_argument("input", type=Path)
    merger.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "inventory":
        inventory_plugin = Inventory()
        result = pytest.main(
            ["--collect-only", "-q", "--strict-markers"], plugins=[inventory_plugin]
        )
        if result != 0:
            raise ValueError(f"inventory collection failed: exit {result}")
        partition(inventory_plugin.ids, 0)
        args.output.write_text(json.dumps(inventory_plugin.ids, indent=2) + "\n")
    elif args.command == "run":
        run(args.index, args.output)
    else:
        combine(args.input, args.output)


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"coverage shard gate failed: {exc}", file=sys.stderr)
        sys.exit(1)
