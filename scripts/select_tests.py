"""Choose the tests a change can affect, for ``scripts/gate.sh --profile changed``.

Everything here is static: files are parsed with ``ast`` or read as text, and
nothing is imported or collected, so a selection takes a few seconds. Each
changed path is judged by these rules, in order:

1. Safety valves. A path matching ``FULL_SUITE_VALVES`` affects tests that no
   static rule can bound, so the full suite runs.
2. The import graph. Python modules under ``src/``, ``tests/`` and
   ``scripts/`` are nodes. An ``import`` anywhere in a file (relative forms
   resolved), the parent packages an import executes, and, in a test, a string
   that names an ``athena`` module (a monkeypatch target or a dynamic import)
   are edges. A changed module selects every test file that reaches it, so a
   changed test selects itself and a changed helper selects the tests that
   import it. The graph is not followed through ``COMPOSITION_ROOTS``: they
   import nearly every module to assemble the app, so following them would
   select every test that builds the app for every change.
3. The map. For Python modules it is the generated map, ``MAP_FILE``
   (``scripts/test_map.json``): for each source file under ``src/athena``,
   the test files whose tests executed it, recorded with per-test coverage
   contexts, less those the graph already selects for it. That is how a router
   reached over HTTP, a page and a helper used only by the app find their
   tests. ``scripts/generate_test_map.py`` writes it, and the scheduled
   ``Test map`` workflow fails when the committed map is stale. Regenerate it
   with one command (it runs the suite)::

       .venv/bin/python scripts/generate_test_map.py --record

   Coverage does not measure templates, static assets or documents, so
   ``TEST_MAP`` keeps hand entries for those, each with its reason. Both are
   applied to every changed path and to every module the graph reaches.
4. References. A test whose string constants (docstrings aside) name the
   changed path, its file name, or a directory containing it (a path built
   with ``/`` or ``Path(...)``) is selected, and so is a test that names a
   script the graph reaches: tests run scripts by path as often as by import.
5. Nothing else. A changed path that none of these rules maps, or a change that
   reaches a module only a composition root imports and the map does not name,
   runs the full suite, and the receipt says which path did it. So does a
   change to a file the generated map leaves out because more than half the
   recorded test files run it (the middleware, which every request passes
   through). Such a file that the graph merely reaches adds no tests and forces
   nothing: the tests that run the changed file are in its own entry.

Blind spots of rule 2 plus 3: a test that reaches a module only through the app
is selected only when the recorded coverage saw it run the module. A test
added since the map was generated, a change to what a module does at import
time (coverage gives import time no test), and code a test runs in a
subprocess are missed until the map is regenerated or the product profile
runs everything.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
import json
from pathlib import Path
import re
import subprocess

# Modules that wire the app together. Their own change runs the full suite.
COMPOSITION_ROOTS = frozenset({"athena.main", "athena.web", "athena.web.router"})

# The generated map of tests that reach a source file through the app.
MAP_FILE = "scripts/test_map.json"

# (path pattern, why): a changed path that matches runs the full suite.
FULL_SUITE_VALVES = (
    ("pyproject.toml", "project, pytest and coverage configuration"),
    ("constraints/**", "the pinned dependency set"),
    ("**/conftest.py", "pytest fixtures every test in its directory uses"),
    ("src/athena/core/db.py", "the database layer every test builds on"),
    ("src/athena/core/migrations/**", "the schema every test database applies"),
    ("src/athena/main.py", "the app's composition root"),
    ("src/athena/web/__init__.py", "a composition root of the web layer"),
    ("src/athena/web/router.py", "a composition root of the web layer"),
    ("scripts/gate.py", "the gate itself"),
    ("scripts/gate.sh", "the gate itself"),
    ("scripts/select_tests.py", "this selection and its map"),
)

_WEB_TESTS = (
    "tests/test_web.py",
    "tests/test_web_*.py",
    "tests/test_*_web.py",
    "tests/test_*_web_*.py",
    "tests/test_*_ui.py",
)


@dataclass(frozen=True)
class MapEntry:
    """Changed paths matching ``source`` select the test files ``tests`` match.

    Patterns are relative to the checkout: ``*`` stays within one directory,
    ``**`` spans directories, and ``{name}`` in ``source`` captures one path
    segment that is substituted into ``tests``.
    """

    source: str
    tests: tuple[str, ...]
    why: str


# Hand entries for what per-test coverage cannot see: files that are not Python
# modules, reached by being rendered, served or read. Python modules reached
# through the app take their tests from the generated map (MAP_FILE).
TEST_MAP = (
    MapEntry(
        "src/athena/templates/**",
        (*_WEB_TESTS, "tests/test_template_contracts.py"),
        "templates, rendered by browser routes; coverage does not measure them",
    ),
    MapEntry(
        "src/athena/static/**",
        (
            *_WEB_TESTS,
            "tests/test_vendored_assets.py",
            "tests/test_static_cache_and_nav_rollup.py",
            "tests/test_csp_no_unsafe_inline.py",
        ),
        "static assets, served to browser pages; coverage does not measure them",
    ),
    MapEntry(
        "src/athena/field_guide/**",
        ("tests/test_field_guide.py",),
        "field guide pages, markdown the guide reads; coverage does not measure them",
    ),
    MapEntry(
        "**/*.md",
        (),
        "a document: tests that read one name it and are selected by reference",
    ),
)


@dataclass(frozen=True)
class GeneratedMap:
    """``MAP_FILE``: source path -> test files that ran it through the app."""

    tests: dict[str, tuple[str, ...]]
    ubiquitous: dict[str, int]  # Left out: run by most recorded test files.
    recorded: int  # Test files the recorded coverage saw.


def load_map(root: Path) -> GeneratedMap:
    """The committed generated map; an empty one when the file is absent."""
    path = root / MAP_FILE
    if not path.exists():
        return GeneratedMap({}, {}, 0)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = document["map"]
        ubiquitous = document["ubiquitous"]
        recorded = document["recorded_test_files"]
        if not (
            isinstance(entries, dict)
            and all(
                isinstance(tests, list) and all(isinstance(t, str) for t in tests)
                for tests in entries.values()
            )
            and isinstance(ubiquitous, dict)
            and all(isinstance(count, int) for count in ubiquitous.values())
            and isinstance(recorded, int)
        ):
            raise ValueError("unexpected shape")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SelectionError(
            f"{MAP_FILE} is unreadable ({error}); regenerate it with "
            "scripts/generate_test_map.py"
        ) from error
    return GeneratedMap(
        {source: tuple(tests) for source, tests in entries.items()},
        dict(ubiquitous),
        recorded,
    )


class SelectionError(Exception):
    """The requested base cannot be used; the gate reports it and stops."""


@dataclass(frozen=True)
class Diff:
    requested_base: str
    base: str | None  # The ref actually used; None when none could be.
    merge_base: str | None
    paths: tuple[str, ...]
    note: str | None  # Why the base differs from the request, or is missing.


@dataclass(frozen=True)
class Plan:
    changed: tuple[str, ...]
    test_files: tuple[str, ...]  # Every test file when full_suite is set.
    total_test_files: int
    full_suite: tuple[str, ...]  # "path: reason" for each path that forced it.
    by_rule: tuple[tuple[str, int], ...]  # (rule, test files it selected).
    explanations: tuple[str, ...]  # One line per changed path.


@cache
def _pattern(glob: str) -> re.Pattern[str]:
    parts = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            parts.append(".*")
            index += 2
        elif glob.startswith("{name}", index):
            parts.append("(?P<name>[^/]+)")
            index += 6
        elif glob[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def matches(glob: str, path: str) -> re.Match[str] | None:
    return _pattern(glob).match(path)


def map_tests(
    entry: MapEntry, path: str, test_files: Iterable[str]
) -> tuple[str, ...] | None:
    """The test files ``entry`` selects for ``path``; None if it does not apply."""
    match = matches(entry.source, path)
    if match is None:
        return None
    name = match.groupdict().get("name")
    globs = [
        test.replace("{name}", name) if name is not None else test
        for test in entry.tests
    ]
    return tuple(
        sorted(
            test for test in test_files if any(matches(glob, test) for glob in globs)
        )
    )


def valve(path: str) -> str | None:
    for glob, why in FULL_SUITE_VALVES:
        if matches(glob, path):
            return why
    return None


# --- the static index -------------------------------------------------------

_SOURCE_ROOTS = ("src", "tests", "scripts")
_ATHENA_NAME = re.compile(r"athena(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _module_name(path: str) -> str | None:
    top, _, rest = path.partition("/")
    if top not in _SOURCE_ROOTS or not rest.endswith(".py"):
        return None
    parts = rest[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _parents(module: str) -> Iterator[str]:
    while "." in module:
        module = module.rpartition(".")[0]
        yield module


def _flatten(node: ast.expr) -> list[str | None]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _flatten(node.left) + _flatten(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and (
            (isinstance(node.func, ast.Name) and node.func.id.endswith("Path"))
            or (
                isinstance(node.func, ast.Attribute) and node.func.attr.endswith("Path")
            )
        )
    ):
        return [node.args[0].value]
    return [None]


@dataclass(frozen=True)
class _Source:
    imports: frozenset[str]  # Dotted names, unresolved.
    strings: frozenset[str]  # Every string constant, f-string parts included.
    paths: frozenset[str]  # Paths built with "/" or Path(...): files or dirs.
    text: str  # The strings joined, searched as one block.


def _read_source(text: str, module: str, is_package: bool) -> _Source:
    imports: set[str] = set()
    strings: set[str] = set()
    paths: set[str] = set()
    docstrings: set[int] = set()
    left_of_division: set[int] = set()
    candidates: list[ast.expr] = []  # Path expressions: "/" chains, Path(...).
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstrings:
                strings.add(node.value)
        elif isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Div):
                left_of_division.add(id(node.left))
                candidates.append(node)
        elif isinstance(node, ast.Call):
            candidates.append(node)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = module.split(".") if is_package else module.split(".")[:-1]
                package = package[: len(package) - (node.level - 1)]
                base = ".".join(package + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            imports.add(base)
            imports.update(f"{base}.{alias.name}" for alias in node.names)
        if (
            isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
        ):
            # ast.walk visits a body's statements after their parent, so the
            # docstring is known before its constant is reached.
            docstrings.add(id(node.body[0].value))
    for node in candidates:
        if id(node) in left_of_division:
            continue  # Part of a longer chain, which is read whole.
        segments = _flatten(node)
        start = 0
        while start < len(segments) and segments[start] is None:
            start += 1
        run: list[str] = []
        for segment in segments[start:]:
            if segment is None:
                break
            run.append(segment.strip("/"))
        joined = "/".join(part for part in run if part and part != ".")
        if joined and joined != "..":
            paths.add(joined)
    return _Source(
        frozenset(imports), frozenset(strings), frozenset(paths), "\0".join(strings)
    )


class Index:
    """The import graph and string references of one checkout."""

    def __init__(self, root: Path, *, with_map: bool = True) -> None:
        self.root = root
        # The generator builds an index without the map it is about to replace.
        self.generated = load_map(root) if with_map else GeneratedMap({}, {}, 0)
        files = sorted(
            path.relative_to(root).as_posix()
            for top in _SOURCE_ROOTS
            if (root / top).is_dir()
            for path in (root / top).rglob("*.py")
            if "__pycache__" not in path.parts
        )
        self.module_paths: dict[str, str] = {}
        for path in files:
            module = _module_name(path)
            if module is not None and module not in self.module_paths:
                self.module_paths[module] = path
        self.path_modules = {path: module for module, path in self.module_paths.items()}
        self.test_files = tuple(path for path in files if _is_test_file(path))
        self.sources: dict[str, _Source] = {}
        for module, path in self.module_paths.items():
            try:
                text = (root / path).read_text(encoding="utf-8")
                self.sources[module] = _read_source(
                    text, module, path.endswith("/__init__.py")
                )
            except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
                # An unparsable file still exists; it just contributes no edges.
                self.sources[module] = _Source(
                    frozenset(), frozenset(), frozenset(), ""
                )
        self.importers: dict[str, set[str]] = {}
        for module, source in self.sources.items():
            names = set(source.imports)
            if self.module_paths[module].startswith("tests/"):
                names.update(s for s in source.strings if _ATHENA_NAME.match(s))
            targets = {self.resolve(name) for name in names}
            targets.update(_parents(module))
            for target in targets:
                if target is not None and target != module:
                    self.importers.setdefault(target, set()).add(module)

    def resolve(self, name: str) -> str | None:
        while name:
            if name in self.module_paths:
                return name
            name = name.rpartition(".")[0]
        return None

    def add_missing(self, path: str) -> None:
        """Index a deleted module, so the tests that still import it are found."""
        module = _module_name(path)
        if module is None or module in self.module_paths:
            return
        self.module_paths[module] = path
        self.path_modules[path] = module
        for holder, source in self.sources.items():
            names = set(source.imports)
            if self.module_paths[holder].startswith("tests/"):
                names.update(s for s in source.strings if _ATHENA_NAME.match(s))
            if any(self.resolve(name) == module for name in names):
                self.importers.setdefault(module, set()).add(holder)

    def reached(self, module: str) -> set[str]:
        """Modules that import ``module``, directly or not, short of the roots."""
        seen = {module}
        pending = deque([module])
        while pending:
            for importer in self.importers.get(pending.popleft(), ()):
                if importer in seen or importer in COMPOSITION_ROOTS:
                    continue
                seen.add(importer)
                pending.append(importer)
        return seen

    def only_roots_import(self, module: str) -> bool:
        """True when composition roots are the only modules that import it."""
        importers = {
            importer
            for importer in self.importers.get(module, ())
            if not importer.startswith(module + ".")
            and self.module_paths[importer].startswith("src/")
        }
        return bool(importers) and importers <= COMPOSITION_ROOTS

    def referrers(self, path: str) -> set[str]:
        """Test modules (helpers included) whose strings name ``path``."""
        name = path.rsplit("/", 1)[-1]
        by_name = not (
            path.endswith(".py") and path.startswith(("src/athena/", "tests/"))
        )
        pattern = (
            re.compile(r"(?<![\w.\-])" + re.escape(name) + r"(?![\w\-])")
            if by_name
            else None
        )
        return {
            module
            for module, source in self.sources.items()
            if self.module_paths[module].startswith("tests/")
            and self.module_paths[module] != path
            and _names(source, path, pattern)
        }


def _names(source: _Source, path: str, name_pattern: re.Pattern[str] | None) -> bool:
    for built in source.paths:
        if path == built or path.endswith("/" + built):
            return True
        if path.startswith(built + "/"):
            return True
        # A path built from a relative root ("Path(__file__).parent / 'a' / 'b'")
        # can name a directory anywhere; one bare segment is too common to trust.
        if "/" in built and f"/{built}/" in path:
            return True
    if path in source.text:
        return True
    return name_pattern is not None and name_pattern.search(source.text) is not None


def everything(root: Path, reason: str) -> Plan:
    """The full suite, for a change whose paths could not be listed."""
    tests = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in (root / "tests").rglob("*.py")
            if _is_test_file(path.relative_to(root).as_posix())
        )
    )
    return Plan((), tests, len(tests), (reason,), (), ())


def named(index: Index, path: str) -> bool:
    """True when the generated map or a hand entry names tests for ``path``."""
    return path in index.generated.tests or any(
        matches(entry.source, path) for entry in TEST_MAP
    )


def select(root: Path, changed: Iterable[str], index: Index | None = None) -> Plan:
    """Select the test files under ``root`` that ``changed`` paths can affect."""
    index = index or Index(root)
    changed = tuple(sorted(set(changed)))
    for path in changed:
        if not (root / path).exists():
            index.add_missing(path)
    existing = {test for test in index.test_files if (root / test).exists()}
    selected: dict[str, set[str]] = {}  # test file -> rules that selected it
    full_suite: list[str] = []
    explanations: list[str] = []

    def tests_in(modules: Iterable[str]) -> set[str]:
        paths = {index.module_paths[module] for module in modules}
        return {path for path in paths if path in existing}

    for path in changed:
        why = valve(path)
        if why is not None:
            full_suite.append(f"{path}: safety valve ({why})")
            explanations.append(f"{path}: FULL SUITE, safety valve: {why}")
            continue
        module = index.path_modules.get(path)
        graph = index.reached(module) if module is not None else set()
        # Tests that name the path, and tests that name a script the graph
        # reaches: tests run scripts by path as often as they import them.
        holders = index.referrers(path)
        for script in graph:
            if index.module_paths[script].startswith("scripts/"):
                holders |= index.referrers(index.module_paths[script])
        referenced: set[str] = set()
        for holder in holders:
            referenced |= index.reached(holder)
        mapped: set[str] = set()
        for source in [path] + [index.module_paths[m] for m in graph if m != module]:
            if source.startswith("src/"):
                mapped |= existing.intersection(index.generated.tests.get(source, ()))
                for entry in TEST_MAP:
                    mapped |= set(map_tests(entry, source, existing) or ())
        found = {
            "graph": tests_in(graph),
            "map": mapped,
            "reference": tests_in(referenced),
        }
        # A module nearly every recorded test runs: any of them can be affected.
        ubiquitous = index.generated.ubiquitous.get(path)
        # A module reached only through the app that the recorded coverage never
        # saw run (new since the map, or never recorded): its tests are unknown.
        # One the map leaves out as ubiquitous is no such gap when it is merely
        # reached: the tests that run the changed file are in its own entry.
        unbounded = sorted(
            m
            for m in graph
            if index.module_paths[m].startswith("src/")
            and index.only_roots_import(m)
            and not named(index, index.module_paths[m])
            and index.module_paths[m] not in index.generated.ubiquitous
        )
        if ubiquitous is not None:
            reason = (
                f"{ubiquitous} of the {index.generated.recorded} recorded test files "
                "run it, so the map leaves it out"
            )
        elif unbounded:
            what = (
                "only a composition root imports it"
                if module in unbounded
                else f"it reaches {unbounded[0]}, which only a composition root imports"
            )
            reason = f"{what} and the map does not name its tests"
        elif any(found.values()):
            for rule, tests in found.items():
                for test in tests:
                    selected.setdefault(test, set()).add(rule)
            counts = ", ".join(f"{rule} {len(tests)}" for rule, tests in found.items())
            explanations.append(f"{path}: {counts}")
            continue
        elif (own := [e for e in TEST_MAP if matches(e.source, path)]) and all(
            not entry.tests for entry in own
        ):
            explanations.append(f"{path}: no test selected ({own[0].why})")
            continue
        else:
            reason = "no import, map entry or reference reaches it"
        full_suite.append(f"{path}: unmapped ({reason})")
        explanations.append(f"{path}: FULL SUITE, unmapped: {reason}")
    by_rule = tuple(
        (rule, sum(1 for rules in selected.values() if rule in rules))
        for rule in ("graph", "map", "reference")
    )
    every_test = tuple(sorted(existing))
    return Plan(
        changed,
        every_test if full_suite else tuple(sorted(selected)),
        len(every_test),
        tuple(full_suite),
        by_rule,
        tuple(explanations),
    )


# --- the diff ---------------------------------------------------------------


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments), cwd=root, check=False, capture_output=True, text=True
    )


def _commit(root: Path, ref: str) -> str | None:
    result = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def diff(root: Path, requested: str, explicit: bool) -> Diff:
    """Paths changed between the merge base with ``requested`` and the worktree.

    Includes staged, unstaged and untracked files. A missing default base falls
    back to ``main``; with neither, the paths are unknown and the full suite
    runs. A missing base the caller named is a SelectionError.
    """
    base = requested
    note = None
    if _commit(root, base) is None:
        if explicit:
            raise SelectionError(f"base {requested!r} is not a commit in this checkout")
        if requested != "main" and _commit(root, "main") is not None:
            base = "main"
            note = f"{requested} not found; using local main"
        else:
            return Diff(requested, None, None, (), f"{requested} not found")
    result = _git(root, "merge-base", base, "HEAD")
    if result.returncode:
        return Diff(
            requested,
            base,
            None,
            (),
            f"no merge base between {base} and HEAD (a shallow clone?)",
        )
    merge_base = result.stdout.strip()
    changed = _git(root, "diff", "--name-only", "--no-renames", "-z", merge_base)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    for listing in (changed, untracked):
        if listing.returncode:
            raise SelectionError(f"git failed: {listing.stderr.strip()}")
    paths = {
        path
        for listing in (changed, untracked)
        for path in listing.stdout.split("\0")
        if path
    }
    return Diff(requested, base, merge_base, tuple(sorted(paths)), note)
