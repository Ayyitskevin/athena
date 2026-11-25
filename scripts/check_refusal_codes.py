"""Enforce that every refusal names a code the registry knows.

Requirement A5 (docs/plans/AGENTIC_USE_SPEC_ATHENA.md): every 4xx and 5xx JSON
body from a REST route carries a top-level ``code`` from one registry,
``src/athena/core/refusal_codes.py``. Refusals are raised at run time, so a
route table cannot list them; this pass reads the source instead and fails on:

1. a ``HTTPException(...)`` constructed anywhere in the package. Routes raise
   ``Refusal(..., code=...)`` (``core/refusals.py``) so the code is explicit;
2. a ``Refusal(...)`` without ``code=``;
3. a kind-to-status table (a dict literal from string kinds to HTTP statuses,
   named ``*STATUS*``) without a code table beside it: the same name with
   ``STATUS`` replaced by ``CODE``, in the same scope, mapping every kind;
4. a code the registry lacks, wherever one is named: a string literal inside
   a ``code=`` argument, a ``"code"`` value in a ``JSONResponse`` or
   middleware body, a code table's values, the code half of a
   ``{kind: (status, code)}`` table, or a module constant ``*_CODE = "..."``;
5. a ``JSONResponse`` or middleware body literal with a ``"detail"`` and no
   ``"code"``.

Rules 1 and 2 cover the whole package. Rules 4 and 5 also cover ``web/``
(G4): the cockpit answers mostly in HTML, but its JSON routes (the command
palette, the fleet roster's and a run replay's JSON, a project's floor) answer
clients too, and their refusals name registered codes like any REST route's.
Rule 3 skips ``web/``, whose status tables answer in HTML and carry no code.
Rules 3 to 5 skip ``mcp/`` (an HTTP client, not a REST route).

MCP-side codes (``McpRefusalCode`` entries: failures that never reach Athena)
have no HTTP status, so they are kept apart: a ``code=`` literal under ``mcp/``
must name a registered code of either kind, and one in a REST module that names
an MCP-side code is refused, because no HTTP answer may carry it.
A dynamic code (``code=CODE_BY_KIND[exc.kind]``) passes rule 2, and its table
is held by rules 3 and 4. A code passed through a variable is out of reach,
like any computed value; the request-level sweep in
``tests/test_refusal_codes.py`` covers those at run time.

Standard library only; it never imports Athena. Usage:

    python scripts/check_refusal_codes.py
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"
REGISTRY_PATH = PACKAGE_ROOT / "core" / "refusal_codes.py"
REFUSAL_MODULE = PACKAGE_ROOT / "core" / "refusals.py"
# Rules 3-5 do not apply under these package directories.
_NON_REST_AREAS = ("web", "mcp")
#: Areas outside REST whose JSON answers are still held to rules 4 and 5 (G4).
_JSON_ANSWER_AREAS = ("web",)


def json_rules_apply(area: str) -> bool:
    """Whether a module in ``area`` (the first path part under the package) is
    held to rules 4 and 5: every REST area, and the cockpit's JSON routes."""
    return area not in _NON_REST_AREAS or area in _JSON_ANSWER_AREAS


_JSON_BODY_CALLS = frozenset({"JSONResponse", "_send_json_response"})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def registered_codes(
    registry_source: str, constructor: str = "RefusalCode"
) -> frozenset[str]:
    """The ``code=`` of every ``RefusalCode(...)`` in the registry module, or of
    every ``McpRefusalCode(...)`` with ``constructor="McpRefusalCode"``."""
    codes: set[str] = set()
    for node in ast.walk(ast.parse(registry_source)):
        if isinstance(node, ast.Call) and _call_name(node) == constructor:
            for keyword in node.keywords:
                if keyword.arg == "code" and _is_str(keyword.value):
                    codes.add(keyword.value.value)  # type: ignore[attr-defined]
    return frozenset(codes)


def mcp_side_codes(registry_source: str) -> frozenset[str]:
    """The codes only the MCP layer answers with (no HTTP status)."""
    return registered_codes(registry_source, "McpRefusalCode")


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_status(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and type(node.value) is int
        and 100 <= node.value <= 599
    )


def _status_table(node: ast.AST) -> list[str] | None:
    """The kinds of a ``{"kind": <status>, ...}`` dict literal, else None."""
    if not isinstance(node, ast.Dict) or not node.keys:
        return None
    if all(key is not None and _is_str(key) for key in node.keys) and all(
        _is_status(value) for value in node.values
    ):
        return [key.value for key in node.keys]  # type: ignore[union-attr]
    return None


def _str_literals(node: ast.AST) -> list[ast.Constant]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Constant) and _is_str(n)]


def _assigned_name(node: ast.AST) -> tuple[str, ast.AST] | None:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id, node.value
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ):
        return node.target.id, node.value
    return None


def check_source(
    source: str,
    path: str,
    codes: frozenset[str],
    *,
    rest: bool = True,
    mcp_codes: frozenset[str] | None = None,
    json_bodies: bool | None = None,
) -> list[Violation]:
    """Every violation in one module. ``rest`` turns on rules 3 to 5;
    ``json_bodies`` turns on rules 4 and 5 alone (default: as ``rest``). With
    ``mcp_codes`` and ``rest`` off (the ``mcp/`` area), a ``code=`` literal may
    also name an MCP-side code; with ``rest`` on, naming one is refused."""
    tree = ast.parse(source)
    found: list[Violation] = []
    mcp_side = mcp_codes or frozenset()
    if json_bodies is None:
        json_bodies = rest

    def unknown(node: ast.AST, where: str, allowed: frozenset[str] = codes) -> None:
        for literal in _str_literals(node):
            if literal.value in allowed:
                continue
            if literal.value in mcp_side:
                found.append(
                    Violation(
                        path,
                        literal.lineno,
                        f"{where} names {literal.value!r}, an MCP-side code with "
                        "no HTTP status; a REST answer may not carry it",
                    )
                )
                continue
            found.append(
                Violation(
                    path,
                    literal.lineno,
                    f"{where} names {literal.value!r}, which "
                    "core/refusal_codes.py does not register",
                )
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "HTTPException":
            found.append(
                Violation(
                    path,
                    node.lineno,
                    "HTTPException(...) carries no refusal code; raise "
                    "core.refusals.Refusal(..., code=...) instead",
                )
            )
        elif name == "Refusal":
            code_args = [k.value for k in node.keywords if k.arg == "code"]
            if not code_args:
                found.append(Violation(path, node.lineno, "Refusal(...) without code="))
            for value in code_args:
                unknown(value, "code=")
        elif name == "McpRefusalCode":
            continue  # the registry's own definitions, not an answer
        elif not rest and mcp_codes is not None:
            for keyword in node.keywords:
                if keyword.arg == "code" and _is_str(keyword.value):
                    unknown(keyword.value, "code=", codes | mcp_codes)
        elif json_bodies:
            for keyword in node.keywords:
                if keyword.arg == "code" and _is_str(keyword.value):
                    unknown(keyword.value, "code=")
            if name in _JSON_BODY_CALLS:
                bodies = [k.value for k in node.keywords if k.arg == "content"]
                bodies += [a for a in node.args if isinstance(a, ast.Dict)]
                for body in bodies:
                    if not isinstance(body, ast.Dict):
                        continue
                    keys = {k.value for k in body.keys if k is not None and _is_str(k)}  # type: ignore[union-attr]
                    if "detail" in keys and "code" not in keys:
                        found.append(
                            Violation(
                                path,
                                body.lineno,
                                f'{name} body has "detail" but no "code"',
                            )
                        )
                    for key, value in zip(body.keys, body.values):
                        if key is not None and _is_str(key) and key.value == "code":  # type: ignore[union-attr]
                            unknown(value, '"code"')

    if not rest:
        return found

    # Rules 3 and 4 over the assignments of each scope (module and functions).
    scopes: list[list[ast.stmt]] = [tree.body]
    scopes += [
        n.body
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    claimed: set[int] = set()
    for body in scopes:
        assigned: dict[str, ast.AST] = {}
        for statement in body:
            pair = _assigned_name(statement)
            if pair is not None:
                assigned[pair[0]] = pair[1]
        for name, value in assigned.items():
            if name.endswith("_CODE") and _is_str(value):
                unknown(value, name)
            kinds = _status_table(value)
            if kinds is None:
                continue
            claimed.add(id(value))
            if "STATUS" not in name:
                found.append(
                    Violation(
                        path,
                        value.lineno,
                        f"kind-to-status table {name} must be named *STATUS* so "
                        "its code table can sit beside it",
                    )
                )
                continue
            code_name = name.replace("STATUS", "CODE")
            code_table = assigned.get(code_name)
            if not isinstance(code_table, ast.Dict):
                found.append(
                    Violation(
                        path,
                        value.lineno,
                        f"kind-to-status table {name} has no code table "
                        f"{code_name} beside it",
                    )
                )
                continue
            code_kinds = {
                k.value  # type: ignore[union-attr]
                for k in code_table.keys
                if k is not None and _is_str(k)
            }
            for kind in kinds:
                if kind not in code_kinds:
                    found.append(
                        Violation(
                            path,
                            value.lineno,
                            f"kind {kind!r} in {name} has no code in {code_name}",
                        )
                    )
            for code_value in code_table.values:
                unknown(code_value, code_name)
    for node in ast.walk(tree):
        if id(node) not in claimed and _status_table(node) is not None:
            found.append(
                Violation(
                    path,
                    node.lineno,
                    "a kind-to-status mapping must be a named *STATUS* table with "
                    "a *CODE* table beside it",
                )
            )
        # {kind: (status, code)}: the code is named beside its status already.
        if isinstance(node, ast.Dict):
            for value in node.values:
                if (
                    isinstance(value, ast.Tuple)
                    and len(value.elts) == 2
                    and _is_status(value.elts[0])
                    and _is_str(value.elts[1])
                ):
                    unknown(value.elts[1], "(status, code) table")
    return found


def check(root: Path = PACKAGE_ROOT) -> list[Violation]:
    registry = REGISTRY_PATH.read_text()
    codes = registered_codes(registry)
    if not codes:
        return [Violation(str(REGISTRY_PATH), 1, "the registry declares no codes")]
    mcp_codes = mcp_side_codes(registry)
    found = [
        Violation(str(REGISTRY_PATH), 1, f"{code!r} is registered as both kinds")
        for code in sorted(codes & mcp_codes)
    ]
    for path in sorted(root.rglob("*.py")):
        if path == REFUSAL_MODULE:
            continue  # defines Refusal on top of HTTPException
        relative = path.relative_to(root)
        area = relative.parts[0]
        rest = area not in _NON_REST_AREAS
        found += check_source(
            path.read_text(),
            str(path.relative_to(root.parent)),
            codes,
            rest=rest,
            mcp_codes=mcp_codes if (rest or area == "mcp") else None,
            json_bodies=json_rules_apply(area),
        )
    return found


def main(argv: list[str] | None = None) -> int:
    del argv
    violations = check()
    for violation in violations:
        print(violation)
    if violations:
        print(f"{len(violations)} refusal-code violation(s)", file=sys.stderr)
        return 1
    print("every refusal names a registered code")
    return 0


if __name__ == "__main__":
    # --help answers without doing the work (tests/test_scripts_runnable.py).
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.parse_args(sys.argv[1:])
    raise SystemExit(main(sys.argv[1:]))
