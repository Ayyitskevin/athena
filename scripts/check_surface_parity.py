"""Fail when a command is reached from only one transport and nobody said why.

Athena has two transports over one command layer: the server-rendered cockpit
(everything under ``web/``) and the REST API (every ``*_api.py`` and
``api.py``). A capability one of them reaches and the other does not is a gap
an operator or an agent meets at run time: the review behind C10
(docs/plans/ATHENA_IN_USE_SPEC.md) found assign-to-seat, rooms and the
self-service password change only in the cockpit, and the agent loop, Icarus
dispatch and event sources only in the API, none of it written down. Some of
those gaps are right and some are not; this check makes each one a decision on
the record. A command function reached from one transport only fails the build
unless ``scripts/surface_parity.toml`` declares it, with its kind and a reason;
a declaration that no longer describes the tree fails too, so the list can only
shrink with the gaps it records.

MCP is out of scope. The ``athena-mcp`` server is a client of REST (its tools
call the API through ``mcp/client.py``), so a command REST reaches is reachable
by a tool or deliberately not, and that choice is the tool declarations' to
make. ``main.py``, the composition root, is not a transport here either: it
wires both. Its JSON refusal handlers do reach commands for REST, and the
declared list names the ones that matter (kind ``elsewhere``).

What "reached" means. A command function is a module-level ``def`` in a module
named ``*_commands.py``. A transport module reaches every one it names
anywhere in its file, called or not (``Depends(...)``, a callback, a table):
an attribute of an imported command module however it was imported or aliased
(``from athena.aegis import issue_commands as ic``, ``import
athena.aegis.issue_commands``, relative forms), or a name imported from one.
Then the names it uses in other modules are followed, to any depth: a helper
module's module-level function, class or assignment reaches what its body or
value names. The walk stops at a command module, so a command that only other
commands call is reached by nobody, and the cockpit reaching
``lease_commands.claim`` does not reach the predicates ``claim`` uses.

What it cannot see: a command reached by a string (``getattr``, a dispatch on
a name), through a registry filled at import time (undo handlers, feed
observers), through a method called on an object, or through a re-export from
a package ``__init__``. It does not compare behaviour, only names: two
functions in a ``pair`` row are the same capability because the row says so.
And it measures commands, not read models: a page that renders a read module
the API never serves is outside it.

The declared list. ``scripts/surface_parity.toml`` holds one ``[[row]]`` per
capability, read top to bottom; its header says what each kind means. Function
names are written without the ``athena.`` prefix. The check fails on an
undeclared one-sided command; on a row naming a function that does not exist,
that no transport reaches, that both reach, or that is reached only from the
side the row does not name; and on a function declared in two rows.

Standard library only; it never imports Athena. Usage:

    python scripts/check_surface_parity.py [--package-root src/athena]
        [--declarations scripts/surface_parity.toml]
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import tomllib
from typing import Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"
DECLARATIONS = Path(__file__).resolve().with_name("surface_parity.toml")
ROOT_PACKAGE = "athena"
COMMAND_SUFFIX = "_commands"
COCKPIT, API = "cockpit", "api"
SIDE_NAMES = {COCKPIT: "the cockpit", API: "the REST API"}

# The kinds a row can be, and what each one claims. A ``pair`` names a function
# on each side; every other kind names functions on one side only.
KINDS = {
    "pair": "one capability, reached under a different name on each side",
    "read": "a read or predicate a transport uses to render or gate, not a capability",
    "by-design": "a capability that belongs to one transport",
    "elsewhere": "the other transport reaches it by a path this check does not follow",
    "missing-twin": "a capability the other transport lacks and someone needs",
}
_ROW_KEYS = frozenset({"kind", "cockpit", "api", "reason"})

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

Name = tuple[str, str]  # (absolute module, attribute)


class ParityCheckError(RuntimeError):
    """The package or the declared list could not be read safely."""


@dataclass(frozen=True)
class Row:
    kind: str
    cockpit: tuple[str, ...]
    api: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Violation:
    subject: str  # The command function, without the ``athena.`` prefix.
    detail: str


@dataclass(frozen=True)
class Report:
    cockpit: frozenset[str]  # Command functions the cockpit reaches.
    api: frozenset[str]  # Command functions the REST API reaches.
    rows: tuple[Row, ...]
    violations: list[Violation]

    @property
    def one_sided(self) -> frozenset[str]:
        return self.cockpit ^ self.api


# --- the declared list ------------------------------------------------------


def parse_declarations(text: str, source: str = "<declarations>") -> tuple[Row, ...]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ParityCheckError(f"cannot parse {source}: {exc}") from exc
    if extra := set(data) - {"row"}:
        raise ParityCheckError(f"{source}: unknown table {sorted(extra)}")
    rows = []
    for number, entry in enumerate(data.get("row", []), start=1):
        where = f"{source}: row {number}"
        if not isinstance(entry, dict):
            raise ParityCheckError(f"{where}: not a table")
        if extra := set(entry) - _ROW_KEYS:
            raise ParityCheckError(f"{where}: unknown key {sorted(extra)}")
        kind = entry.get("kind")
        if kind not in KINDS:
            raise ParityCheckError(f"{where}: kind must be one of {sorted(KINDS)}")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ParityCheckError(f"{where}: a row needs a reason")
        sides = {side: entry.get(side, []) for side in (COCKPIT, API)}
        for side, names in sides.items():
            if not isinstance(names, list) or not all(
                isinstance(name, str) and _is_command_name(name) for name in names
            ):
                raise ParityCheckError(
                    f"{where}: {side} must list <package>.<module>{COMMAND_SUFFIX}"
                    ".<function> names"
                )
        if not sides[COCKPIT] and not sides[API]:
            raise ParityCheckError(f"{where}: names no command")
        if kind == "pair" and not (sides[COCKPIT] and sides[API]):
            raise ParityCheckError(f"{where}: a pair names functions on both sides")
        if kind != "pair" and sides[COCKPIT] and sides[API]:
            raise ParityCheckError(f"{where}: a {kind} row names one side only")
        rows.append(
            Row(
                kind, tuple(sides[COCKPIT]), tuple(sides[API]), " ".join(reason.split())
            )
        )
    return tuple(rows)


def _is_command_name(name: str) -> bool:
    module, _, function = name.rpartition(".")
    return (
        module.endswith(COMMAND_SUFFIX)
        and "." in module
        and function.isidentifier()
        and not name.startswith(f"{ROOT_PACKAGE}.")
    )


def load_declarations(path: Path = DECLARATIONS) -> tuple[Row, ...]:
    return parse_declarations(path.read_text(encoding="utf-8"), path.name)


# --- the package ------------------------------------------------------------


@dataclass
class _Module:
    name: str
    path: Path
    tree: ast.Module
    imports: dict[str, tuple[str, str | None]]

    @property
    def is_command(self) -> bool:
        return self.name.rpartition(".")[2].endswith(COMMAND_SUFFIX)

    def side(self) -> str | None:
        parts = self.name.split(".")
        if len(parts) > 1 and parts[1] == "web":
            return COCKPIT
        if parts[-1] == "api" or parts[-1].endswith("_api"):
            return API
        return None


def _module_name(package_root: Path, path: Path) -> str:
    parts = list(path.relative_to(package_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((ROOT_PACKAGE, *parts))


def _import_table(
    tree: ast.Module, *, module: str, path: Path
) -> dict[str, tuple[str, str | None]]:
    """Bound name -> (absolute module, imported attribute or None)."""
    names: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = (alias.name, None)
                else:
                    top = alias.name.partition(".")[0]
                    names[top] = (top, None)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = (
                    module if path.name == "__init__.py" else module.rpartition(".")[0]
                )
                try:
                    base = importlib.util.resolve_name("." * node.level + base, package)
                except ImportError as exc:
                    raise ParityCheckError(
                        f"cannot resolve relative import in {path}:{node.lineno}"
                    ) from exc
            for alias in node.names:
                names[alias.asname or alias.name] = (base, alias.name)
    return names


def _inventory(package_root: Path) -> dict[str, _Module]:
    if package_root.name != ROOT_PACKAGE or not package_root.is_dir():
        raise ParityCheckError(
            f"package root must be an existing directory named {ROOT_PACKAGE!r}: "
            f"{package_root}"
        )
    modules: dict[str, _Module] = {}
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        name = _module_name(package_root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise ParityCheckError(
                f"cannot parse {path}:{exc.lineno}: {exc.msg}"
            ) from exc
        modules[name] = _Module(
            name, path, tree, _import_table(tree, module=name, path=path)
        )
    return modules


def _references(node: ast.AST, module: _Module) -> set[Name]:
    """Every (module, attribute) the expressions under ``node`` name."""
    found: set[Name] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            if child.id in module.imports:
                base, attribute = module.imports[child.id]
                if attribute is not None:
                    found.add((base, attribute))
            else:
                found.add((module.name, child.id))
        elif isinstance(child, ast.Attribute):
            chain = [child.attr]
            value = child.value
            while isinstance(value, ast.Attribute):
                chain.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name) and value.id in module.imports:
                base, attribute = module.imports[value.id]
                chain.reverse()
                parts = chain if attribute is None else [attribute, *chain]
                # Everything before the last name is a module path.
                found.add((".".join((base, *parts[:-1])), parts[-1]))
    return found


def _top_level(module: _Module) -> dict[str, list[ast.AST]]:
    """Each module-level definition or assigned name, and the nodes it runs."""
    definitions: dict[str, list[ast.AST]] = {}
    for node in module.tree.body:
        if isinstance(node, _DEFINITIONS):
            definitions.setdefault(node.name, []).append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        definitions.setdefault(name.id, []).append(node)
    return definitions


def _reach(modules: dict[str, _Module]) -> tuple[set[str], dict[str, set[str]]]:
    """(every command function, the command functions each side reaches)."""
    commands: set[Name] = set()
    bodies: dict[Name, list[ast.AST]] = {}
    for module in modules.values():
        if module.is_command:
            commands.update(
                (module.name, node.name)
                for node in module.tree.body
                if isinstance(node, _FUNCTIONS)
            )
        for name, nodes in _top_level(module).items():
            bodies[(module.name, name)] = nodes
    reached: dict[str, set[str]] = {COCKPIT: set(), API: set()}
    for side in reached:
        seen: set[Name] = set()
        pending = [
            reference
            for module in modules.values()
            if module.side() == side
            for reference in _references(module.tree, module)
        ]
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            if name in commands:
                reached[side].add(_short(name))
                continue  # The command layer's own calls are not a transport's.
            if name in bodies:
                module = modules[name[0]]
                for node in bodies[name]:
                    pending.extend(_references(node, module))
    return {_short(name) for name in commands}, reached


def _short(name: Name) -> str:
    return f"{name[0].removeprefix(ROOT_PACKAGE + '.')}.{name[1]}"


# --- the check --------------------------------------------------------------


def check(
    package_root: Path = PACKAGE_ROOT, rows: Iterable[Row] | None = None
) -> Report:
    modules = _inventory(package_root.absolute())
    for side in (COCKPIT, API):
        if not any(module.side() == side for module in modules.values()):
            raise ParityCheckError(
                f"no {side} modules under {package_root}: there is nothing to compare"
            )
    rows = load_declarations() if rows is None else tuple(rows)
    commands, reached = _reach(modules)
    cockpit, api = reached[COCKPIT], reached[API]
    violations: list[Violation] = []

    declared = Counter(name for row in rows for name in (*row.cockpit, *row.api))
    for name in sorted(declared):
        if declared[name] > 1:
            violations.append(
                Violation(name, f"declared in {declared[name]} rows; keep one")
            )
    for row in rows:
        for side, names in ((COCKPIT, row.cockpit), (API, row.api)):
            for name in names:
                if (problem := _stale(name, side, commands, cockpit, api)) is not None:
                    violations.append(Violation(name, problem))
    for name in sorted((cockpit ^ api) - set(declared)):
        side = COCKPIT if name in cockpit else API
        violations.append(
            Violation(
                name,
                f"reached only from {SIDE_NAMES[side]} and not declared: give it a "
                f"twin, or a row in {DECLARATIONS.name} with its kind and reason",
            )
        )
    violations.sort(key=lambda v: (v.subject, v.detail))
    return Report(frozenset(cockpit), frozenset(api), rows, violations)


def _stale(
    name: str, side: str, commands: set[str], cockpit: set[str], api: set[str]
) -> str | None:
    if name not in commands:
        return "declared, but there is no such command function"
    on = {COCKPIT: name in cockpit, API: name in api}
    if on[COCKPIT] and on[API]:
        return "declared, but reached from both transports now: delete it from the row"
    if not on[COCKPIT] and not on[API]:
        return "declared, but no transport reaches it now: delete it from the row"
    if not on[side]:
        other = API if side == COCKPIT else COCKPIT
        return f"declared under {side}, but it is reached only from {SIDE_NAMES[other]}"
    return None


def find_violations(
    package_root: Path = PACKAGE_ROOT, rows: Iterable[Row] | None = None
) -> list[Violation]:
    return check(package_root, rows).violations


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="fail on a command reached from one transport and not declared"
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        default=PACKAGE_ROOT,
        help="Athena package root (default: repository src/athena)",
    )
    parser.add_argument(
        "--declarations",
        type=Path,
        default=DECLARATIONS,
        help="the declared list (default: scripts/surface_parity.toml)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        report = check(args.package_root, load_declarations(args.declarations))
    except (ParityCheckError, OSError, UnicodeError) as exc:
        print(f"Athena surface parity failed: {exc}", file=sys.stderr)
        return 1
    if report.violations:
        count = len(report.violations)
        print(
            f"Athena surface parity failed: {count} "
            f"{'violation' if count == 1 else 'violations'}",
            file=sys.stderr,
        )
        for violation in report.violations:
            print(f"{violation.subject}: {violation.detail}", file=sys.stderr)
        return 1
    pairs = sum(1 for row in report.rows if row.kind == "pair")
    print(
        "Athena surface parity passed: "
        f"{len(report.cockpit)} command functions reached from the cockpit, "
        f"{len(report.api)} from the REST API, "
        f"{len(report.cockpit & report.api)} from both; "
        f"{len(report.one_sided)} one-sided, every one declared "
        f"({len(report.rows)} rows, {pairs} of them pairs)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
