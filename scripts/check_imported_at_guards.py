"""Pin every native-only activity reader to its ``imported_at`` guard.

The doctrine (docs/FORGE.md, migration 0041): an imported row is foreign
history — something Athena was TOLD, not work it did. Every mechanism that
ACTS on the trail (projections, rollups, counters, automation scans, undo)
must exclude it, or a hostile import bundle or forge delivery moves native
state. H-0.3 (an imported forge event moved an issue's status through a
wildcard automation rule) and H-0.4 (the security counters and attention
rollup counted imported rows while the docs claimed otherwise) are what
happens when that rule is enforced by hand.

The check is greppable by design. Every SQL read of the ``activity`` table in
``src/athena`` must be declared — either as GUARDED (the reader's function
carries the guard, verified by pattern) or as EXEMPT (a general reader, with
the reason imported rows are safe there). A new reader that is declared in
neither way fails the build; an exemption that no longer matches a live reader
fails as stale.

The declarations live beside the code they describe: one
``imported_at_guards.toml`` per package (``src/athena/aegis/``,
``src/athena/core/``), holding the entries of the modules in that directory and
below it, up to the next directory that has a file of its own. Its header says
what each kind of entry means. A data file rather than a declaration inside
each module, for two reasons: a find-and-replace that removes a guard from a
module (``native_only=True`` to ``False``) would rewrite a pattern declared in
the same file and pass; and a migration's text is checksummed once applied, so
its guard cannot be declared inside it. The checker reads the files with the
standard library, imports nothing from Athena, and fails on an entry declared
in the wrong package's file, naming a module or an exempt function that does
not exist, or naming a mechanism row that MECHANISM_ROWS does not have.

``MECHANISMS``, ``EXTRA_GUARDED`` and ``EXEMPT_READERS`` remain importable from
this script as read-only views gathered from the files. docs/FORGE.md's guard
table is pinned to MECHANISM_ROWS (and so to MECHANISMS) by
tests/test_imported_at_guards.py, so the doc cannot claim a guard the code does
not carry — the exact H-0.4 failure.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import sys
import tomllib
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"
GUARD_FILE = "imported_at_guards.toml"

# Case-sensitive on purpose: the codebase writes SQL keywords uppercase, and
# matching case keeps prose ("events from activity") out of the net. The word
# boundary keeps activity_target_id_sequences and activity_visibility_projects
# from matching.
_ACTIVITY_READ_RE = re.compile(r"\bFROM\s+activity\b|\bJOIN\s+activity\b")

# The rows of docs/FORGE.md's native-only guard table, in its order: (label,
# guard). The guard column is the substring FORGE.md's table cell must contain;
# the pin test compares labels exactly so the table cannot grow or rename a row
# without touching this list. Each row's assertions — which module carries the
# guard, and where — are [[mechanism]] entries in the guard files, and a row
# with none fails the check.
MECHANISM_ROWS: tuple[tuple[str, str], ...] = (
    ("Undo", "undo_imported_event"),
    ("Lifecycle facts (0055)", "imported_at IS NULL"),
    ("Claim handoffs (0058)", "imported_at IS NULL"),
    ("Exact-version candidates and reviews (0082)", "imported_at IS NULL"),
    ("Assignee facts (0068)", "imported_at IS NULL"),
    ("Fleet metrics", "imported_at IS NULL"),
    ("Attention rollup, security refusal counters", "imported_at IS NULL"),
    ("Automation scan", "imported_at IS NULL"),
)

# What each kind of entry carries: required keys, and optional ones.
_ENTRY_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "mechanism": (frozenset({"label", "module", "pattern"}), frozenset({"function"})),
    "guarded": (frozenset({"module", "function", "pattern", "why"}), frozenset()),
    "exempt": (frozenset({"module", "function", "why"}), frozenset()),
}
_MODULE_LEVEL = "<module>"


class GuardCheckError(RuntimeError):
    """A guard or the reader inventory drifted from the code."""


@dataclass(frozen=True)
class Violation:
    """One unguarded, unlisted, or stale activity reader, or a bad declaration."""

    path: Path
    line: int
    detail: str


@dataclass(frozen=True)
class Registry:
    """The declarations gathered from every guard file, in the shapes the
    checks use (paths relative to the package root)."""

    # (label, guard, ((path, function or None, pattern), ...)), in row order.
    mechanisms: tuple[tuple[str, str, tuple[tuple[str, str | None, str], ...]], ...]
    # (path, function, pattern, why)
    extra_guarded: tuple[tuple[str, str, str, str], ...]
    # (path, function or "<module>", why)
    exempt_readers: tuple[tuple[str, str, str], ...]
    # What was wrong with the declarations themselves.
    problems: tuple[Violation, ...]


def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _read_trees(package_root: Path) -> dict[str, tuple[Path, str, ast.AST]]:
    modules: dict[str, tuple[Path, str, ast.AST]] = {}
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise GuardCheckError(
                f"cannot parse {path}:{exc.lineno}: {exc.msg}"
            ) from exc
        modules[relative] = (path, source, tree)
    return modules


def _guard_files(package_root: Path) -> list[Path]:
    return sorted(
        path
        for path in package_root.rglob(GUARD_FILE)
        if "__pycache__" not in path.parts
    )


def _home(package_root: Path, relative: str) -> Path | None:
    """The guard file that holds a module's entries: the nearest one at or
    above the module's directory, within the package root."""
    directory = (package_root / relative).parent
    while True:
        candidate = directory / GUARD_FILE
        if candidate.is_file():
            return candidate
        if directory == package_root or package_root not in directory.parents:
            return None
        directory = directory.parent


def _entry_lines(text: str, kind: str) -> list[int]:
    """The line of each ``[[kind]]`` header, in file order."""
    header = f"[[{kind}]]"
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip() == header
    ]


def _elsewhere(
    modules: dict[str, tuple[Path, str, ast.AST]], name: str, function: str | None
) -> list[str]:
    """Modules that define ``function`` (or, without one, share the file name)."""
    if function is None or function == _MODULE_LEVEL:
        return sorted(r for r in modules if PurePosixPath(r).name == name)
    return sorted(
        r
        for r, (_path, _source, tree) in modules.items()
        if function in _functions(tree)
    )


def load_registry(
    package_root: Path,
    modules: dict[str, tuple[Path, str, ast.AST]] | None = None,
) -> Registry:
    """Gather every guard file under ``package_root``, checking each entry's
    shape and home. An entry that cannot be placed is reported and left out,
    so the reader it meant to declare also fails as undeclared."""
    if modules is None:
        modules = _read_trees(package_root)
    problems: list[Violation] = []
    assertions: dict[str, list[tuple[str, str | None, str]]] = {
        label: [] for label, _guard in MECHANISM_ROWS
    }
    extra: list[tuple[str, str, str, str]] = []
    exempt: list[tuple[str, str, str]] = []
    seen: dict[tuple[str, str, str], Path] = {}

    for guard_file in _guard_files(package_root):
        text = guard_file.read_text(encoding="utf-8")
        try:
            declared = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise GuardCheckError(f"cannot parse {guard_file}: {exc}") from exc
        here = guard_file.parent.relative_to(package_root)

        def problem(line: int, detail: str, file: Path = guard_file) -> None:
            problems.append(Violation(file, line, detail))

        unknown_kinds = sorted(set(declared) - set(_ENTRY_KEYS))
        if unknown_kinds:
            problem(
                1,
                f"unknown entry kinds {unknown_kinds}; a guard file holds "
                f"{sorted(_ENTRY_KEYS)} entries",
            )
        for kind, (required, optional) in _ENTRY_KEYS.items():
            entries = declared.get(kind, [])
            lines = _entry_lines(text, kind)
            if not isinstance(entries, list) or not all(
                isinstance(entry, dict) for entry in entries
            ):
                problem(1, f"{kind} must be written as [[{kind}]] tables")
                continue
            for index, entry in enumerate(entries):
                line = lines[index] if index < len(lines) else 1
                keys = set(entry)
                missing = sorted(required - keys)
                extra_keys = sorted(keys - required - optional)
                if missing or extra_keys:
                    problem(
                        line,
                        f"[[{kind}]] entry has missing keys {missing} and "
                        f"unknown keys {extra_keys}",
                    )
                    continue
                if not all(isinstance(value, str) for value in entry.values()):
                    problem(line, f"[[{kind}]] entry values must be strings")
                    continue
                module = PurePosixPath(entry["module"])
                function: str | None = entry.get("function")
                if module.is_absolute() or ".." in module.parts or not module.parts:
                    problem(
                        line,
                        f"[[{kind}]] module {entry['module']!r} must be a path "
                        f"relative to {guard_file.parent}",
                    )
                    continue
                relative = (PurePosixPath(here.as_posix()) / module).as_posix()
                target = package_root / relative
                if not target.is_file():
                    others = _elsewhere(modules, module.name, function)
                    hint = (
                        f"; {', '.join(others)} "
                        f"{'defines' if function else 'exists'} there — declare "
                        "it in that module's own guard file"
                        if others
                        else ""
                    )
                    problem(
                        line,
                        f"declared in the wrong home: {relative} does not exist "
                        f"beside {guard_file.name} in {here.as_posix()}/{hint}",
                    )
                    continue
                home = _home(package_root, relative)
                if home != guard_file:
                    problem(
                        line,
                        f"declared in the wrong home: {relative}'s entries "
                        f"belong in {home}, the nearest {GUARD_FILE} above it",
                    )
                    continue
                is_sql = relative.endswith(".sql")
                if is_sql and (kind != "mechanism" or function is not None):
                    problem(
                        line,
                        f"{relative}: a migration is declared only as a "
                        "[[mechanism]] without a function (the whole file "
                        "carries the guard)",
                    )
                    continue
                if not is_sql and (not relative.endswith(".py") or function is None):
                    problem(
                        line,
                        f"{relative}: a Python module's entry names the "
                        "function that reads or guards the trail",
                    )
                    continue
                if function == _MODULE_LEVEL and kind != "exempt":
                    problem(
                        line,
                        f"{relative}: only an exempt entry may name {_MODULE_LEVEL}",
                    )
                    continue
                if (
                    kind == "exempt"
                    and function != _MODULE_LEVEL
                    and function not in _functions(modules[relative][2])
                ):
                    others = _elsewhere(modules, module.name, function)
                    problem(
                        line,
                        f"exempt entry names {relative}::{function}, a function "
                        f"that module does not define"
                        + (f" (defined in {', '.join(others)})" if others else ""),
                    )
                    continue
                key = (kind, relative, function or "")
                if key in seen:
                    problem(
                        line,
                        f"{relative}::{function} is declared twice as {kind} "
                        f"(also in {seen[key]})",
                    )
                    continue
                seen[key] = guard_file
                if kind == "mechanism":
                    if entry["label"] not in assertions:
                        problem(
                            line,
                            f"mechanism {entry['label']!r} is not a row of "
                            "MECHANISM_ROWS (docs/FORGE.md's guard table)",
                        )
                        continue
                    assertions[entry["label"]].append(
                        (relative, function, entry["pattern"])
                    )
                elif kind == "guarded":
                    extra.append(
                        (relative, entry["function"], entry["pattern"], entry["why"])
                    )
                else:
                    exempt.append((relative, entry["function"], entry["why"]))

    guarded_sites = {(r, f) for r, f, _p, _w in extra} | {
        (r, f) for found in assertions.values() for r, f, _p in found if f
    }
    for relative, function, _why in exempt:
        if (relative, function) in guarded_sites:
            problems.append(
                Violation(
                    package_root / relative,
                    1,
                    f"{relative}::{function} is declared both guarded and "
                    "exempt; it is one or the other",
                )
            )
    for label, found in assertions.items():
        if not found:
            problems.append(
                Violation(
                    package_root,
                    1,
                    f"{label}: no [[mechanism]] entry carries this row of "
                    "docs/FORGE.md's guard table",
                )
            )
    return Registry(
        mechanisms=tuple(
            (label, guard, tuple(assertions[label])) for label, guard in MECHANISM_ROWS
        ),
        extra_guarded=tuple(extra),
        exempt_readers=tuple(exempt),
        problems=tuple(problems),
    )


def __getattr__(name: str) -> Any:
    """MECHANISMS, EXTRA_GUARDED and EXEMPT_READERS: read-only views of the
    repository's guard files, gathered when first asked for."""
    views = {
        "MECHANISMS": "mechanisms",
        "EXTRA_GUARDED": "extra_guarded",
        "EXEMPT_READERS": "exempt_readers",
    }
    if name not in views:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(load_registry(PACKAGE_ROOT), views[name])


def _activity_read_sites(
    modules: dict[str, tuple[Path, str, ast.AST]],
) -> dict[tuple[str, str], list[int]]:
    """Every SQL read of the activity table, keyed by (file, function)."""
    sites: dict[tuple[str, str], list[int]] = {}
    for relative, (_path, _source, tree) in sorted(modules.items()):
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not _ACTIVITY_READ_RE.search(node.value):
                continue
            owner = _MODULE_LEVEL
            for function in functions:
                if function.lineno <= node.lineno <= (function.end_lineno or 0):
                    owner = function.name
            sites.setdefault((relative, owner), []).append(node.lineno)
    return sites


def find_violations(package_root: Path) -> list[Violation]:
    modules = _read_trees(package_root)
    registry = load_registry(package_root, modules)
    violations: list[Violation] = list(registry.problems)

    # 1. Every mechanism assertion must hold in the current source.
    for label, _guard, assertions in registry.mechanisms:
        for relative, function, pattern in assertions:
            path = package_root / relative
            if relative.endswith(".sql"):
                if not path.is_file():
                    violations.append(
                        Violation(path, 1, f"{label}: missing migration {relative}")
                    )
                    continue
                if not re.search(pattern, path.read_text(encoding="utf-8")):
                    violations.append(
                        Violation(
                            path,
                            1,
                            f"{label}: {relative} no longer carries the guard "
                            f"/{pattern}/",
                        )
                    )
                continue
            entry = modules.get(relative)
            if entry is None:
                violations.append(
                    Violation(path, 1, f"{label}: missing module {relative}")
                )
                continue
            file_path, source, tree = entry
            target = _functions(tree).get(function) if function else None
            if function and target is None:
                violations.append(
                    Violation(
                        file_path,
                        1,
                        f"{label}: {relative} has no function {function} — "
                        f"a rename must move the guard assertion, not drop it",
                    )
                )
                continue
            segment = (
                ast.get_source_segment(source, target) if target is not None else source
            ) or ""
            if not re.search(pattern, segment):
                violations.append(
                    Violation(
                        file_path,
                        target.lineno if target is not None else 1,
                        f"{label}: {relative}::{function} lost its guard /{pattern}/",
                    )
                )

    # 2. Every SQL read of the activity table must be a known reader.
    sites = _activity_read_sites(modules)
    guarded_sites = {
        (relative, function or _MODULE_LEVEL)
        for _label, _guard, assertions in registry.mechanisms
        for relative, function, _pattern in assertions
        if relative.endswith(".py")
    } | {(relative, function) for relative, function, _p, _w in registry.extra_guarded}
    exempt_sites = {
        (relative, function) for relative, function, _why in registry.exempt_readers
    }

    for (relative, function), lines in sorted(sites.items()):
        path = modules[relative][0]
        if (relative, function) in guarded_sites or (relative, function) in (
            exempt_sites
        ):
            continue
        violations.append(
            Violation(
                path,
                lines[0],
                f"new activity reader {relative}::{function} is in neither "
                f"the guarded nor the exempt list — a native-only reader must "
                f"filter imported_at IS NULL; a general reader must name why "
                f"foreign history is safe",
            )
        )

    # 3. Extra guarded readers must still carry their guard.
    for relative, function, pattern, why in registry.extra_guarded:
        entry = modules.get(relative)
        path = package_root / relative
        if entry is None:
            violations.append(Violation(path, 1, f"missing module {relative}"))
            continue
        file_path, source, tree = entry
        target = _functions(tree).get(function)
        if target is None:
            violations.append(
                Violation(file_path, 1, f"{relative} has no function {function}")
            )
            continue
        segment = ast.get_source_segment(source, target) or ""
        if not re.search(pattern, segment):
            violations.append(
                Violation(
                    file_path,
                    target.lineno,
                    f"{relative}::{function} lost its guard /{pattern}/ ({why})",
                )
            )

    # 4. Listed readers that no longer read the trail are stale entries.
    for relative, function, why in sorted(registry.exempt_readers):
        if (relative, function) not in sites:
            path = package_root / relative
            violations.append(
                Violation(
                    path,
                    1,
                    f"stale exemption: {relative}::{function} no longer reads "
                    f"the activity table — delete the entry ({why})",
                )
            )
    return violations


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check that every native-only activity reader excludes "
        "imported rows"
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        default=PACKAGE_ROOT,
        help="Athena package root (default: repository src/athena)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    package_root = args.package_root.absolute()

    try:
        violations = find_violations(package_root)
    except (GuardCheckError, OSError, UnicodeError) as exc:
        print(f"Athena imported_at guards failed: {exc}", file=sys.stderr)
        return 1

    if violations:
        count = len(violations)
        print(
            f"Athena imported_at guards failed: {count} "
            f"{'violation' if count == 1 else 'violations'}",
            file=sys.stderr,
        )
        for violation in violations:
            print(
                f"{violation.path}:{violation.line}: {violation.detail}",
                file=sys.stderr,
            )
        return 1

    print(
        "Athena imported_at guards passed: every native-only activity reader "
        "excludes foreign history"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
