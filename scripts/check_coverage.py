"""Fail closed when Athena's full-source branch-coverage evidence drifts.

Each floor is also a ratchet: a measured value more than ``RATCHET_SLACK`` points
above its floor fails with "raise the floor to X", where X is
``suggested_floor(measured)``. Only the product profile (``scripts/coverage.sh``)
and CI's combined shards measure coverage; the changed profile never runs this.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELATIVE = Path("src/athena")
_COVERAGE_PRAGMA_RE = re.compile(
    r"#\s*pragma[:\s]?\s*no\s*(?:cover|branch)",
    re.IGNORECASE,
)
ALLOWED_PRAGMAS = Counter(
    {
        (
            "src/athena/demo.py",
            'if __name__ == "__main__":  # pragma: no cover - console-script parity',
        ): 1
    }
)
# A measured value more than this many points above its floor fails: gains are
# locked in rather than left as room for the next change to spend.
RATCHET_SLACK = Decimal("0.5")
# The floor the ratchet asks for sits this far below the measurement, rounded
# down to two decimals. CI's four combined shards measure up to about 0.09 points
# above a local run, so one paste leaves both measurements at or above the new
# floor and within RATCHET_SLACK of it.
RATCHET_MARGIN = Decimal("0.25")
COUNT_FIELDS = (
    "covered_lines",
    "num_statements",
    "missing_lines",
    "excluded_lines",
    "covered_branches",
    "num_branches",
    "missing_branches",
)


class CoverageGateError(RuntimeError):
    """Coverage evidence or policy is incomplete, narrowed, or below its floor."""


class CoverageFloorError(CoverageGateError):
    """Complete evidence whose measurement is below a floor or has outgrown one."""

    def __init__(
        self,
        failures: list[str],
        metrics: CoverageMetrics,
        policy: CoveragePolicy,
        file_count: int,
    ) -> None:
        super().__init__("coverage floor failed: " + "; ".join(failures))
        self.failures = tuple(failures)
        self.metrics = metrics
        self.policy = policy
        self.file_count = file_count


def suggested_floor(measured: Decimal) -> Decimal:
    """The floor the ratchet asks for: ``measured - RATCHET_MARGIN``, rounded down
    to two decimals."""

    return (measured - RATCHET_MARGIN).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class CoverageMetrics:
    """Exact coverage percentages derived from integer report totals."""

    covered_lines: int
    statements: int
    covered_branches: int
    branches: int
    line_percent: Decimal
    branch_percent: Decimal
    combined_percent: Decimal


@dataclass(frozen=True)
class CoveragePolicy:
    """Separately enforced floors plus the accounted-for exclusion count."""

    line: Decimal
    branch: Decimal
    combined: Decimal
    excluded_lines: int


def _table(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CoverageGateError(f"{label} must be a TOML/JSON table")
    return value


def _decimal_floor(table: dict[str, Any], key: str) -> Decimal:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CoverageGateError(f"tool.athena.coverage.{key} must be numeric")
    try:
        floor = Decimal(str(value))
    except InvalidOperation as exc:
        raise CoverageGateError(f"tool.athena.coverage.{key} must be numeric") from exc
    if not floor.is_finite() or not Decimal(0) <= floor <= Decimal(100):
        raise CoverageGateError(f"tool.athena.coverage.{key} must be between 0 and 100")
    return floor


def _load_policy(config_path: Path) -> CoveragePolicy:
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CoverageGateError(
            f"cannot read coverage config {config_path}: {exc}"
        ) from exc

    tool = _table(config.get("tool"), "tool")
    coverage = _table(tool.get("coverage"), "tool.coverage")
    run = _table(coverage.get("run"), "tool.coverage.run")
    report = _table(coverage.get("report"), "tool.coverage.report")
    athena = _table(tool.get("athena"), "tool.athena")
    floors = _table(athena.get("coverage"), "tool.athena.coverage")

    expected_run = {
        "branch": True,
        "relative_files": True,
        "source": [SOURCE_RELATIVE.as_posix()],
    }
    for key, expected in expected_run.items():
        if run.get(key) != expected:
            raise CoverageGateError(
                f"tool.coverage.run.{key} must be {expected!r}, got {run.get(key)!r}"
            )

    forbidden_run = sorted(set(run) & {"include", "omit"})
    forbidden_report = sorted(
        set(report)
        & {
            "exclude_also",
            "exclude_lines",
            "include",
            "omit",
            "partial_also",
            "partial_branches",
            "partial_branches_always",
        }
    )
    if forbidden_run or forbidden_report:
        details = ", ".join(
            [
                *(f"run.{key}" for key in forbidden_run),
                *(f"report.{key}" for key in forbidden_report),
            ]
        )
        raise CoverageGateError(
            f"coverage narrowing/exclusion settings are forbidden: {details}"
        )

    expected_report = {"precision": 2, "show_missing": True, "skip_empty": False}
    for key, expected in expected_report.items():
        if report.get(key) != expected:
            raise CoverageGateError(
                f"tool.coverage.report.{key} must be {expected!r}, got {report.get(key)!r}"
            )

    excluded_lines = floors.get("excluded_line_count")
    if (
        isinstance(excluded_lines, bool)
        or not isinstance(excluded_lines, int)
        or excluded_lines < 0
    ):
        raise CoverageGateError(
            "tool.athena.coverage.excluded_line_count must be a non-negative integer"
        )

    return CoveragePolicy(
        line=_decimal_floor(floors, "line_floor"),
        branch=_decimal_floor(floors, "branch_floor"),
        combined=_decimal_floor(floors, "combined_floor"),
        excluded_lines=excluded_lines,
    )


def _source_inventory(repo_root: Path) -> set[str]:
    source_root = repo_root / SOURCE_RELATIVE
    if not source_root.is_dir() or source_root.is_symlink():
        raise CoverageGateError(f"source root must be a real directory: {source_root}")
    symlinks = sorted(path for path in source_root.rglob("*") if path.is_symlink())
    if symlinks:
        rendered = "\n  ".join(str(path) for path in symlinks)
        raise CoverageGateError(
            f"symlinked source paths are unsupported:\n  {rendered}"
        )

    files = sorted(source_root.rglob("*.py"))
    if not files:
        raise CoverageGateError(f"no runtime Python files found below {source_root}")

    pragmas: Counter[tuple[str, str]] = Counter()
    for path in files:
        relative = path.relative_to(repo_root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CoverageGateError(f"cannot read source file {path}: {exc}") from exc
        for line in lines:
            if _COVERAGE_PRAGMA_RE.search(line):
                pragmas[(relative, line.strip())] += 1

    if pragmas != ALLOWED_PRAGMAS:
        added = list((pragmas - ALLOWED_PRAGMAS).elements())
        removed = list((ALLOWED_PRAGMAS - pragmas).elements())
        details = []
        if added:
            details.append(
                "unexpected: " + "; ".join(f"{path}: {line}" for path, line in added)
            )
        if removed:
            details.append(
                "missing: " + "; ".join(f"{path}: {line}" for path, line in removed)
            )
        raise CoverageGateError(
            "coverage pragma policy changed (" + " | ".join(details) + ")"
        )

    return {path.relative_to(repo_root).as_posix() for path in files}


def _count(summary: dict[str, Any], key: str, label: str) -> int:
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoverageGateError(f"{label}.{key} must be a non-negative integer")
    return value


def _percentage(covered: int, total: int, label: str) -> Decimal:
    if total <= 0:
        raise CoverageGateError(f"{label} denominator must be positive")
    if covered > total:
        raise CoverageGateError(f"{label} covered count exceeds its denominator")
    return Decimal(covered) * Decimal(100) / Decimal(total)


def _metrics(totals: dict[str, Any]) -> CoverageMetrics:
    covered_lines = _count(totals, "covered_lines", "totals")
    statements = _count(totals, "num_statements", "totals")
    missing_lines = _count(totals, "missing_lines", "totals")
    covered_branches = _count(totals, "covered_branches", "totals")
    branches = _count(totals, "num_branches", "totals")
    missing_branches = _count(totals, "missing_branches", "totals")
    if covered_lines + missing_lines != statements:
        raise CoverageGateError(
            "covered_lines + missing_lines does not equal num_statements"
        )
    if covered_branches + missing_branches != branches:
        raise CoverageGateError(
            "covered_branches + missing_branches does not equal num_branches"
        )
    return CoverageMetrics(
        covered_lines=covered_lines,
        statements=statements,
        covered_branches=covered_branches,
        branches=branches,
        line_percent=_percentage(covered_lines, statements, "line coverage"),
        branch_percent=_percentage(covered_branches, branches, "branch coverage"),
        combined_percent=_percentage(
            covered_lines + covered_branches,
            statements + branches,
            "combined coverage",
        ),
    )


def evaluate(
    report_path: Path,
    *,
    repo_root: Path = REPO_ROOT,
    config_path: Path | None = None,
) -> tuple[CoverageMetrics, CoveragePolicy, int]:
    """Validate one JSON report and return its metrics, floors, and file count."""

    repo_root = repo_root.resolve()
    config_path = (config_path or repo_root / "pyproject.toml").resolve()
    policy = _load_policy(config_path)
    expected_files = _source_inventory(repo_root)
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageGateError(
            f"cannot read coverage JSON {report_path}: {exc}"
        ) from exc

    root = _table(payload, "coverage report")
    meta = _table(root.get("meta"), "meta")
    if meta.get("branch_coverage") is not True:
        raise CoverageGateError("coverage report did not measure branches")

    files = _table(root.get("files"), "files")
    reported_files = set(files)
    if reported_files != expected_files:
        missing = sorted(expected_files - reported_files)
        unexpected = sorted(reported_files - expected_files)
        details = []
        if missing:
            details.append("missing:\n  " + "\n  ".join(missing))
        if unexpected:
            details.append("unexpected:\n  " + "\n  ".join(unexpected))
        raise CoverageGateError(
            "coverage source inventory mismatch\n" + "\n".join(details)
        )

    summed = {key: 0 for key in COUNT_FIELDS}
    for path, entry_value in files.items():
        entry = _table(entry_value, f"files[{path!r}]")
        summary = _table(entry.get("summary"), f"files[{path!r}].summary")
        for key in COUNT_FIELDS:
            summed[key] += _count(summary, key, f"files[{path!r}].summary")

    totals = _table(root.get("totals"), "totals")
    for key, expected in summed.items():
        actual = _count(totals, key, "totals")
        if actual != expected:
            raise CoverageGateError(
                f"totals.{key} is {actual}, but per-file summaries add to {expected}"
            )

    metrics = _metrics(totals)
    excluded_lines = _count(totals, "excluded_lines", "totals")
    if excluded_lines != policy.excluded_lines:
        raise CoverageGateError(
            f"excluded line count is {excluded_lines}, expected {policy.excluded_lines}"
        )
    failures = []
    for label, actual, floor in (
        ("line", metrics.line_percent, policy.line),
        ("branch", metrics.branch_percent, policy.branch),
        ("combined", metrics.combined_percent, policy.combined),
    ):
        if actual < floor:
            failures.append(f"{label} {actual:.5f}% < {floor}%")
        elif actual - floor > RATCHET_SLACK:
            failures.append(
                f"{label} {actual:.5f}% is more than {RATCHET_SLACK} points above "
                f"its floor {floor}%: raise the floor to {suggested_floor(actual)} "
                f"(tool.athena.coverage.{label}_floor in pyproject.toml)"
            )
    if failures:
        raise CoverageFloorError(failures, metrics, policy, len(reported_files))

    return metrics, policy, len(reported_files)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="coverage.py JSON report")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root whose src/athena inventory must be represented",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="coverage policy TOML (defaults to REPO_ROOT/pyproject.toml)",
    )
    return parser


def _print_measurement(
    metrics: CoverageMetrics, policy: CoveragePolicy, file_count: int
) -> None:
    # scripts/gate.py reads the Line, Branch and Combined lines into its receipt.
    print(f"Coverage inventory: {file_count} runtime Python files")
    print(
        "Line coverage: "
        f"{metrics.covered_lines}/{metrics.statements} = {metrics.line_percent:.5f}% "
        f"(floor {policy.line}%)"
    )
    print(
        "Branch coverage: "
        f"{metrics.covered_branches}/{metrics.branches} = "
        f"{metrics.branch_percent:.5f}% (floor {policy.branch}%)"
    )
    print(
        "Combined coverage: "
        f"{metrics.covered_lines + metrics.covered_branches}/"
        f"{metrics.statements + metrics.branches} = "
        f"{metrics.combined_percent:.5f}% (floor {policy.combined}%)"
    )
    print(f"Excluded lines: {policy.excluded_lines} (accounted existing policy)")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        metrics, policy, file_count = evaluate(
            args.report,
            repo_root=args.repo_root,
            config_path=args.config,
        )
    except CoverageFloorError as exc:
        # The evidence is complete, so the measurement is still reported, and each
        # failed floor gets a line of its own that scripts/gate.py puts in its receipt.
        _print_measurement(exc.metrics, exc.policy, exc.file_count)
        for failure in exc.failures:
            print(f"Coverage floor failed: {failure}")
        print(f"coverage gate failed: {exc}", file=sys.stderr)
        return 1
    except CoverageGateError as exc:
        print(f"coverage gate failed: {exc}", file=sys.stderr)
        return 1

    _print_measurement(metrics, policy, file_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
