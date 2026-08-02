"""Enforce Athena's core, feature-module, and web dependency direction.

The contract is deliberately narrow:

    web -> (aegis | mentor) -> core

Aegis and Mentor are independent peers. ``athena.main`` is the composition root:
it may import the layers, but the layers may not import it. Config, MCP, and
operator entrypoints otherwise sit outside this contract.

The checker parses every .py source file present under the package root and
follows the internal import graph, so an uncontracted helper cannot hide an
indirect forbidden import. Recognized calls through bare __import__ and
importlib.import_module use a small explicit grammar: importlib is imported
directly at the top level, loaders are called in place, and targets are literal.
External targets stay outside the layer graph. Explicit import-machinery
re-exports and machinery-acquisition expressions fail closed; arbitrary
computed reflection, eval/exec, and custom import hooks are out of scope. The
checker uses only the standard library and never imports Athena application code.
"""

from __future__ import annotations

import argparse
import ast
from collections import deque
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from typing import Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"
ROOT_PACKAGE = "athena"
COMPOSITION_ROOT = f"{ROOT_PACKAGE}.main"
MACHINERY_MODULE_ROOTS = frozenset(
    {"_frozen_importlib", "_frozen_importlib_external", "builtins", "importlib"}
)
MACHINERY_IMPORT_NAMES = MACHINERY_MODULE_ROOTS | {
    "__builtins__",
    "__import__",
    "getattr",
    "import_module",
}

# Earlier entries are outer layers and may depend on later entries. Containers
# sharing one entry are independent peers, not mutually importable siblings.
#
# `workflows` sits between the transports and the modules for exactly one job:
# a command that must READ one module and WRITE another. Aegis and Mentor are
# peers by design — neither may import the other — and `web` cannot own that
# work because the cardinal rule bars transports from owning authorization. A
# playbook (a checklist page that creates issues) is the first such command;
# without this layer it had no legal home at all. The rules are unchanged
# otherwise: workflows may import both modules and core, and NOTHING may import
# workflows except the transports above it.
LAYERS = (("web",), ("workflows",), ("aegis", "mentor"), ("core",))
_LAYER_BY_AREA = {
    area: layer_index for layer_index, layer in enumerate(LAYERS) for area in layer
}


class ImportContractError(RuntimeError):
    """The package could not be analyzed safely."""


@dataclass(frozen=True)
class ImportEdge:
    """One statically resolved import between Athena modules."""

    importer: str
    imported: str
    path: Path
    line: int
    statement: str


@dataclass(frozen=True)
class Violation:
    """A forbidden dependency path from one contracted area to another."""

    source_area: str
    target_area: str
    edges: tuple[ImportEdge, ...]


@dataclass(frozen=True)
class DynamicImportAliases:
    """Supported importlib and statically imported module names."""

    importlib_modules: frozenset[str]
    imported_modules: frozenset[str]


def _module_name(package_root: Path, path: Path) -> str:
    relative = path.relative_to(package_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((ROOT_PACKAGE, *parts))


def _inventory_modules(package_root: Path) -> dict[str, Path]:
    if package_root.name != ROOT_PACKAGE or not package_root.is_dir():
        raise ImportContractError(
            f"package root must be an existing directory named {ROOT_PACKAGE!r}: "
            f"{package_root}"
        )
    if package_root.is_symlink():
        raise ImportContractError(
            f"symlinked package root is not supported: {package_root}"
        )

    symlinks = sorted(path for path in package_root.rglob("*") if path.is_symlink())
    if symlinks:
        rendered = "\n  ".join(str(path) for path in symlinks)
        raise ImportContractError(
            f"symlinked source paths are not supported:\n  {rendered}"
        )

    modules: dict[str, Path] = {}
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(package_root, path)
        if module in modules:
            raise ImportContractError(
                f"duplicate module identity {module}: {modules[module]} and {path}"
            )
        modules[module] = path
    if ROOT_PACKAGE not in modules:
        raise ImportContractError(
            f"package root is missing __init__.py: {package_root}"
        )
    for area in _LAYER_BY_AREA:
        if f"{ROOT_PACKAGE}.{area}" not in modules:
            raise ImportContractError(
                f"contracted package is missing __init__.py: {package_root / area}"
            )
    return modules


def _known_prefixes(module: str, known_modules: set[str]) -> set[str]:
    parts = module.split(".")
    return {
        ".".join(parts[:end])
        for end in range(1, len(parts) + 1)
        if ".".join(parts[:end]) in known_modules
    }


def _importer_package(importer: str, importer_path: Path) -> str:
    return (
        importer if importer_path.name == "__init__.py" else importer.rpartition(".")[0]
    )


def _resolve_from_base(
    node: ast.ImportFrom,
    *,
    importer: str,
    importer_path: Path,
) -> str:
    if node.level == 0:
        return node.module or ""

    package = _importer_package(importer, importer_path)
    relative_name = "." * node.level + (node.module or "")
    try:
        return importlib.util.resolve_name(relative_name, package)
    except ImportError as exc:
        raise ImportContractError(
            f"cannot resolve relative import in {importer_path}:{node.lineno}: "
            f"{relative_name}"
        ) from exc


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _is_machinery_module(module: str | None) -> bool:
    return bool(
        module
        and any(
            module == root or module.startswith(f"{root}.")
            for root in MACHINERY_MODULE_ROOTS
        )
    )


def _reject_nested_import_machinery(
    tree: ast.AST,
    importer_path: Path,
) -> None:
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(parents.get(node), ast.Module):
            continue
        if isinstance(node, ast.Import):
            binds_machinery = any(
                _is_machinery_module(alias.name) for alias in node.names
            )
        else:
            binds_machinery = node.level == 0 and _is_machinery_module(node.module)
        if binds_machinery:
            raise ImportContractError(
                f"dynamic import machinery must use a direct top-level import in "
                f"{importer_path}:{node.lineno}"
            )


def _reject_shadowed_builtins(tree: ast.AST, importer_path: Path) -> None:
    reserved = {"__builtins__", "__import__", "getattr"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound_names = {
                alias.asname or alias.name.partition(".")[0] for alias in node.names
            }
            imported_reserved_loader = set()
        elif isinstance(node, ast.ImportFrom):
            bound_names = {alias.asname or alias.name for alias in node.names}
            imported_reserved_loader = {
                alias.asname or alias.name
                for alias in node.names
                if node.level == 0
                and (
                    (node.module == "builtins" and alias.name in reserved)
                    or (node.module == "importlib" and alias.name == "__import__")
                )
            }
        else:
            bound_names = set()
            imported_reserved_loader = set()
        shadowed_import = sorted((bound_names & reserved) - imported_reserved_loader)
        if shadowed_import:
            raise ImportContractError(
                f"shadowing the reserved built-in name {shadowed_import[0]} is "
                f"not supported in {importer_path}:{node.lineno}"
            )

        for name in _bound_names(node):
            if name not in reserved:
                continue
            line = getattr(node, "lineno", 1)
            raise ImportContractError(
                f"shadowing the reserved built-in name {name} is not "
                f"supported in {importer_path}:{line}"
            )


def _bound_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return (node.id,)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (node.name,)
    if isinstance(node, ast.arg):
        return (node.arg,)
    if isinstance(node, ast.ExceptHandler) and isinstance(node.name, str):
        return (node.name,)
    if isinstance(node, ast.MatchAs) and node.name is not None:
        return (node.name,)
    if isinstance(node, ast.MatchStar) and node.name is not None:
        return (node.name,)
    if isinstance(node, ast.MatchMapping) and node.rest is not None:
        return (node.rest,)
    return ()


def _reject_shadowed_dynamic_names(
    tree: ast.AST,
    aliases: DynamicImportAliases,
    importer_path: Path,
) -> None:
    protected = aliases.importlib_modules
    for node in ast.walk(tree):
        imported_names: set[str] = set()
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.partition(".")[0]
                if alias.name == "importlib" and alias.asname is None:
                    continue
                imported_names.add(bound)
        elif isinstance(node, ast.ImportFrom):
            if protected and any(alias.name == "*" for alias in node.names):
                raise ImportContractError(
                    f"wildcard import can shadow the dynamic import name importlib "
                    f"in {importer_path}:{node.lineno}"
                )
            imported_names.update(alias.asname or alias.name for alias in node.names)
        for name in sorted(imported_names & protected):
            raise ImportContractError(
                f"shadowing the dynamic import name {name} is not supported in "
                f"{importer_path}:{node.lineno}"
            )

        for name in _bound_names(node):
            if name not in protected:
                continue
            line = getattr(node, "lineno", 1)
            raise ImportContractError(
                f"shadowing the dynamic import name {name} is not supported in "
                f"{importer_path}:{line}"
            )


def _dynamic_import_aliases(
    tree: ast.AST,
    *,
    importer_path: Path,
) -> DynamicImportAliases:
    _reject_shadowed_builtins(tree, importer_path)
    _reject_nested_import_machinery(tree, importer_path)
    importlib_modules: set[str] = set()
    imported_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.asname or alias.name.partition(".")[0])
                if _is_machinery_module(alias.name):
                    if alias.name == "importlib" and alias.asname is None:
                        importlib_modules.add("importlib")
                        continue
                    raise ImportContractError(
                        "only a direct top-level import importlib is supported in "
                        f"{importer_path}:{node.lineno}"
                    )
                bound = alias.asname or alias.name.partition(".")[0]
                source_name = alias.name.rpartition(".")[2]
                if (
                    bound in MACHINERY_MODULE_ROOTS
                    or source_name in MACHINERY_IMPORT_NAMES
                ):
                    raise ImportContractError(
                        "re-exported dynamic import machinery is not supported in "
                        f"{importer_path}:{node.lineno}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and _is_machinery_module(node.module):
                raise ImportContractError(
                    "only a direct top-level import importlib is supported in "
                    f"{importer_path}:{node.lineno}"
                )
            if (
                node.level == 0
                and not (
                    node.module == ROOT_PACKAGE
                    or (node.module or "").startswith(f"{ROOT_PACKAGE}.")
                )
                and any(alias.name == "*" for alias in node.names)
            ):
                raise ImportContractError(
                    f"external wildcard import is not supported in "
                    f"{importer_path}:{node.lineno}"
                )
            if any(
                alias.name in MACHINERY_IMPORT_NAMES
                or alias.asname in MACHINERY_MODULE_ROOTS
                for alias in node.names
            ):
                raise ImportContractError(
                    "re-exported dynamic import machinery is not supported in "
                    f"{importer_path}:{node.lineno}"
                )

    aliases = DynamicImportAliases(
        importlib_modules=frozenset(importlib_modules),
        imported_modules=frozenset(imported_modules),
    )
    _reject_shadowed_dynamic_names(tree, aliases, importer_path)
    _validate_dynamic_loader_uses(tree, aliases, importer_path)
    return aliases


def _dynamic_module_kind(
    node: ast.expr,
    aliases: DynamicImportAliases,
) -> str | None:
    """Resolve a supported name that directly yields import machinery."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id in aliases.importlib_modules:
            return "importlib"
    return None


def _dynamic_loader_kind(
    node: ast.expr,
    aliases: DynamicImportAliases,
) -> str | None:
    if (
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == "__import__"
    ):
        return "builtin"
    if (
        isinstance(node, ast.Attribute)
        and _dynamic_module_kind(node.value, aliases) == "importlib"
        and node.attr == "import_module"
    ):
        return "importlib"
    return None


def _is_literal_machinery_module_name(value: object) -> bool:
    return isinstance(value, str) and (
        value == "__builtins__" or _is_machinery_module(value)
    )


def _is_imported_module_expression(
    node: ast.expr,
    aliases: DynamicImportAliases,
) -> bool:
    if (
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in aliases.imported_modules
    ):
        return True
    if isinstance(node, ast.Attribute):
        return _is_imported_module_expression(node.value, aliases)
    return (
        isinstance(node, ast.Call)
        and _dynamic_loader_kind(node.func, aliases) is not None
    )


def _is_imported_module_namespace(
    node: ast.expr,
    aliases: DynamicImportAliases,
) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__dict__"
        and _is_imported_module_expression(node.value, aliases)
    )


def _reject_alternate_loader_expressions(
    tree: ast.AST,
    aliases: DynamicImportAliases,
    importer_path: Path,
) -> None:
    machinery_names = {"__builtins__", *MACHINERY_MODULE_ROOTS}
    loader_names = {"__import__", "import_module"}
    loader_attributes = {"__import__"}
    for node in ast.walk(tree):
        unsupported = (
            (isinstance(node, ast.Name) and node.id == "__builtins__")
            or (
                isinstance(node, ast.Attribute)
                and node.attr in (machinery_names | loader_attributes)
            )
            or (
                isinstance(node, ast.Attribute)
                and node.attr == "import_module"
                and _is_imported_module_expression(node.value, aliases)
                and _dynamic_loader_kind(node, aliases) is None
            )
            or (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and (
                    _is_literal_machinery_module_name(node.slice.value)
                    or (
                        node.slice.value in loader_names
                        and _is_imported_module_namespace(node.value, aliases)
                    )
                )
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and (
                    _is_literal_machinery_module_name(node.args[1].value)
                    or (
                        node.args[1].value in loader_names
                        and _is_imported_module_expression(node.args[0], aliases)
                    )
                )
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and bool(node.args)
                and isinstance(node.args[0], ast.Constant)
                and (
                    _is_literal_machinery_module_name(node.args[0].value)
                    or (
                        node.args[0].value in loader_names
                        and _is_imported_module_namespace(node.func.value, aliases)
                    )
                )
            )
        )
        if unsupported:
            raise ImportContractError(
                f"unsupported dynamic import machinery expression in "
                f"{importer_path}:{node.lineno}"
            )


def _validate_dynamic_loader_uses(
    tree: ast.AST,
    aliases: DynamicImportAliases,
    importer_path: Path,
) -> None:
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr):
            continue

        module_kind = _dynamic_module_kind(node, aliases)
        if module_kind is not None:
            parent = parents.get(node)
            direct_attribute = (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and _dynamic_loader_kind(parent, aliases) == "importlib"
            )
            if not direct_attribute:
                raise ImportContractError(
                    f"dynamic import module {module_kind} must be used directly in "
                    f"{importer_path}:{node.lineno}"
                )

        loader_kind = _dynamic_loader_kind(node, aliases)
        if loader_kind is None:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            continue
        raise ImportContractError(
            f"dynamic import loader must be called directly in "
            f"{importer_path}:{node.lineno}"
        )


def _call_argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next(
        (item.value for item in node.keywords if item.arg == keyword),
        None,
    )


def _reject_call_expansions(
    node: ast.Call,
    *,
    importer_path: Path,
) -> None:
    if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
        keyword.arg is None for keyword in node.keywords
    ):
        raise ImportContractError(
            f"dynamic import arguments must not use starred expansion in "
            f"{importer_path}:{node.lineno}"
        )


def _literal_package_from_globals(
    node: ast.expr | None,
    *,
    importer_path: Path,
    line: int,
) -> str:
    message = (
        "relative dynamic import globals must be a literal string-keyed "
        f"mapping with a string __package__ in {importer_path}:{line}"
    )
    if not isinstance(node, ast.Dict) or any(
        not isinstance(key, ast.Constant) or not isinstance(key.value, str)
        for key in node.keys
    ):
        raise ImportContractError(message)

    package_node: ast.expr | None = None
    for key, value in zip(node.keys, node.values):
        if key.value == "__package__":
            package_node = value
    if not isinstance(package_node, ast.Constant) or not isinstance(
        package_node.value, str
    ):
        raise ImportContractError(message)
    return package_node.value


def _literal_fromlist(
    node: ast.expr | None,
    *,
    importer_path: Path,
    line: int,
) -> tuple[str, ...]:
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return ()
    if not isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        raise ImportContractError(
            f"dynamic import fromlist must be a literal string collection in "
            f"{importer_path}:{line}"
        )

    items: list[str] = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            raise ImportContractError(
                f"dynamic import fromlist must be a literal string collection in "
                f"{importer_path}:{line}"
            )
        if item.value == "*":
            raise ImportContractError(
                f"wildcard dynamic import fromlist is not supported in "
                f"{importer_path}:{line}"
            )
        items.append(item.value)
    return tuple(items)


def _validate_dynamic_target(
    target: str,
    *,
    importer_path: Path,
    line: int,
) -> None:
    root = target.partition(".")[0]
    if root in MACHINERY_MODULE_ROOTS:
        raise ImportContractError(
            f"dynamic import of machinery module {target} is not supported in "
            f"{importer_path}:{line}"
        )


def _literal_dynamic_import(
    node: ast.Call,
    *,
    aliases: DynamicImportAliases,
    importer_path: Path,
) -> set[str] | None:
    """Resolve recognized dynamic imports and reject targets hidden from the graph."""
    loader_kind = _dynamic_loader_kind(node.func, aliases)
    if loader_kind is None:
        return None
    _reject_call_expansions(node, importer_path=importer_path)

    name_node = _call_argument(node, 0, "name")
    if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
        raise ImportContractError(
            f"dynamic import target must be a string literal in "
            f"{importer_path}:{node.lineno}"
        )
    name = name_node.value
    if loader_kind == "builtin":
        level_node = _call_argument(node, 4, "level")
        level = 0
        if level_node is not None:
            if not isinstance(level_node, ast.Constant) or not isinstance(
                level_node.value, int
            ):
                raise ImportContractError(
                    f"dynamic import level must be an integer literal in "
                    f"{importer_path}:{node.lineno}"
                )
            level = level_node.value
            if level < 0:
                raise ImportContractError(
                    f"dynamic import level must be non-negative in "
                    f"{importer_path}:{node.lineno}"
                )
        if level:
            package = _literal_package_from_globals(
                _call_argument(node, 1, "globals"),
                importer_path=importer_path,
                line=node.lineno,
            )
            relative_name = "." * level + name
            try:
                target = importlib.util.resolve_name(relative_name, package)
            except ImportError as exc:
                raise ImportContractError(
                    f"cannot resolve dynamic import in "
                    f"{importer_path}:{node.lineno}: {relative_name}"
                ) from exc
        elif name.startswith("."):
            raise ImportContractError(
                f"relative __import__ target requires a positive literal level in "
                f"{importer_path}:{node.lineno}"
            )
        else:
            target = name

        _validate_dynamic_target(
            target,
            importer_path=importer_path,
            line=node.lineno,
        )

        targets = {target}
        for item in _literal_fromlist(
            _call_argument(node, 3, "fromlist"),
            importer_path=importer_path,
            line=node.lineno,
        ):
            targets.add(f"{target}.{item}" if target else item)
        return targets

    if not name.startswith("."):
        target = name
    else:
        package_node = _call_argument(node, 1, "package")
        if not isinstance(package_node, ast.Constant) or not isinstance(
            package_node.value, str
        ):
            raise ImportContractError(
                f"relative dynamic import package must be a string literal in "
                f"{importer_path}:{node.lineno}"
            )
        try:
            target = importlib.util.resolve_name(name, package_node.value)
        except ImportError as exc:
            raise ImportContractError(
                f"cannot resolve dynamic import in {importer_path}:{node.lineno}: "
                f"{name} from {package_node.value}"
            ) from exc

    _validate_dynamic_target(
        target,
        importer_path=importer_path,
        line=node.lineno,
    )
    return {target}


def _import_targets(
    node: ast.AST,
    *,
    importer: str,
    importer_path: Path,
    known_modules: set[str],
    dynamic_aliases: DynamicImportAliases,
) -> set[str]:
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == ROOT_PACKAGE or alias.name.startswith(f"{ROOT_PACKAGE}."):
                targets.update(_known_prefixes(alias.name, known_modules))
                targets.add(alias.name)
    elif isinstance(node, ast.ImportFrom):
        base = _resolve_from_base(
            node,
            importer=importer,
            importer_path=importer_path,
        )
        if base == ROOT_PACKAGE or base.startswith(f"{ROOT_PACKAGE}."):
            targets.update(_known_prefixes(base, known_modules))
            targets.add(base)
            for alias in node.names:
                if alias.name == "*":
                    raise ImportContractError(
                        f"internal wildcard import is not supported in "
                        f"{importer_path}:{node.lineno}"
                    )
                candidate = f"{base}.{alias.name}"
                if candidate in known_modules:
                    targets.update(_known_prefixes(candidate, known_modules))
    elif isinstance(node, ast.Call):
        dynamic_targets = _literal_dynamic_import(
            node,
            aliases=dynamic_aliases,
            importer_path=importer_path,
        )
        for target in dynamic_targets or ():
            if target == ROOT_PACKAGE or target.startswith(f"{ROOT_PACKAGE}."):
                targets.update(_known_prefixes(target, known_modules))
                targets.add(target)
    return targets


def build_import_graph(package_root: Path) -> dict[str, tuple[ImportEdge, ...]]:
    """Build a deterministic internal import graph without importing the package."""
    modules = _inventory_modules(package_root)
    known_modules = set(modules)
    graph: dict[str, tuple[ImportEdge, ...]] = {}

    for importer, path in sorted(modules.items()):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise ImportContractError(
                f"cannot parse {path}:{exc.lineno}: {exc.msg}"
            ) from exc

        dynamic_aliases = _dynamic_import_aliases(tree, importer_path=path)

        edges: set[ImportEdge] = set()
        for parent in sorted(_known_prefixes(importer, known_modules) - {importer}):
            edges.add(
                ImportEdge(
                    importer=importer,
                    imported=parent,
                    path=path,
                    line=1,
                    statement="implicit package initialization",
                )
            )
        for node in ast.walk(tree):
            targets = _import_targets(
                node,
                importer=importer,
                importer_path=path,
                known_modules=known_modules,
                dynamic_aliases=dynamic_aliases,
            )
            if not targets:
                continue
            statement = ast.get_source_segment(source, node) or ast.unparse(node)
            statement = " ".join(statement.split())
            for target in targets:
                if target == importer:
                    continue
                edges.add(
                    ImportEdge(
                        importer=importer,
                        imported=target,
                        path=path,
                        line=node.lineno,
                        statement=statement,
                    )
                )
        _reject_alternate_loader_expressions(tree, dynamic_aliases, path)
        graph[importer] = tuple(
            sorted(edges, key=lambda edge: (edge.imported, edge.line, edge.statement))
        )
    return graph


def _area(module: str) -> str | None:
    if module == COMPOSITION_ROOT or module.startswith(f"{COMPOSITION_ROOT}."):
        return "main"
    for area in _LAYER_BY_AREA:
        package = f"{ROOT_PACKAGE}.{area}"
        if module == package or module.startswith(f"{package}."):
            return area
    return None


def _is_forbidden(source_area: str, target_area: str) -> bool:
    if target_area == "main":
        return True
    if source_area == target_area:
        return False
    return _LAYER_BY_AREA[target_area] <= _LAYER_BY_AREA[source_area]


def _path_to(module: str, parents: dict[str, ImportEdge]) -> list[ImportEdge]:
    path: list[ImportEdge] = []
    while module in parents:
        edge = parents[module]
        path.append(edge)
        module = edge.importer
    path.reverse()
    return path


def find_violations(
    graph: dict[str, tuple[ImportEdge, ...]],
) -> list[Violation]:
    """Return boundary crossings reachable from each contracted source area."""
    violations: dict[
        tuple[str, Path, int, str, str],
        Violation,
    ] = {}

    for source_area in _LAYER_BY_AREA:
        starts = sorted(module for module in graph if _area(module) == source_area)
        queue = deque(starts)
        visited = set(starts)
        parents: dict[str, ImportEdge] = {}

        while queue:
            importer = queue.popleft()
            for edge in graph.get(importer, ()):
                target_area = _area(edge.imported)
                if target_area is not None and _is_forbidden(source_area, target_area):
                    key = (
                        source_area,
                        edge.path,
                        edge.line,
                        edge.statement,
                        target_area,
                    )
                    candidate = Violation(
                        source_area=source_area,
                        target_area=target_area,
                        edges=tuple((*_path_to(importer, parents), edge)),
                    )
                    existing = violations.get(key)
                    if existing is None or (
                        len(candidate.edges),
                        -len(candidate.edges[-1].imported),
                    ) < (
                        len(existing.edges),
                        -len(existing.edges[-1].imported),
                    ):
                        violations[key] = candidate
                    continue

                if edge.imported in graph and edge.imported not in visited:
                    visited.add(edge.imported)
                    parents[edge.imported] = edge
                    queue.append(edge.imported)

    return sorted(
        violations.values(),
        key=lambda violation: (
            str(violation.edges[-1].path),
            violation.edges[-1].line,
            violation.source_area,
            violation.target_area,
            violation.edges[-1].statement,
        ),
    )


def _display_path(path: Path, package_root: Path) -> str:
    display_root = (
        package_root.parent.parent
        if package_root.parent.name == "src"
        else package_root.parent
    )
    try:
        return path.relative_to(display_root).as_posix()
    except ValueError:
        return str(path)


def _format_violation(violation: Violation, package_root: Path) -> str:
    edge = violation.edges[-1]
    chain = [violation.edges[0].importer]
    chain.extend(path_edge.imported for path_edge in violation.edges)
    return (
        f"{_display_path(edge.path, package_root)}:{edge.line}: "
        f"{violation.source_area} cannot depend on {violation.target_area}\n"
        f"  import chain: {' -> '.join(chain)}\n"
        f"  statement: {edge.statement}"
    )


def check_import_contracts(package_root: Path = PACKAGE_ROOT) -> list[Violation]:
    """Analyze ``package_root`` and return every forbidden dependency path."""
    return find_violations(build_import_graph(package_root.absolute()))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check Athena's core, feature-module, and web import contracts"
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
        graph = build_import_graph(package_root)
        violations = find_violations(graph)
    except (ImportContractError, OSError, UnicodeError) as exc:
        print(f"Athena import contracts failed: {exc}", file=sys.stderr)
        return 1

    if violations:
        count = len(violations)
        print(
            f"Athena import contracts failed: {count} forbidden dependency "
            f"{'path' if count == 1 else 'paths'}",
            file=sys.stderr,
        )
        for violation in violations:
            print(
                _format_violation(violation, package_root),
                file=sys.stderr,
            )
        return 1

    print(
        "Athena import contracts passed: "
        f"{len(graph)} modules, no forbidden dependencies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
