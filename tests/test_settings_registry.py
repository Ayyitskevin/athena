"""Every ``ATHENA_*`` environment variable the code reads is declared once.

WHY (B6a): the settings table in OPERATIONS.md was kept by hand and had drifted —
it omitted 13 of the variables the code read, so an operator could not learn
from the runbook that dispatch, the Buzz radio or the worker registry were
configurable at all. ``athena.config.SETTINGS`` is now the one registry: its
name, default, description and whether the value is a secret. This file holds
the code to it statically, and the OPERATIONS.md tables are generated from it.

The scan is an AST pass over every Python file under ``src/`` and ``scripts/``
(plus a regex over ``scripts/*.sh``). It imports nothing it scans. A *read* is:

- ``<x>.environ.get(K[, default])``, ``environ.get(...)`` on a parameter named
  ``environ``, ``os.getenv(K[, default])``, ``<x>.environ[K]`` in a load, or
  ``K in <x>.environ``;
- a call to a function of the same module that passes its own parameter on to
  a read (``config._int_env("ATHENA_X", ...)`` → ``_env(name)`` →
  ``os.environ.get(name)``), followed to a fixed point;
- ``$ATHENA_X`` / ``${ATHENA_X...}`` in a shell script under ``scripts/``.

``K`` resolves when it is a string literal or a module-level name bound to one
(``PROFILE_ENV``, ``WORKERS_VARIABLE``). Any other key — a computed name, a
variable — fails the test rather than being guessed at, except a reading
helper's own parameter, whose callers are the reads.

Out of reach: reads in files outside ``src/`` and ``scripts/`` (``examples/``,
``Dockerfile``, documentation), and reads through a function of another module
that forwards its parameter (none exist; ``config``'s helpers are private).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import re
import sys

import pytest

from athena import config

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = (REPO / "src", REPO / "scripts")
SHELL_GLOB = "scripts/*.sh"
NAME = re.compile(r"ATHENA_[A-Z0-9]+(?:_[A-Z0-9]+)*\Z")
SHELL_READ = re.compile(r"\$\{?(ATHENA_[A-Z0-9_]*[A-Z0-9])")
NO_DEFAULT = object()

# Exact ``ATHENA_*`` string literals that name no variable Athena's code reads.
# Each is explicit, with its reason; a new one is a decision, not a drive-by.
NOT_SETTINGS = {
    "ATHENA_URL": (
        "set by scripts/field_exercise.py for examples/icarus_executor.py, the "
        "stand-in executor it launches; Athena itself never reads it"
    ),
}


@dataclass(frozen=True)
class Read:
    path: str
    line: int
    name: str
    default: object = NO_DEFAULT


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "environ") or (
        isinstance(node, ast.Attribute) and node.attr == "environ"
    )


def _direct_read(node: ast.AST) -> tuple[ast.AST, ast.AST | None] | None:
    """The key (and literal default, if any) of a direct environment read."""
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        is_get = isinstance(func, ast.Attribute) and func.attr == "get"
        if is_get and _is_environ(func.value):
            return node.args[0], (node.args[1] if len(node.args) > 1 else None)
        is_getenv = (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
            isinstance(func, ast.Name) and func.id == "getenv"
        )
        if is_getenv:
            return node.args[0], (node.args[1] if len(node.args) > 1 else None)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_environ(node.value)
    ):
        return node.slice, None
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and _is_environ(node.comparators[0])
    ):
        return node.left, None
    return None


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names bound to one string literal."""
    found: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = value.value
    return found


def _params(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    arguments = function.args
    return [
        a.arg for a in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    ]


def _argument(call: ast.Call, index: int, param: str) -> ast.AST | None:
    if index < len(call.args):
        return call.args[index]
    for keyword in call.keywords:
        if keyword.arg == param:
            return keyword.value
    return None


def _readers(tree: ast.Module) -> dict[str, tuple[int, str]]:
    """Functions of this module that read the environment variable their own
    parameter names: name -> (parameter index, parameter name)."""
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    readers: dict[str, tuple[int, str]] = {}
    changed = True
    while changed:
        changed = False
        for function in functions:
            if function.name in readers:
                continue
            params = _params(function)
            for node in ast.walk(function):
                key: ast.AST | None = None
                direct = _direct_read(node)
                if direct is not None:
                    key = direct[0]
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in readers
                ):
                    index, param = readers[node.func.id]
                    key = _argument(node, index, param)
                if isinstance(key, ast.Name) and key.id in params:
                    readers[function.name] = (params.index(key.id), key.id)
                    changed = True
                    break
    return readers


def scan_source(source: str, path: str) -> tuple[list[Read], list[str]]:
    """The resolved reads in one Python source, and the reads whose key it
    cannot resolve (anything but a literal, a module-level string constant, or
    the reading function's own forwarded parameter)."""
    tree = ast.parse(source)
    constants = _constants(tree)
    readers = _readers(tree)
    reads: list[Read] = []
    unresolved: list[str] = []

    def visit(node: ast.AST, forwarded: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            reader = readers.get(node.name)
            forwarded = reader[1] if reader is not None else None
        key: ast.AST | None = None
        default: ast.AST | None = None
        direct = _direct_read(node)
        if direct is not None:
            key, default = direct
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in readers
        ):
            index, param = readers[node.func.id]
            key = _argument(node, index, param)
        if key is not None:
            name: str | None = None
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                name = key.value
            elif isinstance(key, ast.Name) and key.id in constants:
                name = constants[key.id]
            elif not (isinstance(key, ast.Name) and key.id == forwarded):
                # A reader forwarding its own parameter is not a read: its
                # callers are. Anything else is a name the scan cannot know.
                unresolved.append(f"{path}:{node.lineno}: {ast.unparse(key)}")
            if name is not None and name.startswith("ATHENA_"):
                literal = (
                    default.value if isinstance(default, ast.Constant) else NO_DEFAULT
                )
                reads.append(Read(path, node.lineno, name, literal))
        for child in ast.iter_child_nodes(node):
            visit(child, forwarded)

    visit(tree, None)
    return reads, unresolved


def _python_files() -> list[Path]:
    return sorted(
        path
        for root in SCAN_ROOTS
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def scan() -> tuple[list[Read], list[str]]:
    reads: list[Read] = []
    unresolved: list[str] = []
    for path in _python_files():
        relative = str(path.relative_to(REPO))
        found, unknown = scan_source(path.read_text(), relative)
        reads += found
        unresolved += unknown
    for path in sorted(REPO.glob(SHELL_GLOB)):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            for match in SHELL_READ.finditer(line):
                reads.append(Read(str(path.relative_to(REPO)), number, match.group(1)))
    return reads, unresolved


def name_literals() -> dict[str, list[str]]:
    """Every exact ``ATHENA_*`` string literal under src/ and scripts/."""
    found: dict[str, list[str]] = {}
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and NAME.match(node.value)
            ):
                found.setdefault(node.value, []).append(
                    f"{path.relative_to(REPO)}:{node.lineno}"
                )
    return found


@pytest.fixture(scope="module")
def scanned() -> tuple[list[Read], list[str]]:
    return scan()


def test_every_read_names_a_declared_setting(scanned):
    reads, _ = scanned
    undeclared = sorted(
        f"{read.path}:{read.line}: {read.name}"
        for read in reads
        if read.name not in config.SETTINGS
    )
    assert undeclared == [], (
        "these reads name an ATHENA_* variable athena.config.SETTINGS does not "
        f"declare — add a Setting for each: {undeclared}"
    )


def test_every_declared_setting_is_read_somewhere(scanned):
    reads, _ = scanned
    unread = sorted(set(config.SETTINGS) - {read.name for read in reads})
    assert unread == [], (
        f"declared settings nothing reads — delete them or read them: {unread}"
    )


def test_no_read_computes_its_variable_name(scanned):
    _, unresolved = scanned
    assert unresolved == [], (
        "these environment reads name their variable through a value the "
        f"registry check cannot follow: {unresolved}"
    )


def test_a_literal_default_at_a_read_matches_the_registry(scanned):
    reads, _ = scanned
    mismatched = []
    for read in reads:
        setting = config.SETTINGS.get(read.name)
        if setting is None or read.default is NO_DEFAULT:
            continue
        if read.default != setting.default and read.default != str(setting.default):
            mismatched.append(
                f"{read.path}:{read.line}: {read.name} defaults to "
                f"{read.default!r} there but {setting.default!r} in the registry"
            )
    assert mismatched == []


def test_every_athena_name_in_the_code_is_a_setting_or_a_stated_exception():
    """Names the code writes or emits (a child's environment, the MCP launch
    block) are held to the registry too, so a variable cannot be configured on
    one side and spelled differently on the other."""
    unknown = {
        name: places
        for name, places in name_literals().items()
        if name not in config.SETTINGS and name not in NOT_SETTINGS
    }
    assert unknown == {}
    stale = sorted(set(NOT_SETTINGS) - set(name_literals()))
    assert stale == [], f"exceptions that no longer match any literal: {stale}"
    assert not set(NOT_SETTINGS) & set(config.SETTINGS)


def test_the_scan_finds_the_reads_it_must(scanned):
    """Canaries for each read shape the scan claims to follow, so a scan that
    silently matches nothing fails here rather than passing everything."""
    reads, _ = scanned
    places = {(read.path, read.name) for read in reads}
    assert ("src/athena/config.py", "ATHENA_DB") in places  # _str_env -> _env
    assert ("src/athena/config.py", "ATHENA_COOKIE_SECURE") in places  # _bool_env
    assert ("src/athena/mcp/server.py", "ATHENA_TOKEN") in places  # os.environ.get
    assert ("src/athena/mcp/server.py", "ATHENA_MCP_PROFILE") in places  # constant
    assert ("src/athena/ops.py", "ATHENA_BASE_URL") in places
    assert ("scripts/gate.py", "ATHENA_COVERAGE_WORKERS") in places
    assert ("scripts/coverage.sh", "ATHENA_PYTHON") in places
    assert len({read.name for read in reads}) == len(config.SETTINGS)


@pytest.mark.parametrize(
    "source, expected",
    [
        ('import os\nos.environ.get("ATHENA_NEW")', ["ATHENA_NEW"]),
        ('import os\nos.environ["ATHENA_NEW"]', ["ATHENA_NEW"]),
        ('import os\nos.getenv("ATHENA_NEW", "1")', ["ATHENA_NEW"]),
        ('import os\nx = "ATHENA_NEW" in os.environ', ["ATHENA_NEW"]),
        ('import os\nK = "ATHENA_NEW"\nos.environ.get(K)', ["ATHENA_NEW"]),
        (
            "import os\n"
            "def _raw(name):\n    return os.environ.get(name)\n"
            "def _num(name, *, minimum):\n    return int(_raw(name))\n"
            'X = _num("ATHENA_NEW", minimum=1)',
            ["ATHENA_NEW"],
        ),
        ('import os\nos.environ["ATHENA_NEW"] = "1"', []),  # a write, not a read
        ('env = {}\nenv["ATHENA_NEW"]', []),  # not the process environment
    ],
)
def test_the_scan_recognises_each_read_shape(source, expected):
    reads, unresolved = scan_source(source, "synthetic.py")
    assert [read.name for read in reads] == expected
    assert unresolved == []


def test_a_computed_name_is_reported_not_guessed():
    _, unresolved = scan_source(
        'import os\nos.environ.get(f"ATHENA_{x}")', "synthetic.py"
    )
    assert len(unresolved) == 1


def test_config_refuses_to_read_an_undeclared_name():
    with pytest.raises(KeyError, match="ATHENA_NOT_DECLARED"):
        config._env("ATHENA_NOT_DECLARED")


def test_registry_entries_are_well_formed():
    for setting in config.SETTINGS.values():
        assert NAME.match(setting.name), setting.name
        assert setting.group in config.SETTING_GROUPS, setting.name
        assert setting.description.strip(), setting.name
        assert "\n" not in setting.description, setting.name
        # A value that is a credential is marked so; the table says which.
        if setting.name.endswith(("_SECRET", "_TOKEN")):
            assert setting.secret, setting.name


# --- the generated tables in OPERATIONS.md -------------------------------------


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_settings_doc", REPO / "scripts" / "generate_settings_doc.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_settings_doc"] = module
    spec.loader.exec_module(module)
    return module


def test_operations_settings_tables_match_the_registry():
    generator = _load_generator()
    committed = (REPO / "docs" / "OPERATIONS.md").read_text()
    assert committed == generator.render(), (
        "docs/OPERATIONS.md's settings tables are stale — regenerate with "
        "`python scripts/generate_settings_doc.py`"
    )


def test_every_setting_has_one_row_and_no_hand_table_lists_variables():
    generator = _load_generator()
    text = (REPO / "docs" / "OPERATIONS.md").read_text()
    for name in config.SETTINGS:
        assert text.count(f"| `{name}` |") == 1, name
    # A table row naming a variable outside the generated blocks is a second,
    # hand-kept inventory: exactly what drifted before.
    prose = generator.generated_blocks.outside_blocks(text)
    hand_rows = [line for line in prose.splitlines() if line.startswith("| `ATHENA_")]
    assert hand_rows == []
