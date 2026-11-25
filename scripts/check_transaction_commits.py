"""Fail on a commit that ``db.transaction`` does not own.

``db.transaction`` owns the final commit or rollback of the work inside it
(B5, docs/plans/AGENTIC_USE_SPEC_ATHENA.md). Anything that commits underneath
it splits one atomic write in two: the part before the commit lands even when
the rest of the block then fails. Review ARCH-1 was exactly that, a lifecycle
event recorded through a helper whose ``commit`` parameter defaulted to true.

And every write either takes a ``commit`` parameter, committing only when it
is true, or goes through ``db.transaction`` (G2,
docs/plans/ATHENA_IN_USE_SPEC.md). A writer that commits whatever it is asked
cannot be composed into a command: called inside another's transaction it
commits that transaction's first half. A transaction opened by hand (``BEGIN
IMMEDIATE`` ... ``commit()``) is the same writer with the same defect, and it
also refuses to run inside a caller's transaction at all.

There are three kinds of finding.

A. A commit inside a block. A statement lexically inside the body of a
   ``with`` whose item is a call to ``athena.core.db.transaction`` (nested
   ``def`` and ``lambda`` bodies excluded, since they do not run there) fails
   when it is:

   1. a commit point: a zero-argument ``<anything>.commit()`` call, an
      ``<anything>.executescript(...)`` call (in Python 3.12's default legacy
      transaction mode it commits a pending transaction first), or an
      ``<anything>.execute(...)`` whose first argument is the string literal
      ``COMMIT`` or ``END`` (any case, optional ``TRANSACTION``); or
   2. a call that resolves to an Athena function that commits at that call
      (below).

B. A commit of its own. Outside ``core/db.py``, a commit point (as in A1) in
   any function, method, lambda, class body or module body fails unless the
   function's own ``commit`` parameter governs it (as defined below). A
   module-level function's commit point inside its own block is reported once,
   as A. A *call* that passes ``commit=True`` (or leaves a true default) to a
   writer is not a B finding: the writer takes ``commit`` and the caller chose;
   inside a block that call is an A finding.

C. A transaction opened or ended by hand. Outside ``core/db.py``, an
   ``<anything>.execute(...)`` or ``.executescript(...)`` whose first argument
   is a string literal (or an f-string whose literal start) beginning with
   ``BEGIN``, ``SAVEPOINT``, ``RELEASE``, ``ROLLBACK``, ``COMMIT`` or ``END``
   fails, anywhere in the file. ``db.transaction`` is the one place that
   writes those statements.

One call is reported once: A first, then C, then B.

The allow-list. ``ALLOWED_OWN_TRANSACTIONS`` names the functions that must
commit or manage a transaction on their own, each with the reason. It
suppresses that function's B and C findings, never an A finding (a call to it
inside a block still fails). An entry whose function no longer exists, or no
longer has a B or C finding to excuse, fails as stale, so the list can only
shrink.

How callees resolve. Only module-level functions under ``src/athena`` are
known. A call resolves by static name, the way ``check_write_ownership.py``
resolves one: a bare name through the module's imports or to the same module,
an attribute chain through the module it was imported from. Methods, calls
through ``self``/objects, callbacks, functions passed as values and
``getattr`` do not resolve and are not findings.

When a resolved callee commits. Every module-level function gets a summary,
computed to a fixed point over the whole static call graph (so the depth is
unbounded along resolvable calls, not one level of helpers):

* A function that takes ``commit`` commits at a call unless the call passes
  the literal ``False`` (by keyword or position). ``commit=True``, any other
  expression, an omitted argument whose default is not the literal ``False``,
  and ``*args``/``**kwargs`` that do not name it all count as committing.
  ``commit=commit`` inside a transaction block counts as committing, because
  the caller can pass true. Whether the callee honours a false argument is
  the next point.
* A function commits *unconditionally* (whatever its ``commit`` argument) when,
  outside its own ``db.transaction`` blocks, it reaches a commit point (as in
  1) that its ``commit`` parameter does not govern, or calls a function that
  commits at that call when the caller's own ``commit`` is false. A commit
  point (or call) is governed by the parameter when it runs only if
  ``commit`` is true, as far as the checker reads ``if`` statements: it is in
  the body of an ``if commit`` (or ``if commit and ...``), in the ``else`` of
  an ``if not commit`` (or ``if not commit or ...``), or after such an ``if
  not commit`` whose body ends in ``return`` or ``raise`` in an enclosing
  block. A test of any other shape governs nothing, nor does a function with
  no ``commit`` parameter.
* ``db.transaction`` itself is excluded: its commit is the one that owns the
  block, and a nested use is a savepoint.

Calls inside a function's own transaction block are findings of that function,
not part of its summary, so one bug is reported once, where it is.

Out of reach. The check does not compare connections: a committing call on a
second connection inside the block is reported even though it commits a
different transaction. It does not see ``with conn:`` (a transaction and a
commit), ``rollback()`` (which also ends a block's transaction; writers still
call it in their ``if commit`` error paths), SQL computed other than as a
literal or an f-string's literal start, or dynamic dispatch. The runtime guard
in ``db.transaction`` covers what reaches it at run time; this pass covers what
the tests do not execute.

Standard library only; it never imports Athena. Usage:

    python scripts/check_transaction_commits.py [--package-root src/athena]
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import re
import sys
from typing import Iterable, Iterator, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"
ROOT_PACKAGE = "athena"
TRANSACTION = ("athena.core.db", "transaction")
COMMIT_PARAMETER = "commit"

#: The one module that may write transaction statements and commit on its own.
OWNER_MODULE = TRANSACTION[0]

#: Functions that must commit, or manage a transaction, on their own (B and C
#: findings): the qualified name of the function and why. Keep it short: a
#: writer belongs here only when ``db.transaction`` cannot express what it does.
ALLOWED_OWN_TRANSACTIONS: dict[str, str] = {
    "athena.core.activity.record": (
        "the one writer of the hash-chained trail takes SQLite's writer "
        "reservation (BEGIN IMMEDIATE) before it reads the access envelope and "
        "the chain head, and a standalone commit=False call leaves that "
        "transaction open for the command that owns it. db.transaction is a "
        "block and cannot hand an open transaction back to its caller. Its "
        "commit is governed by its commit parameter"
    ),
    "athena.core.portability._read_snapshot": (
        "a read snapshot for export: BEGIN, reads, then ROLLBACK, always. It "
        "writes nothing, and db.transaction would end it with a commit"
    ),
}

_COMMIT_SQL_RE = re.compile(r"^\s*(?:COMMIT|END)(?:\s+TRANSACTION)?\s*;?\s*$", re.I)
_TRANSACTION_SQL_RE = re.compile(
    r"^\s*(?:BEGIN|SAVEPOINT|RELEASE|ROLLBACK|COMMIT|END)\b", re.I
)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# What an argument expression says about the callee's commit parameter.
_FALSE = "false"  # the literal False: the callee is asked not to commit
_PASSED = "passed"  # the caller's own ``commit`` name, passed through
_TRUE = "true"  # True, an omitted non-False default, or anything else


class TransactionCheckError(RuntimeError):
    """The package could not be analyzed safely."""


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    function: str
    detail: str


@dataclass(frozen=True)
class _Signature:
    """Where a function takes ``commit`` and what it defaults to."""

    position: int | None  # Positional index, or None when keyword-only.
    default: str | None  # _FALSE, _TRUE, or None when the argument is required.


@dataclass
class _Function:
    module: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    path: Path
    imports: dict[str, tuple[str, str | None]]
    signature: _Signature | None
    calls: list[tuple[ast.Call, bool, bool]]  # (call, inside a block, governed)
    unconditional: bool = False
    reason: str = ""


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
                names[alias.asname or alias.name.partition(".")[0]] = (
                    alias.name if alias.asname else alias.name.partition(".")[0],
                    None,
                )
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = (
                    module if path.name == "__init__.py" else module.rpartition(".")[0]
                )
                try:
                    base = importlib.util.resolve_name("." * node.level + base, package)
                except ImportError as exc:
                    raise TransactionCheckError(
                        f"cannot resolve relative import in {path}:{node.lineno}"
                    ) from exc
            for alias in node.names:
                names[alias.asname or alias.name] = (base, alias.name)
    return names


def _resolve(
    func: ast.expr, imports: dict[str, tuple[str, str | None]], module: str
) -> set[tuple[str, str]]:
    """(module, function) candidates for a called expression, static names only."""
    if isinstance(func, ast.Name):
        if func.id in imports:
            base, attribute = imports[func.id]
            if attribute is None:
                return set()
            # ``from athena.core import db`` binds a module, not a function; the
            # candidate is harmless because no function is keyed by it.
            return {(base, attribute)}
        return {(module, func.id)}
    if not isinstance(func, ast.Attribute):
        return set()
    chain = [func.attr]
    value = func.value
    while isinstance(value, ast.Attribute):
        chain.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name) or value.id not in imports:
        return set()
    base, attribute = imports[value.id]
    chain.reverse()
    parts = chain if attribute is None else [attribute, *chain]
    # ``import athena.core.db`` binds ``athena``; ``from athena.core import db``
    # binds ``db`` to the module athena.core.db. Both resolve to the last name's
    # module: everything before the function name is a module path.
    return {(".".join((base, *parts[:-1])), parts[-1])}


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> _Signature | None:
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    for index, argument in enumerate(positional):
        if argument.arg == COMMIT_PARAMETER:
            offset = index - (len(positional) - len(arguments.defaults))
            default = arguments.defaults[offset] if offset >= 0 else None
            return _Signature(index, _default_kind(default))
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        if argument.arg == COMMIT_PARAMETER:
            return _Signature(None, _default_kind(default))
    return None


def _default_kind(default: ast.expr | None) -> str | None:
    if default is None:
        return None
    if isinstance(default, ast.Constant) and default.value is False:
        return _FALSE
    return _TRUE


def _argument_kind(call: ast.Call, signature: _Signature) -> str:
    """What this call passes as the callee's ``commit``."""
    value = next((k.value for k in call.keywords if k.arg == COMMIT_PARAMETER), None)
    if value is None and signature.position is not None:
        if any(isinstance(argument, ast.Starred) for argument in call.args):
            return _TRUE  # *args may carry it.
        if signature.position < len(call.args):
            value = call.args[signature.position]
    if value is None:
        if any(keyword.arg is None for keyword in call.keywords):
            return _TRUE  # **kwargs may carry it.
        return signature.default or _TRUE
    if isinstance(value, ast.Constant) and value.value is False:
        return _FALSE
    if isinstance(value, ast.Name) and value.id == COMMIT_PARAMETER:
        return _PASSED
    return _TRUE


def _is_commit_point(call: ast.Call) -> str | None:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "commit" and not call.args and not call.keywords:
        return f"{ast.unparse(func)}()"
    if func.attr == "executescript":
        return f"{ast.unparse(func)}(...) (commits a pending transaction first)"
    if (
        func.attr == "execute"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and _COMMIT_SQL_RE.match(call.args[0].value)
    ):
        return f"{ast.unparse(func)}({call.args[0].value!r})"
    return None


def _transaction_statement(call: ast.Call) -> str | None:
    """``execute("BEGIN ...")`` and its kin: a transaction managed by hand."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr in ("execute", "executescript")
        and call.args
    ):
        return None
    sql = call.args[0]
    if isinstance(sql, ast.JoinedStr):
        # An f-string: only its literal start is read.
        first = sql.values[0] if sql.values else None
        sql = first if isinstance(first, ast.Constant) else sql
    if (
        isinstance(sql, ast.Constant)
        and isinstance(sql.value, str)
        and _TRANSACTION_SQL_RE.match(sql.value)
    ):
        return f"{ast.unparse(func)}({ast.unparse(call.args[0])})"
    return None


def _is_commit_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == COMMIT_PARAMETER


def _is_not_commit(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _is_commit_name(node.operand)
    )


def _true_only_if_committing(test: ast.expr) -> bool:
    """``if commit`` / ``if commit and ...``: the body runs only when true."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_is_commit_name(value) for value in test.values)
    return _is_commit_name(test)


def _false_only_if_committing(test: ast.expr) -> bool:
    """``if not commit`` / ``if not commit or ...``: the else runs only when true."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_is_not_commit(value) for value in test.values)
    return _is_not_commit(test)


def _leaves(statements: list[ast.stmt]) -> bool:
    return bool(statements) and isinstance(statements[-1], (ast.Return, ast.Raise))


class _Walker:
    """Calls of one scope's body, each with its transaction and guard context.
    Nested scopes (``def``, ``lambda``, ``class``) are not entered: each is a
    scope of its own."""

    def __init__(self, imports: dict[str, tuple[str, str | None]], module: str) -> None:
        self.imports = imports
        self.module = module

    def calls(self, body: list[ast.stmt]) -> Iterator[tuple[ast.Call, bool, bool]]:
        """Yield (call, inside a transaction block, governed by ``commit``)."""
        yield from self._block(body, False, False)

    def expression_calls(
        self, expression: ast.expr
    ) -> Iterator[tuple[ast.Call, bool, bool]]:
        """The calls of a lambda's body."""
        yield from self._expression(expression, False, False)

    def _is_transaction(self, item: ast.withitem) -> bool:
        expr = item.context_expr
        return isinstance(expr, ast.Call) and TRANSACTION in _resolve(
            expr.func, self.imports, self.module
        )

    def _block(
        self, statements: list[ast.stmt], inside: bool, governed: bool
    ) -> Iterator[tuple[ast.Call, bool, bool]]:
        for statement in statements:
            yield from self._statement(statement, inside, governed)
            if (
                isinstance(statement, ast.If)
                and _false_only_if_committing(statement.test)
                and _leaves(statement.body)
            ):
                # ``if not commit: return ...``: what follows runs only when the
                # function was asked to commit.
                governed = True

    def _statement(
        self, statement: ast.stmt, inside: bool, governed: bool
    ) -> Iterator[tuple[ast.Call, bool, bool]]:
        if isinstance(statement, _SCOPES):
            # A nested definition does not run here; decorators and defaults do.
            for expression in (
                *getattr(statement, "decorator_list", ()),
                *_argument_defaults(statement),
            ):
                yield from self._expression(expression, inside, governed)
            return
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            opens = False
            for item in statement.items:
                yield from self._expression(item.context_expr, inside, governed)
                opens = opens or self._is_transaction(item)
            yield from self._block(statement.body, inside or opens, governed)
            return
        if isinstance(statement, ast.If):
            yield from self._expression(statement.test, inside, governed)
            test = statement.test
            yield from self._block(
                statement.body, inside, governed or _true_only_if_committing(test)
            )
            yield from self._block(
                statement.orelse, inside, governed or _false_only_if_committing(test)
            )
            return
        for _field, value in ast.iter_fields(statement):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                yield from self._block(value, inside, governed)
            elif isinstance(value, list):
                for element in value:
                    if isinstance(element, ast.excepthandler):
                        yield from self._block(element.body, inside, governed)
                    elif isinstance(element, ast.match_case):
                        yield from self._block(element.body, inside, governed)
                    elif isinstance(element, ast.AST):
                        yield from self._expression(element, inside, governed)
            elif isinstance(value, ast.AST):
                yield from self._expression(value, inside, governed)

    def _expression(
        self, node: ast.AST, inside: bool, governed: bool
    ) -> Iterator[tuple[ast.Call, bool, bool]]:
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Call):
            yield node, inside, governed
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                continue
            yield from self._expression(child, inside, governed)


def _argument_defaults(node: ast.AST) -> list[ast.expr]:
    arguments = getattr(node, "args", None)
    if not isinstance(arguments, ast.arguments):
        return []
    return [
        *arguments.defaults,
        *(default for default in arguments.kw_defaults if default is not None),
    ]


@dataclass
class _Module:
    name: str
    path: Path
    tree: ast.Module
    imports: dict[str, tuple[str, str | None]]


def _inventory(
    package_root: Path,
) -> tuple[dict[tuple[str, str], _Function], list[_Module]]:
    if package_root.name != ROOT_PACKAGE or not package_root.is_dir():
        raise TransactionCheckError(
            f"package root must be an existing directory named {ROOT_PACKAGE!r}: "
            f"{package_root}"
        )
    functions: dict[tuple[str, str], _Function] = {}
    modules: list[_Module] = []
    owner_found = False
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(package_root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise TransactionCheckError(
                f"cannot parse {path}:{exc.lineno}: {exc.msg}"
            ) from exc
        imports = _import_table(tree, module=module, path=path)
        modules.append(_Module(module, path, tree, imports))
        for node in tree.body:
            if not isinstance(node, _FUNCTIONS):
                continue
            if (module, node.name) == TRANSACTION:
                owner_found = True  # Its commit is the block's own.
                continue
            functions[(module, node.name)] = _Function(
                module,
                node,
                path,
                imports,
                _signature(node),
                list(_Walker(imports, module).calls(node.body)),
            )
    if not owner_found:
        raise TransactionCheckError(
            "athena.core.db.transaction is missing: there is no block to check"
        )
    return functions, modules


@dataclass(frozen=True)
class _Scope:
    """One body that runs as a unit: a function at any depth, a method, a
    lambda, a class body or a module body."""

    name: str  # Qualified: athena.pkg.module.outer.inner, or <module>.
    signature: _Signature | None
    calls: tuple[tuple[ast.Call, bool, bool], ...]


def _scopes(module: _Module) -> Iterator[_Scope]:
    walker = _Walker(module.imports, module.name)
    yield _Scope(f"{module.name}.<module>", None, tuple(walker.calls(module.tree.body)))

    def nested(node: ast.AST, prefix: str) -> Iterator[_Scope]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTIONS):
                name = f"{prefix}.{child.name}"
                calls = tuple(walker.calls(child.body))
                yield _Scope(name, _signature(child), calls)
                yield from nested(child, name)
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}"
                yield _Scope(name, None, tuple(walker.calls(child.body)))
                yield from nested(child, name)
            elif isinstance(child, ast.Lambda):
                name = f"{prefix}.<lambda>"
                yield _Scope(name, None, tuple(walker.expression_calls(child.body)))
                yield from nested(child, name)
            else:
                yield from nested(child, prefix)

    yield from nested(module.tree, module.name)


_UNCONDITIONAL = "unconditional"


def _commits_at(
    call: ast.Call, caller: _Function, functions: dict[tuple[str, str], _Function]
) -> tuple[_Function, str] | None:
    """(callee, why) when this call commits, where why is ``_UNCONDITIONAL``,
    ``_TRUE`` or ``_PASSED`` (commits when the caller's own commit is true)."""
    for candidate in sorted(_resolve(call.func, caller.imports, caller.module)):
        callee = functions.get(candidate)
        if callee is None:
            continue
        if callee.unconditional:
            return callee, _UNCONDITIONAL
        if callee.signature is not None:
            kind = _argument_kind(call, callee.signature)
            if kind != _FALSE:
                return callee, kind
    return None


def _summarize(functions: dict[tuple[str, str], _Function]) -> None:
    """Mark every function that commits whatever its own ``commit`` says."""
    changed = True
    while changed:
        changed = False
        for key in sorted(functions):
            function = functions[key]
            if function.unconditional:
                continue
            governs = function.signature is not None
            for call, inside, governed in function.calls:
                if inside or (governed and governs):
                    continue  # Reported where it is / honours its own commit.
                point = _is_commit_point(call)
                if point is not None:
                    function.reason = f"{point} at line {call.lineno}"
                else:
                    hit = _commits_at(call, function, functions)
                    if hit is None or (hit[1] == _PASSED and governs):
                        continue
                    callee = hit[0]
                    function.reason = (
                        f"calls {callee.module}.{callee.node.name} at line "
                        f"{call.lineno}"
                    )
                function.unconditional = True
                changed = True
                break


def _finding(call: ast.Call, caller: _Function, functions) -> str | None:
    point = _is_commit_point(call)
    if point is not None:
        return f"{point} commits inside db.transaction"
    hit = _commits_at(call, caller, functions)
    if hit is None:
        return None
    callee, why = hit
    name = f"{callee.module}.{callee.node.name}"
    if why == _UNCONDITIONAL:
        return (
            f"{name} commits whatever its commit argument ({callee.reason}); "
            "make it honour commit=False or restructure"
        )
    if why == _PASSED:
        return f"{name} commits when this function's own commit is true"
    return f"{name} commits: its commit argument is not False; pass commit=False"


def _own_findings(scope: _Scope) -> Iterator[tuple[ast.Call, str]]:
    """B and C findings of one scope, C before B. A commit point that is also an
    A finding is dropped by the caller, which reports each call once."""
    for call, _inside, governed in scope.calls:
        statement = _transaction_statement(call)
        if statement is not None:
            yield (
                call,
                (
                    f"{statement} manages a transaction by hand; use "
                    "db.transaction(conn[, immediate=True])"
                ),
            )
            continue
        point = _is_commit_point(call)
        if point is None or (governed and scope.signature is not None):
            continue
        yield (
            call,
            (
                f"{point} commits on its own; wrap the write in db.transaction, or "
                "take commit and commit only when it is true"
            ),
        )


def find_violations(
    package_root: Path = PACKAGE_ROOT,
    *,
    allowed: Mapping[str, str] | None = None,
) -> list[Violation]:
    """Every A, B and C finding, and every stale ``allowed`` entry.

    ``allowed`` defaults to ``ALLOWED_OWN_TRANSACTIONS`` for the repository's
    own package and to nothing for any other tree: the entries name this
    package's functions."""
    if allowed is None:
        own = package_root.resolve() == PACKAGE_ROOT.resolve()
        allowed = ALLOWED_OWN_TRANSACTIONS if own else {}
    functions, modules = _inventory(package_root.absolute())
    _summarize(functions)
    reported: set[tuple[Path, int, int]] = set()
    violations: list[Violation] = []
    for function in functions.values():
        for call, inside, _governed in function.calls:
            if inside and (detail := _finding(call, function, functions)) is not None:
                reported.add((function.path, call.lineno, call.col_offset))
                violations.append(
                    Violation(
                        function.path,
                        call.lineno,
                        f"{function.module}.{function.node.name}",
                        detail,
                    )
                )
    names: dict[str, Path] = {}
    excused: set[str] = set()
    for module in modules:
        for scope in _scopes(module):
            names[scope.name] = module.path
            if module.name == OWNER_MODULE:
                continue
            for call, detail in _own_findings(scope):
                key = (module.path, call.lineno, call.col_offset)
                if key in reported:
                    continue
                reported.add(key)
                if scope.name in allowed:
                    excused.add(scope.name)
                    continue
                violations.append(
                    Violation(module.path, call.lineno, scope.name, detail)
                )
    for name in sorted(set(allowed) - excused):
        if name in names:
            detail = (
                "stale allow-list entry: it no longer commits or manages a "
                "transaction on its own; delete the entry"
            )
            violations.append(Violation(names[name], 0, name, detail))
        else:
            detail = "stale allow-list entry: no such function; delete the entry"
            violations.append(Violation(Path(__file__).resolve(), 0, name, detail))
    return sorted(violations, key=lambda v: (str(v.path), v.line, v.detail))


def _display(path: Path, package_root: Path) -> str:
    root = package_root.parent
    if root.name == "src":
        root = root.parent
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="fail on a commit that db.transaction does not own"
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        default=PACKAGE_ROOT,
        help=(
            "Athena package root (default: repository src/athena); the allow-list "
            "names that package's functions and applies only to it"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    package_root = args.package_root.absolute()
    try:
        violations = find_violations(package_root)
    except (TransactionCheckError, OSError, UnicodeError) as exc:
        print(f"Athena transaction commits failed: {exc}", file=sys.stderr)
        return 1
    if violations:
        count = len(violations)
        print(
            f"Athena transaction commits failed: {count} "
            f"{'violation' if count == 1 else 'violations'}",
            file=sys.stderr,
        )
        for violation in violations:
            print(
                f"{_display(violation.path, package_root)}:{violation.line}: "
                f"{violation.function}: {violation.detail}",
                file=sys.stderr,
            )
        return 1
    print(
        "Athena transaction commits passed: nothing inside a db.transaction block "
        "commits, and outside core/db.py nothing commits on its own or manages a "
        f"transaction by hand but the {len(ALLOWED_OWN_TRANSACTIONS)} allowed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
