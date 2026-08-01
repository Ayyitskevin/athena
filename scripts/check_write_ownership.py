"""Enforce one-command-owns-each-write at the transport boundary.

The doctrine (AGENTS.md's cardinal rule, docs/COMMAND_MIGRATION.md): transports
— ``web/*``, ``mcp/*``, and ``*_api.py`` — parse transport input and translate
results. They never execute INSERT/UPDATE/DELETE themselves, and they never
call a data module's mutating helper directly. A write lives behind a command
module (the ``*_commands.py`` naming convention IS the self-declaration) or
behind one of the designated data-module writers that COMMAND_MIGRATION.md
documents: the personal-state category, the login/session flow, the budget and
approval owners, and the delivery-outcome recorders.

This check exists because H-0.5/H-1.2/H-1.3 were all instances of a transport
reaching around the command layer — a rule that was written down and followed
by hand until it wasn't. Like the import contract, it fails the build instead.

The check is AST-only (standard library, never imports Athena). It builds a
registry of every function whose body carries a write-SQL literal, propagates
write-ness across the package's static call graph, then flags any transport
that (a) contains a write-SQL literal or (b) calls a writer outside the
command-module convention and the explicit allowlist below. Allowlist entries
that no longer match a live call site fail as stale, so the list can only
shrink with the debt it records. Arbitrary computed dispatch (getattr chains,
string-built calls) is out of scope, as it is for the import checker — which
already forbids the internal wildcard imports that would defeat name
resolution here.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import re
import sys
from typing import Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"
ROOT_PACKAGE = "athena"

# A write statement, anchored at the start of a SQL string. The codebase writes
# SQL in one uppercase shape, and docstrings are excluded before this runs, so
# English prose ("Delete an automation rule.") never reaches the pattern.
_WRITE_SQL_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*)*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO"
    r"|DELETE\s+FROM|UPDATE\s+(?:OR\s+\w+\s+)?[\w.]+(?:\s+SET\b|\s*$))"
)

# The explicit exceptions, each one a (transport, writer module, function)
# triple with the reason the doctrine permits it. "*" as the transport means
# any transport may call it — used only where COMMAND_MIGRATION.md names both
# adapters as the intended callers. A transport-scoped entry that stops
# matching a live call fails as stale: the allowlist records debt, and debt
# that quietly disappears should be celebrated by deleting the entry, not by
# drifting out of sync with it.
ALLOWED_TRANSPORT_WRITES: dict[tuple[str, str, str], str] = {
    # --- Personal state (COMMAND_MIGRATION.md, "Personal state"): owner-scoped
    # single-writer modules; REST and browser adapters both call them and
    # translate the result. No audit events by design.
    ("*", "athena.aegis.saved_filters", "create_filter"): "personal state",
    ("*", "athena.aegis.saved_filters", "update_filter"): "personal state",
    ("*", "athena.aegis.saved_filters", "delete_filter"): "personal state",
    ("*", "athena.core.notifications", "mark_read"): "personal state",
    ("*", "athena.core.notifications", "mark_all_read"): "personal state",
    ("*", "athena.core.notifications", "watch"): "personal state",
    ("*", "athena.core.notifications", "unwatch"): "personal state",
    # A page draft is one author's unsaved work in progress: owner-scoped, never
    # audited, never content. Migration 0071 carries the full boundary.
    ("*", "athena.mentor.page_drafts", "save_draft"): "personal state",
    ("*", "athena.mentor.page_drafts", "discard_draft"): "personal state",
    # --- The login/session flow (COMMAND_MIGRATION.md, "Users and agents" /
    # "OIDC identities"): credential operational state and provider exchange
    # are documented as bounded flows outside the command layer. Scoped to the
    # one transport that performs them.
    ("athena.web.auth", "athena.core.sessions", "create_session"): (
        "session mint on login; login flow documented outside the command layer"
    ),
    ("athena.web.auth", "athena.core.sessions", "destroy_session"): (
        "session revocation on logout/login; same documented flow"
    ),
    ("athena.web.auth", "athena.core.oidc", "start_login"): (
        "OIDC login state; a provider-exchange flow, not a durable domain write"
    ),
    ("athena.web.auth", "athena.core.oidc", "take_login_state"): (
        "OIDC login state; same provider-exchange flow"
    ),
    ("athena.web.auth", "athena.core.oidc_flow", "provision_or_link"): (
        "first-login SSO provisioning; documented OIDC service flow"
    ),
    ("athena.web.auth", "athena.core.users", "verify_credentials"): (
        "transparent password rehash on login; the documented bounded exception"
    ),
    ("athena.web.auth", "athena.core.security_events", "record_failure"): (
        "best-effort login-failure audit; the refusal must go out regardless"
    ),
    # --- The auth dependency itself records boundary refusals (scope_denied,
    # revoked_token_used, ...) as part of authenticating, on every transport.
    ("*", "athena.core.identity", "current_actor"): (
        "refusal audit inside the authentication dependency"
    ),
    ("*", "athena.core.identity", "optional_actor"): (
        "refusal audit inside the authentication dependency"
    ),
    # --- Documented command owners that predate the *_commands naming
    # convention (COMMAND_MIGRATION.md: each "owns its write plus its audit
    # event"). Renaming the modules is the follow-up, not widening this list.
    ("*", "athena.core.budgets", "set_budget"): (
        "documented budget command owner without the _commands suffix"
    ),
    ("*", "athena.core.budgets", "clear_budget"): (
        "documented budget command owner without the _commands suffix"
    ),
    ("*", "athena.core.approvals", "set_policy"): (
        "documented approvals command owner without the _commands suffix"
    ),
    ("*", "athena.core.approvals", "clear_policy"): (
        "documented approvals command owner without the _commands suffix"
    ),
    ("*", "athena.core.approvals", "decide"): (
        "documented approvals command owner without the _commands suffix"
    ),
    ("*", "athena.mentor.run_learnings", "record_learning"): (
        "documented learning-promotion command owner without the _commands suffix"
    ),
    # --- Inbound event-source recording (COMMAND_MIGRATION.md, "Event
    # sources"): the forge delivery path is deliberately not a command; it writes
    # *imported* history owned by the delivery subsystem. Dispatch callback writes
    # have no exception here: icarus_commands.apply_callback owns them.
    ("athena.aegis.forge_api", "athena.aegis.forge", "land_delivery"): (
        "the HMAC-verified delivery path writes imported history by design"
    ),
}


class WriteOwnershipError(RuntimeError):
    """The package could not be analyzed safely."""


@dataclass(frozen=True)
class Violation:
    """One transport-side write that bypasses the command layer."""

    path: Path
    line: int
    transport: str
    kind: str
    detail: str


def _module_name(package_root: Path, path: Path) -> str:
    relative = path.relative_to(package_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((ROOT_PACKAGE, *parts))


def _inventory_modules(package_root: Path) -> dict[str, Path]:
    if package_root.name != ROOT_PACKAGE or not package_root.is_dir():
        raise WriteOwnershipError(
            f"package root must be an existing directory named {ROOT_PACKAGE!r}: "
            f"{package_root}"
        )
    if package_root.is_symlink():
        raise WriteOwnershipError(
            f"symlinked package root is not supported: {package_root}"
        )
    modules: dict[str, Path] = {}
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        modules[_module_name(package_root, path)] = path
    return modules


def _is_transport(module: str) -> bool:
    """Transports are the HTTP/MCP adapters the doctrine binds: the web layer,
    the MCP server/client, and every REST surface (``*_api.py``)."""
    base = module.rpartition(".")[2]
    area = module.partition(".")[2].partition(".")[0]
    return area in {"web", "mcp"} or base.endswith("_api")


def _is_command_module(module: str) -> bool:
    """The self-declaration: a module named ``*_commands`` is a designated
    writer, so calling it from a transport is the doctrine working, not a
    violation."""
    return module.rpartition(".")[2].endswith("_commands")


def _non_docstring_constants(tree: ast.AST) -> Iterable[ast.Constant]:
    """Every string constant except docstrings — prose starting "Delete ..."
    is documentation, not SQL."""
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        expr = parents.get(node)
        owner = parents.get(expr) if expr is not None else None
        if (
            isinstance(expr, ast.Expr)
            and isinstance(
                owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and owner.body
            and owner.body[0] is expr
        ):
            continue
        yield node


def _contains_write_sql(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        _WRITE_SQL_RE.search(constant.value)
        for constant in _non_docstring_constants(node)
    )


@dataclass(frozen=True)
class _ImportTable:
    """Bound name -> (absolute module, imported attribute or None)."""

    names: dict[str, tuple[str, str | None]]


def _import_table(
    tree: ast.AST,
    *,
    module: str,
    path: Path,
) -> _ImportTable:
    names: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.partition(".")[0]] = (
                    alias.name,
                    None,
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = (
                    module if path.name == "__init__.py" else module.rpartition(".")[0]
                )
                relative = "." * node.level + (node.module or "")
                try:
                    base = importlib.util.resolve_name(relative, package)
                except ImportError as exc:
                    raise WriteOwnershipError(
                        f"cannot resolve relative import in {path}:{node.lineno}: "
                        f"{relative}"
                    ) from exc
            else:
                base = node.module or ""
            for alias in node.names:
                names[alias.asname or alias.name] = (base, alias.name)
    return _ImportTable(names=names)


def _call_candidates(
    node: ast.Call,
    imports: _ImportTable,
    *,
    module: str,
) -> set[tuple[str, str]]:
    """Resolve a call to (module, function) candidates, static names only.

    A bare name resolves through the import table or to the same module; an
    attribute chain resolves through the module it was imported from. Anything
    more computed is out of scope by design (see the module docstring)."""
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in imports.names:
            base, attribute = imports.names[func.id]
            if base and attribute:
                return {(base, attribute)}
            return set()
        return {(module, func.id)}
    if isinstance(func, ast.Attribute):
        names = [func.attr]
        value = func.value
        while isinstance(value, ast.Attribute):
            names.append(value.attr)
            value = value.value
        if not isinstance(value, ast.Name) or value.id not in imports.names:
            return set()
        base, attribute = imports.names[value.id]
        names.append(value.id)
        names.reverse()
        rest = names[1:] if attribute is None else [attribute, *names[1:]]
        if not base or not rest:
            return set()
        candidates = {(base, rest[-1])}
        if len(rest) > 1:
            candidates.add((".".join((base, *rest[:-1])), rest[-1]))
        return candidates
    return set()


def build_writer_registry(
    modules: dict[str, Path],
    trees: dict[str, ast.AST],
) -> dict[tuple[str, str], tuple[Path, int]]:
    """Every (module, function) that can reach a write statement, directly or
    through the static call graph. Transports are excluded from propagation:
    every call inside a transport is checked directly, so transport-local
    helpers add nothing but false hops."""
    registry: dict[tuple[str, str], tuple[Path, int]] = {}
    for module, tree in sorted(trees.items()):
        if _is_transport(module):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                _contains_write_sql(node)
            ):
                registry[(module, node.name)] = (modules[module], node.lineno)

    changed = True
    while changed:
        changed = False
        for module, tree in sorted(trees.items()):
            if _is_transport(module):
                continue
            imports = _import_table(tree, module=module, path=modules[module])
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                key = (module, node.name)
                if key in registry:
                    continue
                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    if any(
                        candidate in registry
                        for candidate in _call_candidates(child, imports, module=module)
                    ):
                        registry[key] = (modules[module], node.lineno)
                        changed = True
                        break
    return registry


def find_violations(
    modules: dict[str, Path],
    trees: dict[str, ast.AST],
) -> list[Violation]:
    writers = build_writer_registry(modules, trees)
    exercised: set[tuple[str, str, str]] = set()
    violations: list[Violation] = []

    for module in sorted(modules):
        if not _is_transport(module):
            continue
        path = modules[module]
        tree = trees[module]
        for constant in _non_docstring_constants(tree):
            if _WRITE_SQL_RE.search(constant.value):
                violations.append(
                    Violation(
                        path=path,
                        line=constant.lineno,
                        transport=module,
                        kind="direct-write-sql",
                        detail=" ".join(constant.value.split())[:80],
                    )
                )
        imports = _import_table(tree, module=module, path=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for candidate in _call_candidates(node, imports, module=module):
                    if candidate not in writers:
                        continue
                    writer_module, function = candidate
                    if _is_command_module(writer_module):
                        continue
                    allowance = next(
                        (
                            transport
                            for transport, allowed_module, allowed_function in (
                                ALLOWED_TRANSPORT_WRITES
                            )
                            if allowed_module == writer_module
                            and allowed_function == function
                            and (transport == "*" or transport == module)
                        ),
                        None,
                    )
                    if allowance is not None:
                        exercised.add((allowance, writer_module, function))
                        continue
                    violations.append(
                        Violation(
                            path=path,
                            line=node.lineno,
                            transport=module,
                            kind="data-module-write",
                            detail=f"calls {writer_module}.{function}",
                        )
                    )

    for transport, writer_module, function in sorted(ALLOWED_TRANSPORT_WRITES):
        if writer_module not in modules:
            # A smaller tree (a test fixture) simply has nothing to exempt.
            continue
        entry = (transport, writer_module, function)
        if entry not in exercised:
            violations.append(
                Violation(
                    path=modules[writer_module],
                    line=1,
                    transport=transport,
                    kind="stale-allowlist-entry",
                    detail=(
                        f"{writer_module}.{function} is allowlisted for "
                        f"{transport} but no transport calls it — delete the entry"
                    ),
                )
            )
    return sorted(
        violations,
        key=lambda violation: (str(violation.path), violation.line, violation.detail),
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


def check_write_ownership(package_root: Path = PACKAGE_ROOT) -> list[Violation]:
    """Analyze ``package_root`` and return every transport-side write."""
    modules = _inventory_modules(package_root.absolute())
    trees: dict[str, ast.AST] = {}
    for module, path in sorted(modules.items()):
        try:
            trees[module] = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise WriteOwnershipError(
                f"cannot parse {path}:{exc.lineno}: {exc.msg}"
            ) from exc
    return find_violations(modules, trees)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check that transports never write except through commands"
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
        violations = check_write_ownership(package_root)
    except (WriteOwnershipError, OSError, UnicodeError) as exc:
        print(f"Athena write ownership failed: {exc}", file=sys.stderr)
        return 1

    if violations:
        count = len(violations)
        print(
            f"Athena write ownership failed: {count} transport-side "
            f"{'write' if count == 1 else 'writes'}",
            file=sys.stderr,
        )
        for violation in violations:
            print(
                f"{_display_path(violation.path, package_root)}:{violation.line}: "
                f"{violation.kind}: {violation.transport}: {violation.detail}",
                file=sys.stderr,
            )
        return 1

    print(
        "Athena write ownership passed: transports write only through "
        "commands and designated writers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
